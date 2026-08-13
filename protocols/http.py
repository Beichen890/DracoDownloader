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
from ..logger import get_logger

log = get_logger('http')

# === 常量定义 ===
_DEFAULT_MAX_CONNECTIONS = 64
_DEFAULT_CHUNK_SIZE = 1024 * 1024        # 1 MiB
_HTTP_TIMEOUT_TOTAL = 300                 # 5 min
_HTTP_TIMEOUT_CONNECT = 30                # 30 s
_HTTP_TIMEOUT_SOCK_READ = 15              # 15s 单次读取间隔超时（防完全卡死）
_MERGE_BUFFER_SIZE = 16 * 1024 * 1024     # 16 MiB 合并缓冲区
_CHUNK_RETRIES = 5                        # 分片下载最大重试次数
_CHUNK_RETRY_BACKOFF = 2                  # 重试退避指数基数
_SINGLE_FILE_THRESHOLD_FACTOR = 2         # 单线程阈值 = chunk_size * 2
_PROGRESS_EMIT_INTERVAL = 0.05            # 进度回调最小间隔（秒）
_SPEED_WINDOW_SECONDS = 2.0               # 速度计算滑动窗口（秒）

# === 慢速 trickle 检测（速率守卫） ===
# sock_read 只能抓"完全无数据 N 秒"，抓不到"持续发送少量数据"的 trickle。
# 真实场景：CDN 某条路由变慢，服务器以 9 KB/s 持续吐字节，sock_read 永不触发，
# 但实际下载要卡 250s+。这里在应用层加滑动窗口速率检测。
_SLOW_SHARD_WINDOW_S = 10.0               # 速率检测窗口（秒）
_SLOW_SHARD_MIN_BPS = 200_000             # 最低可接受速度 200 Kbps ≈ 25 KB/s
_SLOW_SHARD_GRACE_S = 5.0                 # 启动 grace 期（TCP slow start、首包延迟）

