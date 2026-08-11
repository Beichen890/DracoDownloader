"""
HTTP/HTTPS 协议驱动
"""

import asyncio
import aiohttp
import os
import math
import json
import time
import re
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Callable

from .base import ProtocolDriver, DownloadHandle
from ..optimizer import (
    DownloadOptimizer, OptimalParams, NetworkProfile, AdaptiveSpeedupController,
)

# === 常量定义 ===
_DEFAULT_MAX_CONNECTIONS = 64
_DEFAULT_CHUNK_SIZE = 1024 * 1024        # 1 MiB
_HTTP_TIMEOUT_TOTAL = 300                 # 5 min
_HTTP_TIMEOUT_CONNECT = 30                # 30 s
_MERGE_BUFFER_SIZE = 16 * 1024 * 1024     # 16 MiB 合并缓冲区
_CHUNK_RETRIES = 5                        # 分片下载最大重试次数
_CHUNK_RETRY_BACKOFF = 2                  # 重试退避指数基数
_SINGLE_FILE_THRESHOLD_FACTOR = 2         # 单线程阈值 = chunk_size * 2
_PROGRESS_EMIT_INTERVAL = 0.05            # 进度回调最小间隔（秒）
_SPEED_WINDOW_SECONDS = 2.0               # 速度计算滑动窗口（秒）


class _SpeedTracker:
    """基于滑动窗口的下载速度计算器（线程安全）"""

    def __init__(self, window_seconds: float = _SPEED_WINDOW_SECONDS):
        self._window = window_seconds
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def add(self, downloaded: int):
        """记录已下载字节数样本"""
        now = time.time()
        with self._lock:
            cutoff = now - self._window
            self._samples = [s for s in self._samples if s[0] > cutoff]
            self._samples.append((now, downloaded))

    def get_speed(self) -> int:
        """获取当前速度（bytes/s）"""
        with self._lock:
            if len(self._samples) < 2:
                return 0
            first_time, first_bytes = self._samples[0]
            last_time, last_bytes = self._samples[-1]
            elapsed = last_time - first_time
            if elapsed <= 0:
                return 0
            return max(0, int((last_bytes - first_bytes) / elapsed))


class _ProgressReporter:
    """汇总各分片进度并触发回调，避免频繁 glob/stat"""

    def __init__(self, total_size: int,
                 callback: Optional[Callable[[int, int, int], None]],
                 speed_tracker: _SpeedTracker,
                 min_interval: float = _PROGRESS_EMIT_INTERVAL):
        self._total = total_size
        self._callback = callback
        self._speed = speed_tracker
        self._min_interval = min_interval
        self._chunk_progress: Dict[int, int] = {}
        self._last_emit = 0.0

    def update(self, idx: int, downloaded: int):
        """更新指定分片的进度"""
        self._chunk_progress[idx] = downloaded
        now = time.time()
        if now - self._last_emit < self._min_interval:
            return
        self._last_emit = now
        total_downloaded = sum(self._chunk_progress.values())
        self._speed.add(total_downloaded)
        speed = self._speed.get_speed()
        if self._callback:
            self._callback(total_downloaded, self._total, speed)


def _make_threadsafe_callback(
    loop: asyncio.AbstractEventLoop,
    callback: Optional[Callable[[int, int, int], None]]
) -> Optional[Callable[[int, int, int], None]]:
    """将回调包装为线程安全版本，供合并阶段在 executor 中调用"""
    if callback is None:
        return None

    def wrapper(downloaded: int, total: int, speed: int):
        loop.call_soon_threadsafe(callback, downloaded, total, speed)

    return wrapper


