"""
DracoDownloader 核心入口
"""

import asyncio
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, AsyncIterator
from dataclasses import dataclass, field

from .logger import get_logger
from .protocols import ProtocolRouter
from .protocols.base import DownloadHandle
from .scheduler import Scheduler
from .engine import DownloadEngine
from .progress import ProgressManager

log = get_logger('core')


@dataclass
class DownloadResult:
    """下载结果"""
    success: bool
    path: str
    size: int = 0
    speed: float = 0.0  # MB/s
    duration: float = 0.0
    protocol: str = "unknown"
    error: Optional[str] = None


@dataclass
class ProgressEvent:
    """进度事件"""
    progress: float  # 0-100
    speed: int  # bytes/s
    downloaded: int
    total: int
    message: str = ""


class DracoDownloader:
    """DracoDownloader 主类"""

    def __init__(self, max_concurrent: int = 5):
        self.scheduler = Scheduler(max_concurrent=max_concurrent)
        self.engine = DownloadEngine()
        self.progress = ProgressManager()
        self.router = ProtocolRouter()
        # 注册调度器执行器
        self.scheduler.set_executor(self._execute_task)
        log.info(f"DracoDownloader v{__import__('DracoDownloader', fromlist=['__version__']).__version__} initialized")

    def download(self, url: str, output_path: str,
                 headers: Optional[Dict[str, str]] = None,
                 proxy: Optional[str] = None,
                 callback: Optional[Callable[[ProgressEvent], None]] = None) -> DownloadResult:
        """同步下载接口"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # 无运行中的事件循环 → 可安全调用 asyncio.run()
        else:
            raise RuntimeError(
                "Cannot call download() in an async context. "
                "Use await downloader.download_async() instead."
            )

        return asyncio.run(self._download_async(
            url, output_path, headers, proxy, callback
        ))

    async def download_async(self, url: str, output_path: str,
                             headers: Optional[Dict[str, str]] = None,
                             proxy: Optional[str] = None,
                             callback: Optional[Callable[[ProgressEvent], None]] = None,
                             timeout: float = 3600) -> DownloadResult:
        """异步下载接口"""
        return await self._download_async(url, output_path, headers, proxy, callback, timeout=timeout)

    async def download_stream(self, url: str, output_path: str,
                              headers: Optional[Dict[str, str]] = None,
                              proxy: Optional[str] = None) -> AsyncIterator[ProgressEvent]:
        """流式进度下载（支持 asyncio 流式迭代）"""
        queue: asyncio.Queue = asyncio.Queue()
        cancel_event = asyncio.Event()

        def on_progress(event: ProgressEvent):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        # 启动下载任务（通过调度器管理）
        output_path = str(Path(output_path).resolve())
        driver = self.router.route(url)
        if driver is None:
            yield ProgressEvent(progress=0, speed=0, downloaded=0, total=0, message="不支持的协议")
            return

        handle = DownloadHandle(
            url=url,
            output_path=output_path,
            headers=headers or {},
            proxy=proxy
        )

        # 探测
        try:
            metadata = await driver.probe(url)
            handle.total_size = metadata.get('size', 0)
            handle.metadata = metadata
        except (ValueError, OSError, ConnectionError) as e:
            yield ProgressEvent(progress=0, speed=0, downloaded=0, total=0, message=f"探测失败: {e}")
            return

        # 通过调度器执行
        task_id = self.scheduler.add(handle, timeout=3600)
        download_task = asyncio.create_task(
            self._execute_download(driver, handle, task_id, on_progress)
        )

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield event
                    if event.progress >= 100:
                        break
                except asyncio.TimeoutError:
                    if download_task.done():
                        break
                    if cancel_event.is_set():
                        break
                    continue
        except asyncio.CancelledError:
            cancel_event.set()
            self.scheduler.cancel(task_id)
            download_task.cancel()
        finally:
            if not download_task.done():
                download_task.cancel()
                try:
                    await download_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _execute_task(self, handle, task_id: str) -> DownloadResult:
        """
        调度器执行器 - 由 scheduler._worker 在并发限制下调用

        Args:
            handle: DownloadHandle
            task_id: 调度器分配的任务 ID

        Returns:
            DownloadResult
        """
        driver = self.router.route(handle.url)
        if driver is None:
            return DownloadResult(
                success=False, path=handle.output_path,
                error=f"不支持的协议: {handle.url}"
            )
        return await self._execute_download(driver, handle, task_id, None)

    async def _execute_download(self, driver, handle, task_id: str,
                                callback: Optional[Callable[[ProgressEvent], None]] = None) -> DownloadResult:
        """实际下载执行（被 _execute_task 和 download_stream 调用）"""
        output_path = handle.output_path
        start_time = time.time()

        # 合并外部回调和 handle 上设置的回调
        user_callback = callback or handle.progress_callback

        try:
            # 探测（如果还没探测）
            if handle.total_size == 0:
                metadata = await driver.probe(handle.url)
                handle.total_size = metadata.get('size', 0)
                handle.metadata = metadata

            def progress_wrapper(downloaded: int, total: int, speed: int = 0):
                pct = (downloaded / total * 100) if total > 0 else 0
                event = ProgressEvent(
                    progress=pct, speed=speed,
                    downloaded=downloaded, total=total
                )
                if user_callback:
                    try:
                        user_callback(event)
                    except Exception:
                        pass
                self.progress.update(task_id, downloaded, total, speed)

            await driver.download(handle, callback=progress_wrapper)

            duration = time.time() - start_time
            speed_mb = (handle.total_size / 1024 / 1024) / duration if duration > 0 else 0

            log.info(f"Download complete: {output_path} ({handle.total_size} bytes, {speed_mb:.2f} MB/s)")

            return DownloadResult(
                success=True, path=output_path,
                size=handle.total_size,
                speed=speed_mb, duration=duration,
                protocol=driver.__class__.__name__
            )

        except (asyncio.CancelledError, Exception) as e:
            error_msg = str(e) if not isinstance(e, asyncio.CancelledError) else "下载已取消"
            log.error(f"Download failed: {error_msg}")
            return DownloadResult(
                success=False, path=output_path, error=error_msg
            )

    async def _download_async(self, url: str, output_path: str,
                              headers: Optional[Dict[str, str]] = None,
                              proxy: Optional[str] = None,
                              callback: Optional[Callable[[ProgressEvent], None]] = None,
                              timeout: float = 3600) -> DownloadResult:
        """内部异步下载 - 通过调度器管理"""
        output_path = str(Path(output_path).resolve())
        log.info(f"Download: {url[:80]} -> {output_path}")

        driver = self.router.route(url)
        if driver is None:
            log.warning(f"Unsupported protocol: {url}")
            return DownloadResult(
                success=False, path=output_path,
                error=f"不支持的协议: {url}"
            )

        handle = DownloadHandle(
            url=url,
            output_path=output_path,
            headers=headers or {},
            proxy=proxy
        )

        # 将外部回调传递到 handle，供 _execute_download 使用
        if callback:
            orig_callback = callback

            def wrapped_cb(event):
                try:
                    orig_callback(event)
                except Exception:
                    pass

            handle.progress_callback = wrapped_cb

        try:
            metadata = await driver.probe(url)
            handle.total_size = metadata.get('size', 0)
            handle.metadata = metadata
            log.debug(f"Probe: size={handle.total_size}, type={driver.__class__.__name__}")
        except (ValueError, OSError, ConnectionError) as e:
            log.error(f"Probe failed: {e}")
            return DownloadResult(
                success=False, path=output_path, error=f"探测失败: {e}"
            )

        # 添加到调度器，由 scheduler 管理并发和重试
        task_id = self.scheduler.add(handle, timeout=timeout)

        try:
            result = await self.scheduler.wait_for(task_id)

            return result

        except asyncio.CancelledError:
            self.scheduler.cancel(task_id)
            return DownloadResult(
                success=False, path=output_path, error="下载已取消"
            )
        except (ConnectionError, OSError, ValueError, RuntimeError, TimeoutError) as e:
            log.error(f"Download failed: {e}")
            return DownloadResult(
                success=False, path=output_path, error=str(e)
            )
        except Exception as e:
            log.exception(f"Unexpected download error: {e}")
            return DownloadResult(
                success=False, path=output_path, error=f"未知错误: {e}"
            )

    def list_protocols(self) -> list[str]:
        return self.router.list_supported()

    def get_status(self) -> Dict[str, Any]:
        return {
            'active': self.scheduler.active_count(),
            'queued': self.scheduler.queued_count(),
            'completed': self.scheduler.completed_count(),
            'failed': self.scheduler.failed_count(),
            'protocols': self.list_protocols()
        }

    def cancel(self, task_id: str) -> bool:
        return self.scheduler.cancel(task_id)

    def pause(self, task_id: str) -> bool:
        return self.scheduler.pause(task_id)

    def resume(self, task_id: str) -> bool:
        return self.scheduler.resume(task_id)
