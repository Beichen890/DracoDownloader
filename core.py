"""
DracoDownloader 核心入口
"""

import asyncio
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, AsyncIterator, List
from dataclasses import dataclass, field

from .logger import get_logger
from .protocols import ProtocolRouter
from .protocols.base import DownloadHandle
from .protocols.http import HTTPDriver
from .scheduler import Scheduler
from .engine import DownloadEngine
from .progress import ProgressManager
from .mirror_selector import (
    MirrorSelector, SmartMirrorDownloader,
    MIRROR_CATEGORIES, MirrorProbeResult
)
from .optimizer import DownloadOptimizer, OptimalParams

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
    mirror_used: Optional[str] = None   # 使用的镜像
    optimization: Optional[Dict[str, Any]] = None  # 优化信息


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

    def __init__(self, max_concurrent: int = 5,
                 auto_optimize: bool = True,
                 auto_mirror: bool = False,
                 mirror_region: str = "cn",
                 optimizer: Optional[DownloadOptimizer] = None,
                 mirror_selector: Optional[SmartMirrorDownloader] = None):
        """
        Args:
            max_concurrent: 最大并发任务数
            auto_optimize: 是否自动优化分片/线程参数
            auto_mirror: 是否自动选择最优镜像站
            mirror_region: 镜像区域 ("cn", "global", "auto")
            optimizer: 自定义优化器
            mirror_selector: 自定义镜像选择器
        """
        self.scheduler = Scheduler(max_concurrent=max_concurrent)
        self.engine = DownloadEngine()
        self.progress = ProgressManager()
        self.router = ProtocolRouter()
        self.auto_optimize = auto_optimize
        self.auto_mirror = auto_mirror
        self.mirror_region = mirror_region

        # 镜像选择器
        self._mirror_selector = mirror_selector
        if self.auto_mirror and self._mirror_selector is None:
            self._mirror_selector = SmartMirrorDownloader()

        # 优化器
        self._optimizer = optimizer
        if self.auto_optimize and self._optimizer is None:
            self._optimizer = DownloadOptimizer()

        # 将优化器注入到 HTTP 驱动
        self._inject_optimizer()

        # 注册调度器执行器
        self.scheduler.set_executor(self._execute_task)

        from DracoDownloader import __version__
        log.info(f"DracoDownloader v{__version__} initialized "
                 f"(optimize={auto_optimize}, mirror={auto_mirror})")

    def _inject_optimizer(self):
        """将优化器和镜像器注入到协议驱动中"""
        for driver in self.router._drivers:
            if isinstance(driver, HTTPDriver):
                if self._optimizer and self.auto_optimize:
                    driver._optimizer = self._optimizer
                if self.auto_optimize:
                    driver.auto_optimize = True

    async def _resolve_mirror_url(self, url: str) -> tuple[str, Optional[str]]:
        """
        解析镜像URL

        Returns:
            (最终URL, 镜像名称)
        """
        if not self.auto_mirror or self._mirror_selector is None:
            return url, None

        # 选择镜像列表
        if self.mirror_region == "auto":
            mirrors = MIRROR_CATEGORIES.get("cn", []) + MIRROR_CATEGORIES.get("global", [])
        else:
            mirrors = MIRROR_CATEGORIES.get(self.mirror_region, [])

        if not mirrors:
            return url, None

        try:
            mirror_url = await self._mirror_selector.select_mirror(url, mirrors)
            if mirror_url and mirror_url != url:
                log.info(f"使用镜像: {mirror_url}")
                # 从缓存获取镜像名称
                cache_key = url.split("//")[1].split("/")[0] if "//" in url else url
                cached = self._mirror_selector._cache.get(cache_key)
                mirror_name = cached[0].name if cached and cached[0] else "mirror"
                return mirror_url, mirror_name
        except Exception as e:
            log.warning(f"镜像选择失败，使用原始URL: {e}")

        return url, None

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

        output_path = str(Path(output_path).resolve())

        resolved_url, mirror_name = await self._resolve_mirror_url(url)

        driver = self.router.route(resolved_url)
        if driver is None:
            yield ProgressEvent(progress=0, speed=0, downloaded=0, total=0, message="不支持的协议")
            return

        handle = DownloadHandle(
            url=resolved_url,
            output_path=output_path,
            headers=headers or {},
            proxy=proxy
        )

        try:
            metadata = await driver.probe(resolved_url)
            handle.total_size = metadata.get('size', 0)
            handle.metadata = metadata
        except (ValueError, OSError, ConnectionError, RuntimeError, TimeoutError) as e:
            yield ProgressEvent(progress=0, speed=0, downloaded=0, total=0, message=f"探测失败: {e}")
            return

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
                        if mirror_name:
                            log.info(f"流式下载完成，使用镜像: {mirror_name}")
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

            # 获取优化信息
            opt_info = None
            if hasattr(driver, 'get_optimization_info'):
                try:
                    opt_info = driver.get_optimization_info()
                except Exception:
                    pass

            return DownloadResult(
                success=True, path=output_path,
                size=handle.total_size,
                speed=speed_mb, duration=duration,
                protocol=driver.__class__.__name__,
                optimization=opt_info,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            error_msg = str(e)
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

        # 镜像解析
        resolved_url, mirror_name = await self._resolve_mirror_url(url)

        driver = self.router.route(resolved_url)
        if driver is None:
            log.warning(f"Unsupported protocol: {resolved_url}")
            return DownloadResult(
                success=False, path=output_path,
                error=f"不支持的协议: {url}"
            )

        handle = DownloadHandle(
            url=resolved_url,
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
            metadata = await driver.probe(resolved_url)
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

            # 附加镜像信息
            if mirror_name:
                result.mirror_used = mirror_name

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

    async def optimize_url(self, url: str, file_size: int = 0) -> OptimalParams:
        """
        预优化指定URL

        Args:
            url: 目标URL
            file_size: 文件大小（0=自动探测）

        Returns:
            最优参数
        """
        if self._optimizer is None:
            self._optimizer = DownloadOptimizer()

        if file_size == 0:
            # 尝试探测文件大小
            driver = self.router.route(url)
            if driver:
                try:
                    metadata = await driver.probe(url)
                    file_size = metadata.get('size', 0)
                except Exception:
                    pass

        return await self._optimizer.optimize_for_url(url, file_size)

    def list_protocols(self) -> list[str]:
        return self.router.list_supported()

    def get_status(self) -> Dict[str, Any]:
        return {
            'active': self.scheduler.active_count(),
            'queued': self.scheduler.queued_count(),
            'completed': self.scheduler.completed_count(),
            'failed': self.scheduler.failed_count(),
            'protocols': self.list_protocols(),
            'auto_optimize': self.auto_optimize,
            'auto_mirror': self.auto_mirror,
        }

    def cancel(self, task_id: str) -> bool:
        return self.scheduler.cancel(task_id)

    def pause(self, task_id: str) -> bool:
        return self.scheduler.pause(task_id)

    def resume(self, task_id: str) -> bool:
        return self.scheduler.resume(task_id)