class HTTPDriver(ProtocolDriver):
    """HTTP/HTTPS 下载驱动 - 异步多分片"""

    def __init__(self, max_connections: int = _DEFAULT_MAX_CONNECTIONS,
                 chunk_size: int = _DEFAULT_CHUNK_SIZE,
                 auto_optimize: bool = True,
                 optimizer: Optional[DownloadOptimizer] = None,
                 adaptive: bool = True):
        self.max_connections = max_connections
        self.chunk_size = chunk_size
        self.auto_optimize = auto_optimize
        self._optimizer = optimizer
        self._optimal_params: Optional[OptimalParams] = None
        self._network_profile: Optional[NetworkProfile] = None
        # 运行时自适应加速（免合并 + 动态分裂最慢分片）
        self.adaptive = adaptive
        self._adaptive_controller: Optional[AdaptiveSpeedupController] = None
        self.timeout = aiohttp.ClientTimeout(
            total=_HTTP_TIMEOUT_TOTAL,
            connect=_HTTP_TIMEOUT_CONNECT
        )

    def match(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    async def probe(self, url: str) -> Dict[str, Any]:
        """探测文件信息 (HEAD 请求)"""
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.head(url) as resp:
                if resp.status not in (200, 302, 301):
                    if resp.status == 404:
                        raise FileNotFoundError(f"HTTP 404: {url}")
                    raise ConnectionError(f"HTTP {resp.status} for {url}")

                size = int(resp.headers.get('content-length', 0))
                accepts_ranges = resp.headers.get('accept-ranges', '') == 'bytes'
                content_type = resp.headers.get('content-type', '')
                filename = self._extract_filename(url, resp.headers)

            # 如果大小未知，尝试 Range 探测
            if size == 0:
                async with session.get(url, headers={'Range': 'bytes=0-1'}) as resp:
                    if resp.status == 206:
                        size = self._parse_content_range(resp.headers.get('content-range', ''))
                        accepts_ranges = True

            # 自动优化：使用真实文件大小和网络条件计算最优参数
            if self.auto_optimize and size > 0:
                try:
                    params, profile = await self._auto_optimize_params(url, size, accepts_ranges)
                    self._optimal_params = params
                    self._network_profile = profile
                except Exception as e:
                    # 优化失败不影响主流程
                    pass

            return {
                'size': size,
                'supports_range': accepts_ranges,
                'filename': filename,
                'content_type': content_type,
                'url': url
            }

    async def _auto_optimize_params(self, url: str, size: int,
                                     supports_range: bool) -> tuple:
        """自动计算最优参数"""
        if self._optimizer is None:
            from ..optimizer import DownloadOptimizer
            self._optimizer = DownloadOptimizer()

        # 传入已知信息作为网络画像基础
        profile = NetworkProfile(supports_range=supports_range)

        # 执行完整优化（内部会进行带宽探测）
        params = await self._optimizer.optimize_for_url(url, size, profile)

        return params, self._optimizer.get_last_profile()

    async def download(self, handle: DownloadHandle,
                       callback: Optional[Callable[[int, int, int], None]] = None):
        """执行下载"""
        output_path = Path(handle.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        total_size = handle.total_size
        supports_range = handle.metadata.get('supports_range', False)

        if output_path.exists() and output_path.stat().st_size == total_size and total_size > 0:
            if callback:
                callback(total_size, total_size, 0)
            return

        use_optimized = False
        opt_shards = None
        opt_connections = None
        opt_chunk_size = None

        if self._optimal_params is not None and supports_range and total_size > 0:
            opt_shards = self._optimal_params.shard_count
            opt_connections = max(
                self._optimal_params.max_connections,
                self._optimal_params.thread_count
            )
            opt_chunk_size = self._optimal_params.chunk_size
            use_optimized = True

        if total_size <= 0 or not supports_range or total_size < self.chunk_size * _SINGLE_FILE_THRESHOLD_FACTOR:
            await self._download_single(handle, callback)
            return

        # 优先尝试自适应路径（免合并 + 运行时分裂最慢分片）
        # 失败则回退到原"分片落盘 + 合并"路径
        if self.adaptive and supports_range and total_size > 0:
            try:
                await self._download_adaptive(handle, callback,
                                              opt_shards, opt_connections,
                                              opt_chunk_size, use_optimized)
                return
            except Exception as e:
                log.warning(f"自适应下载失败，回退到合并路径: {e}")

        if use_optimized:
            chunk_count = opt_shards or self._calculate_chunks(total_size)
            chunk_size = opt_chunk_size or math.ceil(total_size / chunk_count)
            max_conn = opt_connections or self.max_connections
        else:
            chunk_count = self._calculate_chunks(total_size)
            chunk_size = math.ceil(total_size / chunk_count)
            max_conn = self.max_connections

        progress_file = Path(f"{output_path}.progress")
        chunks_done = self._load_progress(progress_file, chunk_count)

        temp_dir = output_path.parent / f".{output_path.name}.parts"
        temp_dir.mkdir(exist_ok=True)

        speed_tracker = _SpeedTracker()
        reporter = _ProgressReporter(total_size, callback, speed_tracker)

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                semaphore = asyncio.Semaphore(max_conn)
                tasks = []
                completed = sum(1 for d in chunks_done if d)

                for i in range(chunk_count):
                    chunk_file = temp_dir / f"part_{i:08d}"
                    if chunks_done[i]:
                        if chunk_file.exists():
                            reporter.update(i, chunk_file.stat().st_size)
                        continue
                    start = i * chunk_size
                    end = min(start + chunk_size - 1, total_size - 1)
                    tasks.append(self._download_chunk(
                        handle.url, start, end, temp_dir, i,
                        semaphore, session, reporter, total_size
                    ))

                if tasks:
                    await asyncio.gather(*tasks)

            completed = 0
            for i in range(chunk_count):
                chunk_file = temp_dir / f"part_{i:08d}"
                if chunk_file.exists() and chunk_file.stat().st_size > 0:
                    completed += 1

            if completed == chunk_count:
                await self._merge_chunks(temp_dir, output_path, chunk_count, callback, total_size)
                progress_file.unlink(missing_ok=True)
                self._cleanup_temp_dir(temp_dir)
                if callback:
                    callback(total_size, total_size, 0)
            else:
                self._save_progress(progress_file, [
                    chunk_file.exists() for chunk_file in temp_dir.glob("part_*")
                ])
        except Exception:
            self._cleanup_temp_dir(temp_dir)
            raise

    async def _download_adaptive(self, handle: DownloadHandle,
                                 callback: Optional[Callable[[int, int, int], None]],
                                 opt_shards: Optional[int],
                                 opt_connections: Optional[int],
                                 opt_chunk_size: Optional[int],
                                 use_optimized: bool) -> None:
        """自适应下载路径：免合并定位写 + 运行时分裂最慢分片

        与传统"分片落临时文件 → 合并"不同，本路径：
        1. 预分配目标文件（ftruncate）
        2. 各 worker 用 os.pwrite 直接写最终文件的对应偏移，零合并
        3. AdaptiveSpeedupController 监控速度，速度稳定时把最慢分片的
           剩余区间一分为二，新增 worker 接管后半段

        断点续传：用 .progress 文件记录每个分片的已写字节，恢复时跳过已写部分。
        """
        output_path = Path(handle.output_path)
        total_size = handle.total_size
        url = handle.url

        chunk_count = opt_shards or self._calculate_chunks(total_size)
        chunk_size = opt_chunk_size or math.ceil(total_size / chunk_count)
        max_conn = opt_connections or self.max_connections

        # 预分配文件
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(output_path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                os.ftruncate(fd, total_size)
            except OSError as e:
                log.warning(f"预分配失败，降级为稀疏写: {e}")
        except Exception:
            os.close(fd)
            raise

        # 分片区间表（闭区间 [start, end]）
        # 初始等分；运行时由控制器分裂
        ranges: list[list[int]] = []  # [start, end, downloaded]
        progress_file = Path(f"{output_path}.progress")
        saved = self._load_adaptive_progress(progress_file, chunk_count)
        for i in range(chunk_count):
            start = i * chunk_size
            end = min(start + chunk_size - 1, total_size - 1)
            ranges.append([start, end, saved[i] if i < len(saved) else 0])

        speed_tracker = _SpeedTracker()
        reporter = _ProgressReporter(total_size, callback, speed_tracker)

        if self._adaptive_controller is None:
            self._adaptive_controller = AdaptiveSpeedupController()
        else:
            self._adaptive_controller.reset()

        # 共享状态
        state = {
            'ranges': ranges,
            'fd': fd,
            'url': url,
            'semaphore': asyncio.Semaphore(max_conn),
            'reporter': reporter,
            'speed_tracker': speed_tracker,
            'active_workers': 0,
            'lock': asyncio.Lock(),
            'session': None,
            'progress_path': str(progress_file),
            'all_tasks': set(),  # 所有 worker task（含分裂产生的）
            'done': False,       # 主流程是否已结束（阻止 monitor 继续分裂）
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                state['session'] = session
                # 初始 worker：每个分片一个（跳过已完成的）
                for i, r in enumerate(ranges):
                    if r[2] >= (r[1] - r[0] + 1):
                        reporter.update(i, r[1] - r[0] + 1)
                        continue
                    t = asyncio.create_task(
                        self._adaptive_worker(i, state, session)
                    )
                    state['all_tasks'].add(t)
                    t.add_done_callback(state['all_tasks'].discard)

                # 监控任务：定期喂速度采样给控制器，按需分裂最慢分片
                monitor = asyncio.create_task(
                    self._adaptive_monitor(state, callback)
                )

                # 等待所有 worker 完成（含分裂产生的）
                while state['all_tasks']:
                    await asyncio.gather(*state['all_tasks'],
                                         return_exceptions=True)

                state['done'] = True
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

            # 校验完整性
            downloaded_total = sum(r[2] for r in ranges)
            if downloaded_total < total_size:
                raise RuntimeError(
                    f"自适应下载不完整: {downloaded_total}/{total_size} bytes"
                )

            # 保存断点已无意义，删除
            progress_file.unlink(missing_ok=True)
            if callback:
                callback(total_size, total_size, 0)

            # 反馈实际速度给优化器（修正后续同站优化）
            if self._optimizer is not None and self._network_profile is not None:
                speed_bps = speed_tracker.get_speed()
                if speed_bps > 0:
                    self._optimizer.record_actual(
                        url, self._network_profile,
                        (speed_bps * 8) / 1_000_000, total_size
                    )
        finally:
            os.close(fd)

    async def _adaptive_worker(self, idx: int, state: dict,
                               session: aiohttp.ClientSession) -> None:
        """单个自适应分片下载 worker

        下载 ranges[idx] 对应的区间，用 os.pwrite 定位写入。
        """
        ranges = state['ranges']
        fd = state['fd']
        url = state['url']
        semaphore = state['semaphore']
        reporter = state['reporter']
        chunk_size = self.chunk_size

        async with semaphore:
            async with state['lock']:
                state['active_workers'] += 1

            try:
                while True:
                    async with state['lock']:
                        r = ranges[idx]
                        start, end, downloaded = r[0], r[1], r[2]
                        remaining = end - start + 1 - downloaded
                        if remaining <= 0:
                            break
                        http_start = start + downloaded
                        http_end = end

                    headers = {'Range': f'bytes={http_start}-{http_end}'}
                    try:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status not in (200, 206):
                                if resp.status == 416:
                                    async with state['lock']:
                                        ranges[idx][2] = end - start + 1
                                        reporter.update(idx, ranges[idx][2])
                                    return
                                raise RuntimeError(f"HTTP {resp.status}")
                            offset = http_start
                            async for data in resp.content.iter_chunked(chunk_size):
                                if not data:
                                    continue
                                # 定位写：直接写最终文件偏移，零合并
                                os.pwrite(fd, data, offset)
                                offset += len(data)
                                async with state['lock']:
                                    ranges[idx][2] += len(data)
                                    reporter.update(idx, ranges[idx][2])
                                    state['speed_tracker'].add(
                                        sum(rr[2] for rr in ranges)
                                    )
                    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                        log.debug(f"分片 {idx} 错误，重试: {e}")
                        await asyncio.sleep(1)
                        continue
            finally:
                async with state['lock']:
                    state['active_workers'] -= 1

    async def _adaptive_monitor(self, state: dict,
                                callback: Optional[Callable[[int, int, int], None]]) -> None:
        """监控任务：定期采样速度，速度稳定时分裂最慢分片

        每 1 秒喂一次速度采样给 AdaptiveSpeedupController。
        当控制器要求分裂时，选剩余字节最多的分片，把它的 [start, end]
        一分为二，缩小原分片 end，新增一个 worker 接管后半段。
        """
        ranges = state['ranges']
        reporter = state['reporter']
        controller = self._adaptive_controller
        if controller is None:
            return

        last_save = 0.0
        while not state.get('done', False):
            await asyncio.sleep(1.0)
            loop_time = asyncio.get_event_loop().time()
            speed = state['speed_tracker'].get_speed()

            async with state['lock']:
                active = state['active_workers']
                # 选剩余字节最多的分片作为分裂候选
                candidates = []
                for i, r in enumerate(ranges):
                    remaining = r[1] - r[0] + 1 - r[2]
                    if remaining > controller.min_split_bytes:
                        candidates.append((remaining, i))
                candidates.sort(reverse=True)

            split_count = 0
            # 首次触发时 controller 通过 feed 返回（当前实现返回空列表，
            # 由本方法直接选候选）；这里统一用候选数限制
            should_split = controller.feed(speed, active, loop_time)
            # controller.feed 首次触发后 _observe_started_at != 0，
            # 我们在首次触发时主动分裂 split_on_trigger 个
            if (controller._observe_started_at != 0
                    and controller._baseline_workers == active
                    and not getattr(controller, '_initial_split_done', False)):
                split_count = min(controller.split_on_trigger,
                                  len(candidates),
                                  controller.max_workers - active)
                controller._initial_split_done = True
            elif should_split:
                split_count = len(should_split)

            for _ in range(split_count):
                if not candidates:
                    break
                _, idx = candidates.pop(0)
                async with state['lock']:
                    r = ranges[idx]
                    start, end, downloaded = r[0], r[1], r[2]
                    # 只分裂未下载部分的尾部
                    split_at = AdaptiveSpeedupController.split_range(
                        start + downloaded, end
                    )
                    if split_at is None:
                        continue
                    new_start, new_end = split_at
                    # 缩小原分片 end 到分裂点前
                    ranges[idx][1] = new_start - 1
                    # 新增分片
                    new_idx = len(ranges)
                    ranges.append([new_start, new_end, 0])
                    log.debug(f"分裂分片 {idx} → 新分片 {new_idx} "
                              f"[{new_start},{new_end}]")
                # 启动新 worker（需重新获取 session）
                # 这里复用 state 中没有 session，简化为创建新任务由调用方 session 池
                # 实际通过外部 session 引用启动
                state.setdefault('pending_spawns', []).append(new_idx)

            # 处理待启动的 worker
            spawns = state.pop('pending_spawns', []) if 'pending_spawns' in state else []
            session = state.get('session')
            for new_idx in spawns:
                if session is not None and not state.get('done', False):
                    t = asyncio.create_task(
                        self._adaptive_worker(new_idx, state, session)
                    )
                    state['all_tasks'].add(t)
                    t.add_done_callback(state['all_tasks'].discard)

            # 定期保存断点
            now = asyncio.get_event_loop().time()
            if now - last_save > 2.0:
                async with state['lock']:
                    self._save_adaptive_progress(
                        Path(state['progress_path']), ranges
                    )
                last_save = now

    def _load_adaptive_progress(self, path: Path, count: int) -> list[int]:
        """加载自适应断点（每分片已写字节数）"""
        if not path.exists():
            return [0] * count
        try:
            with open(path) as f:
                data = json.load(f)
            saved = data.get('adaptive_bytes', [])
            return saved[:count] + [0] * max(0, count - len(saved))
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            return [0] * count

    def _save_adaptive_progress(self, path: Path,
                                ranges: list[list[int]]) -> None:
        """保存自适应断点（每分片已写字节数 + 区间）"""
        try:
            with open(path, 'w') as f:
                json.dump({
                    'adaptive_bytes': [r[2] for r in ranges],
                    'ranges': [[r[0], r[1]] for r in ranges],
                    'updated': time.time(),
                }, f)
        except OSError:
            pass

    def _cleanup_temp_dir(self, temp_dir: Path):
        """清理临时目录"""
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    async def _download_chunk(self, url: str, start: int, end: int,
                              temp_dir: Path, idx: int,
                              semaphore: asyncio.Semaphore,
                              session: aiohttp.ClientSession,
                              reporter: '_ProgressReporter',
                              total_size: int):
        """下载单个分片"""
        chunk_file = temp_dir / f"part_{idx:08d}"
        chunk_total = end - start + 1

        # 检查是否已完整下载
        if chunk_file.exists() and chunk_file.stat().st_size >= chunk_total:
            reporter.update(idx, chunk_file.stat().st_size)
            return

        for attempt in range(_CHUNK_RETRIES):
            try:
                downloaded = 0
                async with semaphore:
                    headers = {'Range': f'bytes={start}-{end}'}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status not in (200, 206):
                            if resp.status == 416:
                                # Range not satisfiable → 已完整下载
                                return
                            raise RuntimeError(f"HTTP {resp.status}")

                        with open(chunk_file, 'wb') as f:
                            async for data in resp.content.iter_chunked(self.chunk_size):
                                f.write(data)
                                downloaded += len(data)
                                reporter.update(idx, downloaded)
                return
            except Exception as e:
                if attempt == _CHUNK_RETRIES - 1:
                    raise
                await asyncio.sleep(_CHUNK_RETRY_BACKOFF ** attempt)

    async def _download_single(self, handle: DownloadHandle,
                               callback: Optional[Callable[[int, int, int], None]] = None):
        """单线程下载"""
        output_path = Path(handle.output_path)
        total_size = handle.total_size

        speed_tracker = _SpeedTracker()
        last_emit = 0.0

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(handle.url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                downloaded = 0
                with open(output_path, 'wb') as f:
                    async for data in resp.content.iter_chunked(self.chunk_size):
                        f.write(data)
                        downloaded += len(data)
                        now = time.time()
                        if now - last_emit >= _PROGRESS_EMIT_INTERVAL:
                            last_emit = now
                            speed_tracker.add(downloaded)
                            if callback:
                                callback(downloaded, total_size or downloaded,
                                         speed_tracker.get_speed())
                # 确保最终进度到达 100%
                if callback:
                    callback(downloaded, total_size or downloaded, 0)

    async def resume(self, handle: DownloadHandle,
                     callback: Optional[Callable[[int, int, int], None]] = None):
        """断点续传"""
        await self.download(handle, callback)

    async def _merge_chunks(self, temp_dir: Path, output_path: Path, count: int,
                            callback: Optional[Callable[[int, int, int], None]],
                            total_size: int):
        """合并分片（在线程池中执行，避免阻塞事件循环）"""
        loop = asyncio.get_running_loop()
        thread_callback = _make_threadsafe_callback(loop, callback)
        speed_tracker = _SpeedTracker()

        def do_merge():
            merged = 0
            with open(output_path, 'wb') as out:
                for i in range(count):
                    chunk_file = temp_dir / f"part_{i:08d}"
                    if not chunk_file.exists():
                        raise RuntimeError(f"分片文件缺失: {chunk_file}")
                    
                    chunk_size = chunk_file.stat().st_size
                    if chunk_size == 0:
                        raise RuntimeError(f"分片文件为空: {chunk_file}")
                    
                    with open(chunk_file, 'rb') as f:
                        while True:
                            data = f.read(_MERGE_BUFFER_SIZE)
                            if not data:
                                break
                            out.write(data)
                            merged += len(data)
                            speed_tracker.add(merged)
                            if thread_callback:
                                thread_callback(total_size, total_size,
                                                speed_tracker.get_speed())
                    chunk_file.unlink()
            
            if total_size > 0 and merged != total_size:
                raise RuntimeError(f"合并后的文件大小不匹配: 期望 {total_size}, 实际 {merged}")

        await asyncio.to_thread(do_merge)

    def _calculate_chunks(self, total_size: int) -> int:
        """动态计算分片数（原始算法，自动优化时被覆盖）"""
        if total_size < 10 * 1024 * 1024:
            return 4
        if total_size < 100 * 1024 * 1024:
            return 8
        if total_size < 500 * 1024 * 1024:
            return 16
        if total_size < 2 * 1024 * 1024 * 1024:
            return 32
        return 64

    def _load_progress(self, path: Path, count: int) -> list[bool]:
        if not path.exists():
            return [False] * count
        try:
            with open(path) as f:
                data = json.load(f)
            return data.get('done', [False] * count)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            return [False] * count

    def _save_progress(self, path: Path, done: list):
        with open(path, 'w') as f:
            json.dump({'done': done, 'updated': time.time()}, f)

    def _extract_filename(self, url: str, headers) -> str:
        cd = headers.get('content-disposition', '')
        if 'filename=' in cd:
            match = re.search(r'filename=([^;]+)', cd)
            if match:
                return match.group(1).strip('"\'')
        return url.split('/')[-1].split('?')[0] or 'download'

    def _parse_content_range(self, cr: str) -> int:
        if '/' in cr:
            return int(cr.split('/')[-1])
        return 0

    def get_optimization_info(self) -> Optional[Dict[str, Any]]:
        """获取优化信息（用于展示）"""
        if self._optimal_params is None:
            return None
        return {
            'shard_count': self._optimal_params.shard_count,
            'thread_count': self._optimal_params.thread_count,
            'chunk_size': self._optimal_params.chunk_size,
            'max_connections': self._optimal_params.max_connections,
            'estimated_speed_mbps': self._optimal_params.estimated_speed_mbps,
            'rationale': self._optimal_params.rationale,
        }