# 分片下载专用 timeout：sock_read 防止服务器 hold 连接 trickle 数据
# 导致长尾分片卡满 total timeout（如 19 分片 18 个秒完、最后 1 个卡 300s）
_CHUNK_TIMEOUT = aiohttp.ClientTimeout(
    total=_HTTP_TIMEOUT_TOTAL,
    connect=_HTTP_TIMEOUT_CONNECT,
    sock_read=_HTTP_TIMEOUT_SOCK_READ,
)


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
        # 反爬 HttpClient（由 core 注入，None 时回退到普通 aiohttp session）
        # 注入后：所有请求自动带真实浏览器 UA、cookie 复用、proxy 生效、Referer 自动注入
        self._http_client = None

    def _get_session(self) -> aiohttp.ClientSession:
        """获取 HTTP session

        优先用注入的 HttpClient 的持久 session（带 UA/cookie/proxy/Referer），
        否则回退到临时 aiohttp.ClientSession（向后兼容）。
        """
        if self._http_client is not None and self._http_client._session is not None:
            return self._http_client.session
        # 回退：临时 session（无 UA/cookie/proxy）
        return aiohttp.ClientSession(timeout=self.timeout)

    def _is_shared_session(self) -> bool:
        """是否使用共享 HttpClient session（决定是否需要 close）"""
        return (self._http_client is not None
                and self._http_client._session is not None)

    def _merge_headers(self, extra: Optional[Dict[str, str]] = None,
                       handle_headers: Optional[Dict[str, str]] = None
                       ) -> Dict[str, str]:
        """合并请求头：handle.headers + 用户 extra

        注：UA/cookie/proxy 由 HttpClient 统一管理，这里不重复设置。
        """
        headers = {}
        if handle_headers:
            headers.update(handle_headers)
        if extra:
            headers.update(extra)
        return headers

    def match(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    async def probe(self, url: str) -> Dict[str, Any]:
        """探测文件信息 (HEAD 请求)"""
        session = self._get_session()
        shared = self._is_shared_session()
        try:
            # 共享 session 用 HttpClient 的 proxy；临时 session 无 proxy
            proxy = self._http_client._proxy if shared else None
            async with session.head(url, proxy=proxy) as resp:
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
                async with session.get(url, headers={'Range': 'bytes=0-1'},
                                       proxy=proxy) as resp:
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
        finally:
            if not shared:
                await session.close()

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
            session = self._get_session()
            shared = self._is_shared_session()
            try:
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
                        semaphore, session, reporter, total_size,
                        handle_headers=handle.headers
                    ))

                if tasks:
                    await asyncio.gather(*tasks)
            finally:
                if not shared:
                    await session.close()

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
            'active_ranges': set(),  # 有 worker 在跑的分片 idx（检测孤儿）
            'lock': asyncio.Lock(),
            'session': None,
            'progress_path': str(progress_file),
            'all_tasks': set(),  # 所有 worker task（含分裂产生的）
            'done': False,       # 主流程是否已结束（阻止 monitor 继续分裂）
            'split_event': asyncio.Event(),  # worker 完成/退出信号（事件驱动分裂）
        }

        try:
            session = self._get_session()
            shared = self._is_shared_session()
            try:
                state['session'] = session
                # 初始 worker：每个分片一个（跳过已完成的）
                for i, r in enumerate(ranges):
                    if r[2] >= (r[1] - r[0] + 1):
                        reporter.update(i, r[1] - r[0] + 1)
                        continue
                    state['active_ranges'].add(i)
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
            finally:
                if not shared:
                    await session.close()

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

            consecutive_stalls = 0
            _MAX_STALL_RETRIES = 5
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
                        async with session.get(url, headers=headers,
                                               timeout=_CHUNK_TIMEOUT) as resp:
                            if resp.status not in (200, 206):
                                if resp.status == 416:
                                    async with state['lock']:
                                        ranges[idx][2] = end - start + 1
                                        reporter.update(idx, ranges[idx][2])
                                    return
                                raise RuntimeError(f"HTTP {resp.status}")
                            offset = http_start
                            async for data in self._iter_with_rate_guard(
                                resp.content, chunk_size, idx
                            ):
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
                            # 本次请求成功完成，重置 stall 计数
                            consecutive_stalls = 0
                    except asyncio.TimeoutError as e:
                        # sock_read 超时或速率守卫触发：trickle/卡死
                        consecutive_stalls += 1
                        log.debug(f"分片 {idx} 超时（sock_read/速率守卫），"
                                  f"第 {consecutive_stalls}/{_MAX_STALL_RETRIES} 次: {e}")
                        if consecutive_stalls >= _MAX_STALL_RETRIES:
                            # 持续 stall：剩余区间交给 monitor 分裂接管，本 worker 退出
                            log.warning(f"分片 {idx} 连续 {_MAX_STALL_RETRIES} 次 "
                                        f"读取超时，放弃由分裂接管")
                            return
                        await asyncio.sleep(1)
                        continue
                    except (ConnectionError, OSError) as e:
                        log.debug(f"分片 {idx} 错误，重试: {e}")
                        await asyncio.sleep(1)
                        continue
            finally:
                async with state['lock']:
                    state['active_workers'] -= 1
                    state['active_ranges'].discard(idx)
                # 事件驱动：通知 monitor 立即检查分裂/孤儿接管
                # （worker 完成或 stall 退出都会走这里，monitor 响应延迟从 ~1s 降到 ~0）
                state['split_event'].set()

    def _spawn_split_worker(self, state: dict, idx: int) -> bool:
        """分裂指定分片并启动新 worker（同步方法，内部无 await）

        把 ranges[idx] 的未下载尾部一分为二，缩小原分片 end，
        新增一个分片并启动 worker 接管后半段。

        Returns:
            True 如果分裂并启动了新 worker；False 如果不可分裂（剩余太小等）
        """
        ranges = state['ranges']
        r = ranges[idx]
        start, end, downloaded = r[0], r[1], r[2]
        split_at = AdaptiveSpeedupController.split_range(
            start + downloaded, end
        )
        if split_at is None:
            return False
        new_start, new_end = split_at
        ranges[idx][1] = new_start - 1
        new_idx = len(ranges)
        ranges.append([new_start, new_end, 0])
        log.debug(f"分裂分片 {idx} → 新分片 {new_idx} "
                  f"[{new_start},{new_end}]")
        session = state.get('session')
        if session is None or state.get('done', False):
            return False
        state['active_ranges'].add(new_idx)
        t = asyncio.create_task(
            self._adaptive_worker(new_idx, state, session)
        )
        state['all_tasks'].add(t)
        t.add_done_callback(state['all_tasks'].discard)
        return True

    async def _adaptive_monitor(self, state: dict,
                                callback: Optional[Callable[[int, int, int], None]]) -> None:
        """监控任务：事件驱动分裂 + 定时速度采样

        响应模式（二选一触发）：
        1. 事件驱动：worker 完成/退出时 set split_event，monitor 立即醒
           → 检查孤儿接管 + 尝试分裂最慢分片（响应延迟 ~0 vs 原 ~1s）
        2. 定时兜底：1s 超时无事件 → 速度采样喂 controller + 分裂判定

        速度采样按 1s 节奏喂 AdaptiveSpeedupController（避免事件频繁触发采样失真）。
        事件触发的分裂受 controller.enabled 控制，不绕过收益判定。
        """
        ranges = state['ranges']
        controller = self._adaptive_controller
        if controller is None:
            return

        split_event = state['split_event']
        last_save = 0.0
        last_feed = 0.0

        while not state.get('done', False):
            # 等待事件或 1s 超时（事件驱动优先，定时兜底）
            try:
                await asyncio.wait_for(split_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            split_event.clear()

            loop_time = asyncio.get_event_loop().time()
            speed = state['speed_tracker'].get_speed()

            async with state['lock']:
                active = state['active_workers']
                active_ranges = set(state['active_ranges'])
                # 检测孤儿分片：有剩余字节但无 worker 在跑（stall 退出后遗留）
                orphans = [i for i, r in enumerate(ranges)
                           if r[1] - r[0] + 1 - r[2] > 0 and i not in active_ranges]
                # 选剩余字节最多的分片作为分裂候选
                candidates = [(r[1] - r[0] + 1 - r[2], i)
                              for i, r in enumerate(ranges)
                              if r[1] - r[0] + 1 - r[2] > controller.min_split_bytes]
                candidates.sort(reverse=True)

            # 接管孤儿分片：stall worker 退出后遗留的未完成区间
            session = state.get('session')
            for orphan_idx in orphans:
                if session is not None and not state.get('done', False):
                    async with state['lock']:
                        if orphan_idx in state['active_ranges']:
                            continue  # 已被其他循环接管
                        state['active_ranges'].add(orphan_idx)
                    log.debug(f"接管孤儿分片 {orphan_idx}")
                    t = asyncio.create_task(
                        self._adaptive_worker(orphan_idx, state, session)
                    )
                    state['all_tasks'].add(t)
                    t.add_done_callback(state['all_tasks'].discard)

            # 速度采样按 1s 节奏喂 controller（避免事件频繁触发采样失真）
            should_feed = loop_time - last_feed >= 1.0
            if should_feed:
                last_feed = loop_time
                should_split = controller.feed(speed, active, loop_time)
                # 首次触发：建立基线并分裂 split_on_trigger 个
                if (controller._observe_started_at != 0
                        and controller._baseline_workers == active
                        and not getattr(controller, '_initial_split_done', False)):
                    split_count = min(controller.split_on_trigger,
                                      len(candidates),
                                      controller.max_workers - active)
                    controller._initial_split_done = True
                elif should_split:
                    split_count = len(should_split)
                else:
                    split_count = 0
            else:
                # 事件触发（非采样节奏）：事件驱动分裂 1 个最慢分片
                # 受 controller.enabled 控制，收益不达标时 controller 会 disabled
                if controller.enabled and active < controller.max_workers:
                    split_count = min(1, len(candidates),
                                      controller.max_workers - active)
                else:
                    split_count = 0

            for _ in range(split_count):
                if not candidates:
                    break
                _, idx = candidates.pop(0)
                self._spawn_split_worker(state, idx)

            # 定期保存断点（1s 间隔，崩溃最多丢 1s 进度）
            if loop_time - last_save > 1.0:
                async with state['lock']:
                    self._save_adaptive_progress(
                        Path(state['progress_path']), ranges
                    )
                last_save = loop_time

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

    async def _iter_with_rate_guard(
        self,
        content: aiohttp.StreamReader,
        chunk_size: int,
        shard_idx: int,
    ):
        """带速率守卫的响应内容迭代器

        sock_read 只能抓"完全无数据 N 秒"，抓不到 trickle：
        服务器持续发送少量数据（如 9 KB/s），每次 read 都在 sock_read 内返回，
        但整体速度极低，导致长尾分片卡 250s+。

        本方法在应用层加滑动窗口速率检测：
        - 启动 grace 期 _SLOW_SHARD_GRACE_S 秒内不检查（TCP slow start）
        - 之后每 _SLOW_SHARD_WINDOW_S 秒检查窗口内接收字节数
        - 低于 _SLOW_SHARD_MIN_BPS 对应字节数 → 抛 asyncio.TimeoutError
        - 单次 read 也用 asyncio.wait_for 限制，双重保险

        TimeoutError 由调用方捕获并触发重连/分裂。
        """
        now_start = time.time()
        grace_until = now_start + _SLOW_SHARD_GRACE_S
        window_start = now_start
        bytes_in_window = 0
        in_grace = True
        iterator = content.iter_chunked(chunk_size)
        while True:
            try:
                data = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=_SLOW_SHARD_WINDOW_S,
                )
            except StopAsyncIteration:
                break
            now = time.time()
            if in_grace:
                # grace 期内不检查（TCP slow start、首包延迟等正常慢启动）
                if now >= grace_until:
                    # grace 结束，重置窗口起点，丢弃 grace 期累积
                    in_grace = False
                    window_start = now
                    bytes_in_window = 0
                bytes_in_window += len(data)
                yield data
                continue
            bytes_in_window += len(data)
            elapsed = now - window_start
            if elapsed >= _SLOW_SHARD_WINDOW_S:
                min_bytes = (_SLOW_SHARD_MIN_BPS / 8) * elapsed
                if bytes_in_window < min_bytes:
                    actual_bps = (bytes_in_window * 8 / elapsed) if elapsed > 0 else 0
                    raise asyncio.TimeoutError(
                        f"分片 {shard_idx} 速率过低: "
                        f"{actual_bps:.0f} bps < 阈值 {_SLOW_SHARD_MIN_BPS} bps "
                        f"(window={elapsed:.1f}s, bytes={bytes_in_window})"
                    )
                # 窗口重置
                window_start = now
                bytes_in_window = 0
            yield data

    async def _download_chunk(self, url: str, start: int, end: int,
                              temp_dir: Path, idx: int,
                              semaphore: asyncio.Semaphore,
                              session: aiohttp.ClientSession,
                              reporter: '_ProgressReporter',
                              total_size: int,
                              handle_headers: Optional[Dict[str, str]] = None):
        """下载单个分片"""
        chunk_file = temp_dir / f"part_{idx:08d}"
        chunk_total = end - start + 1

        # 检查是否已完整下载
        if chunk_file.exists() and chunk_file.stat().st_size >= chunk_total:
            reporter.update(idx, chunk_file.stat().st_size)
            return

        proxy = self._http_client._proxy if self._is_shared_session() else None

        for attempt in range(_CHUNK_RETRIES):
            try:
                downloaded = 0
                async with semaphore:
                    headers = self._merge_headers(
                        {'Range': f'bytes={start}-{end}'}, handle_headers
                    )
                    async with session.get(url, headers=headers, proxy=proxy,
                                           timeout=_CHUNK_TIMEOUT) as resp:
                        if resp.status not in (200, 206):
                            if resp.status == 416:
                                # Range not satisfiable → 已完整下载
                                return
                            raise RuntimeError(f"HTTP {resp.status}")

                        with open(chunk_file, 'wb') as f:
                            async for data in self._iter_with_rate_guard(
                                resp.content, self.chunk_size, idx
                            ):
                                f.write(data)
                                downloaded += len(data)
                                reporter.update(idx, downloaded)
                return
            except asyncio.TimeoutError as e:
                # sock_read 超时或速率守卫触发：断开重连
                log.debug(f"分片 {idx} 超时（sock_read/速率守卫），重试: {e}")
                if attempt == _CHUNK_RETRIES - 1:
                    raise
                await asyncio.sleep(_CHUNK_RETRY_BACKOFF ** attempt)
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

        session = self._get_session()
        shared = self._is_shared_session()
        proxy = self._http_client._proxy if shared else None
        try:
            async with session.get(handle.url, headers=handle.headers or None,
                                   proxy=proxy) as resp:
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
        finally:
            if not shared:
                await session.close()

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
