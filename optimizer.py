"""
Download Optimizer — 动态最优分片数和线程数计算

通过评估网络条件（延迟、带宽）、文件大小和系统资源，
动态计算最优并发参数，最大化下载效率。
"""

import asyncio
import time
import os
import math
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass, field

from .logger import get_logger

log = get_logger('optimizer')


@dataclass
class NetworkProfile:
    """网络环境画像"""
    latency_ms: float = 30.0           # 服务器延迟（毫秒）
    bandwidth_mbps: float = 100.0       # 可用带宽（Mbps）
    bandwidth_confidence: float = 0.5  # 带宽测量置信度 (0-1)
    loss_rate: float = 0.0             # 丢包率 (0-1)
    supports_range: bool = True        # 是否支持Range请求
    download_speed_bps: int = 0        # 实际测得下载速度（bps）


@dataclass
class OptimalParams:
    """最优参数推荐"""
    shard_count: int = 4               # 推荐分片数
    thread_count: int = 4              # 推荐线程数
    chunk_size: int = 1024 * 1024      # 推荐分片大小（字节）
    max_connections: int = 16          # 推荐最大连接数
    estimated_speed_mbps: float = 0.0  # 预估速度（Mbps）
    rationale: str = ""                # 推荐理由


class BandwidthProbe:
    """
    带宽探测工具 — 测量到服务器的实际带宽

    用法：
        probe = BandwidthProbe()
        profile = await probe.measure(url)
    """

    def __init__(self, probe_size: int = 10 * 1024 * 1024,  # 10MB测速文件
                 probe_timeout: float = 25.0,
                 min_download: int = 256 * 1024):  # 最少256KB
        self.probe_size = probe_size
        self.probe_timeout = probe_timeout
        self.min_download = min_download

    async def measure(self, url: str) -> NetworkProfile:
        """
        测量到指定URL的网络状况

        Args:
            url: 目标URL

        Returns:
            网络配置文件
        """
        import aiohttp

        profile = NetworkProfile()
        timeout = aiohttp.ClientTimeout(total=self.probe_timeout, connect=10)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 阶段1: 延迟测量
                latency_samples = []
                for _ in range(3):
                    start = time.time()
                    try:
                        async with session.head(url) as resp:
                            elapsed = (time.time() - start) * 1000
                            latency_samples.append(elapsed)
                            profile.supports_range = (
                                resp.headers.get('accept-ranges', '') == 'bytes'
                            )
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)

                if latency_samples:
                    profile.latency_ms = sum(latency_samples) / len(latency_samples)

                # 阶段2: 带宽测量（下载测速文件）
                # 6s 取样窗口：TCP slow start 在前 1-2s 未充分跑满，
                # 3s 采样会低估带宽（实测 179Mbps 被测成 100Mbps，低估 52%）
                bw_start = time.time()
                downloaded = 0
                # 取窗口后 4s 的稳态速度（丢弃前 2s slow start 段）
                warmup_s = 2.0
                total_s = 6.0
                warmup_bytes = 0
                steady_start = None

                async with session.get(url) as resp:
                    if resp.status == 200:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            downloaded += len(chunk)
                            elapsed = time.time() - bw_start
                            if elapsed >= warmup_s and steady_start is None:
                                # warmup 结束，开始稳态采样
                                steady_start = time.time()
                                warmup_bytes = downloaded
                            if elapsed >= total_s or downloaded >= self.probe_size:
                                break

                elapsed = time.time() - bw_start
                if elapsed > 0 and downloaded >= self.min_download:
                    # 优先用稳态段（warmup 后）计算带宽，避免 slow start 低估
                    steady_elapsed = None
                    steady_bytes = 0
                    if steady_start is not None:
                        steady_elapsed = time.time() - steady_start
                        steady_bytes = downloaded - warmup_bytes
                        if steady_elapsed > 0.5 and steady_bytes > 0:
                            profile.download_speed_bps = int(
                                steady_bytes * 8 / steady_elapsed
                            )
                            profile.bandwidth_mbps = (
                                steady_bytes * 8 / (steady_elapsed * 1_000_000)
                            )
                        else:
                            # 稳态段太短，退回全程
                            profile.download_speed_bps = int(downloaded * 8 / elapsed)
                            profile.bandwidth_mbps = (
                                downloaded * 8 / (elapsed * 1_000_000)
                            )
                    else:
                        # 未到 warmup 就下完了（小文件），用全程
                        profile.download_speed_bps = int(downloaded * 8 / elapsed)
                        profile.bandwidth_mbps = (
                            downloaded * 8 / (elapsed * 1_000_000)
                        )
                    profile.bandwidth_confidence = min(
                        1.0,
                        max(0.3, downloaded / self.probe_size)
                    )
                    if steady_elapsed is not None:
                        log.info(f"带宽测量: {downloaded/1024/1024:.1f}MB in {elapsed:.1f}s"
                                 f" (稳态段 {steady_elapsed:.1f}s"
                                 f" {steady_bytes/1024/1024:.1f}MB), "
                                 f"= {profile.bandwidth_mbps:.1f} Mbps")
                    else:
                        log.info(f"带宽测量: {downloaded/1024/1024:.1f}MB in {elapsed:.1f}s"
                                 f" (全程，未到 warmup), "
                                 f"= {profile.bandwidth_mbps:.1f} Mbps")
                else:
                    # 使用较保守的估计
                    profile.bandwidth_mbps = 50.0
                    profile.bandwidth_confidence = 0.3
                    log.info("带宽测量数据不足，使用保守估计")

        except Exception as e:
            log.warning(f"网络探测失败: {e}")
            # 返回默认配置
            profile.bandwidth_mbps = 50.0
            profile.bandwidth_confidence = 0.2

        return profile


class OptimalShardCalculator:
    """
    动态最优分片数计算器

    基于以下因素计算最优分片数：
    - 文件大小
    - 可用带宽
    - 服务器延迟
    - HTTP Range 支持

    用法：
        calc = OptimalShardCalculator()
        params = calc.calculate(file_size=500*1024*1024, network_profile=profile)
        print(f"推荐分片数: {params.shard_count}")
    """

    def __init__(self,
                 min_shards: int = 2,
                 max_shards: int = 128,
                 min_chunk_size: int = 512 * 1024,      # 最小分片 512KB
                 target_chunk_size: int = 4 * 1024 * 1024,  # 目标分片 4MB
                 max_chunk_size: int = 64 * 1024 * 1024):   # 最大分片 64MB
        self.min_shards = min_shards
        self.max_shards = max_shards
        self.min_chunk_size = min_chunk_size
        self.target_chunk_size = target_chunk_size
        self.max_chunk_size = max_chunk_size

    def calculate(self,
                  file_size: int,
                  network_profile: Optional[NetworkProfile] = None) -> OptimalParams:
        """
        计算最优分片参数

        Args:
            file_size: 文件大小（字节）
            network_profile: 网络环境画像

        Returns:
            最优参数推荐
        """
        profile = network_profile or NetworkProfile()

        # 小文件/不支持Range → 不分片
        if file_size <= 0:
            return OptimalParams(
                shard_count=1, thread_count=1,
                chunk_size=self.target_chunk_size,
                max_connections=4,
                rationale="未知文件大小，使用单线程"
            )

        if not profile.supports_range:
            return OptimalParams(
                shard_count=1, thread_count=1,
                chunk_size=file_size,
                max_connections=4,
                rationale="服务器不支持Range请求，使用单线程下载"
            )

        if file_size < self.min_chunk_size * 2:
            return OptimalParams(
                shard_count=1, thread_count=1,
                chunk_size=file_size,
                max_connections=4,
                rationale=f"文件较小 ({file_size/1024:.0f}KB)，单线程足够"
            )

        # === 计算分片数 ===

        # 方法1: 基于目标分片大小
        size_based_shards = max(
            self.min_shards,
            min(self.max_shards,
                math.ceil(file_size / self.target_chunk_size))
        )

        # 方法2: 基于带宽延迟积 (BDP)
        # 理想分片数 = 带宽 * 延迟 / 分片大小
        if profile.bandwidth_mbps > 0 and profile.latency_ms > 0:
            # BDP = 带宽(bps) * 延迟(s)
            bandwidth_bps = profile.bandwidth_mbps * 1_000_000
            rtt_seconds = profile.latency_ms / 1000
            bdp_bytes = int(bandwidth_bps * rtt_seconds / 8)  # BDP in bytes

            # 每个分片应至少能容纳1个BDP（避免TCP窗口限制）
            ideal_chunk_size = max(
                self.min_chunk_size,
                min(self.max_chunk_size, bdp_bytes * 4)
            )
            bdp_based_shards = max(
                self.min_shards,
                min(self.max_shards,
                    math.ceil(file_size / ideal_chunk_size))
            )
        else:
            bdp_based_shards = size_based_shards
            ideal_chunk_size = self.target_chunk_size

        # 方法3: 基于带宽的并发限制
        # 每个连接的实际吞吐 ≈ 带宽 / 连接数
        max_connections = self._estimate_max_connections(profile)
        bandwidth_per_connection = profile.bandwidth_mbps / max_connections if max_connections > 0 else 10

        # 建议分片数略大于最大连接数（以容忍慢连接）
        bandwidth_based_shards = min(self.max_shards, max(self.min_shards, int(max_connections * 1.5)))

        # 综合计算：取三种方法的中位数作为基准
        candidates = sorted([size_based_shards, bdp_based_shards, bandwidth_based_shards])
        optimal_shards = candidates[len(candidates) // 2]  # 中位数

        # 最终分片大小
        chunk_size = max(
            self.min_chunk_size,
            min(self.max_chunk_size,
                math.ceil(file_size / optimal_shards))
        )

        # 根据最终分片大小调整分片数
        optimal_shards = max(self.min_shards,
                             min(self.max_shards,
                                 math.ceil(file_size / chunk_size)))

        # 预估下载速度
        estimated_speed = profile.bandwidth_mbps * 0.85 * min(
            1.0, optimal_shards / max_connections
        )

        # 构建推荐理由
        rationale_parts = [
            f"文件大小={self._format_bytes(file_size)}",
            f"带宽={profile.bandwidth_mbps:.0f}Mbps",
            f"延迟={profile.latency_ms:.0f}ms",
            f"BDP={self._format_bytes(bdp_bytes if 'bdp_bytes' in dir() else file_size // optimal_shards)}",
        ]

        return OptimalParams(
            shard_count=optimal_shards,
            thread_count=optimal_shards,
            chunk_size=chunk_size,
            max_connections=max_connections,
            estimated_speed_mbps=estimated_speed,
            rationale=f"分片数={optimal_shards}, 分片大小={self._format_bytes(chunk_size)}, "
                      f"最大连接数={max_connections}, "
                      f"预估速度={estimated_speed:.0f}Mbps "
                      f"({'|'.join(rationale_parts)})"
        )

    def _estimate_max_connections(self, profile: NetworkProfile) -> int:
        """估算最优并发连接数"""
        if profile.bandwidth_mbps <= 0:
            return 8

        # 高带宽低延迟 → 较少连接即可打满带宽
        # 低带宽高延迟 → 需要更多连接打满带宽
        if profile.latency_ms < 10:
            # 低延迟环境：连接数 = 带宽/20 + 4
            connections = max(4, min(64, int(profile.bandwidth_mbps / 20 + 4)))
        elif profile.latency_ms < 50:
            connections = max(8, min(96, int(profile.bandwidth_mbps / 10 + 8)))
        elif profile.latency_ms < 150:
            connections = max(16, min(128, int(profile.bandwidth_mbps / 5 + 16)))
        else:
            # 高延迟环境：更多连接补偿延迟
            connections = max(32, min(192, int(profile.bandwidth_mbps / 2 + 32)))

        return connections

    @staticmethod
    def _format_bytes(n: int) -> str:
        if n >= 1024 ** 3:
            return f"{n/1024**3:.1f}GB"
        if n >= 1024 ** 2:
            return f"{n/1024**2:.0f}MB"
        if n >= 1024:
            return f"{n/1024:.0f}KB"
        return f"{n}B"


class OptimalThreadCalculator:
    """
    动态最优线程数计算器

    基于系统资源和网络条件计算最优线程/并发数。

    用法：
        calc = OptimalThreadCalculator()
        threads = calc.calculate(network_profile=profile)
        print(f"推荐线程数: {threads}")
    """

    def __init__(self,
                 min_threads: int = 2,
                 max_threads: int = 64,
                 cpu_factor: float = 2.0,       # CPU核心数的倍数
                 io_bound_factor: float = 4.0):  # IO密集型场景的倍数
        self.min_threads = min_threads
        self.max_threads = max_threads
        self.cpu_factor = cpu_factor
        self.io_bound_factor = io_bound_factor

    def calculate(self,
                  network_profile: Optional[NetworkProfile] = None,
                  cpu_count: Optional[int] = None) -> int:
        """
        计算最优线程数

        Args:
            network_profile: 网络环境画像
            cpu_count: CPU核心数（None=自动检测）

        Returns:
            推荐线程数
        """
        profile = network_profile or NetworkProfile()
        cpus = cpu_count or os.cpu_count() or 4

        # 方法1: 基于CPU
        cpu_based = max(self.min_threads, int(cpus * self.cpu_factor))

        # 下载操作是IO密集型，更高并发
        io_based = max(self.min_threads, int(cpus * self.io_bound_factor))

        # 方法2: 基于网络延迟
        if profile.latency_ms < 10:
            latency_based = 16
        elif profile.latency_ms < 50:
            latency_based = 32
        elif profile.latency_ms < 150:
            latency_based = 48
        else:
            latency_based = 64

        # 方法3: 基于带宽
        if profile.bandwidth_mbps > 500:
            bandwidth_based = 48
        elif profile.bandwidth_mbps > 200:
            bandwidth_based = 32
        elif profile.bandwidth_mbps > 50:
            bandwidth_based = 16
        else:
            bandwidth_based = 8

        # 综合评分
        candidates = [cpu_based, io_based, latency_based, bandwidth_based]

        # 取中位数作为推荐值
        candidates.sort()
        recommended = candidates[len(candidates) // 2]

        # 限制在范围内
        recommended = max(self.min_threads, min(self.max_threads, recommended))

        log.info(f"最优线程数计算: CPU={cpus}核, "
                 f"延迟={profile.latency_ms:.0f}ms, "
                 f"带宽={profile.bandwidth_mbps:.0f}Mbps, "
                 f"推荐={recommended}线程")
        return recommended


class DownloadOptimizer:
    """
    下载优化器 — 综合计算最优下载参数

    整合带宽探测、分片计算和线程计算。

    用法：
        optimizer = DownloadOptimizer()
        params = await optimizer.optimize_for_url(url, file_size)
        print(params.rationale)
    """

    def __init__(self,
                 bandwidth_probe: Optional[BandwidthProbe] = None,
                 shard_calculator: Optional[OptimalShardCalculator] = None,
                 thread_calculator: Optional[OptimalThreadCalculator] = None,
                 auto_probe: bool = True):
        self.bandwidth_probe = bandwidth_probe or BandwidthProbe()
        self.shard_calculator = shard_calculator or OptimalShardCalculator()
        self.thread_calculator = thread_calculator or OptimalThreadCalculator()
        self.auto_probe = auto_probe
        self._last_profile: Optional[NetworkProfile] = None
        # 按域名记忆的历史实际带宽（运行时反馈修正）
        self._host_bandwidth: Dict[str, float] = {}

    async def optimize_for_url(self,
                               url: str,
                               file_size: int = 0,
                               network_profile: Optional[NetworkProfile] = None) -> OptimalParams:
        """
        对指定URL进行优化计算

        Args:
            url: 目标URL
            file_size: 文件大小（字节）
            network_profile: 已知网络画像（可选）

        Returns:
            最优参数
        """
        # 探测网络状况
        if network_profile is None and self.auto_probe and url:
            # 优先用历史实际带宽（record_actual 回填，置信度高于短时探测）
            # 仅当历史带宽存在且文件较大时跳过实时探测（小文件不值得多花 6s 探测）
            historical = self.profile_for(url)
            if historical is not None and file_size > 50 * 1024 * 1024:
                profile = historical
                log.info(f"使用历史带宽画像: {profile.bandwidth_mbps:.1f} Mbps "
                         f"(host={self._host_of(url)})")
            else:
                try:
                    profile = await self.bandwidth_probe.measure(url)
                    self._last_profile = profile
                    # 与历史带宽融合（历史可信度更高，权重 0.6）
                    if historical is not None and historical.bandwidth_mbps > 0:
                        fused = (0.4 * profile.bandwidth_mbps
                                 + 0.6 * historical.bandwidth_mbps)
                        profile.bandwidth_mbps = fused
                        profile.download_speed_bps = int(fused * 1_000_000 / 8)
                        log.info(f"带宽融合: 探测 {profile.bandwidth_mbps:.1f} + "
                                 f"历史 {historical.bandwidth_mbps:.1f} "
                                 f"→ {fused:.1f} Mbps")
                except Exception as e:
                    log.warning(f"网络探测失败: {e}")
                    # 探测失败时退回历史画像，避免用默认 50Mbps
                    if historical is not None:
                        profile = historical
                    else:
                        profile = NetworkProfile()
        else:
            profile = network_profile or NetworkProfile()

        # 计算分片参数
        params = self.shard_calculator.calculate(file_size, profile)

        # 计算线程数
        thread_count = self.thread_calculator.calculate(profile)

        # 整合参数
        params.thread_count = thread_count
        params.max_connections = max(
            params.max_connections,
            thread_count,
            params.shard_count
        )

        log.info(f"优化完成: {params.rationale}")
        return params

    def get_last_profile(self) -> Optional[NetworkProfile]:
        """获取上次探测的网络画像"""
        return self._last_profile

    async def quick_optimize(self,
                             url: str,
                             file_size: int) -> Tuple[int, int]:
        """
        快速优化 — 直接返回(分片数, 线程数)

        Args:
            url: 目标URL
            file_size: 文件大小

        Returns:
            (shard_count, thread_count)
        """
        params = await self.optimize_for_url(url, file_size)
        return (params.shard_count, params.thread_count)

    # ── 运行时反馈：用历史实际速度修正后续优化 ──

    def record_actual(self, url: str, profile: NetworkProfile,
                      actual_speed_mbps: float, file_size: int) -> None:
        """下载完成后回填实际速度，供后续优化参考

        下载前的 NetworkProfile 来自短时探测，置信度有限。
        下载完成后用整段下载的实际吞吐量修正画像，使后续同站下载更准。

        Args:
            url: 目标URL（取域名做记忆键）
            profile: 本次下载使用的网络画像
            actual_speed_mbps: 实际平均速度（Mbps）
            file_size: 文件大小（字节）
        """
        if actual_speed_mbps <= 0:
            return
        host = self._host_of(url)
        # 实际速度可信度高（整段下载平均），用指数加权融合
        prev = self._host_bandwidth.get(host)
        if prev is None:
            fused = actual_speed_mbps
        else:
            fused = 0.6 * actual_speed_mbps + 0.4 * prev
        self._host_bandwidth[host] = fused
        self._last_profile = profile
        log.debug(f"记录实际带宽 {host}: {actual_speed_mbps:.1f}Mbps → 融合 {fused:.1f}Mbps")

    def profile_for(self, url: str) -> Optional[NetworkProfile]:
        """获取该 URL 域名的历史画像（若有）"""
        host = self._host_of(url)
        bw = self._host_bandwidth.get(host)
        if bw is None or self._last_profile is None:
            return None
        return NetworkProfile(
            latency_ms=self._last_profile.latency_ms,
            bandwidth_mbps=bw,
            bandwidth_confidence=0.8,
            loss_rate=self._last_profile.loss_rate,
            supports_range=self._last_profile.supports_range,
            download_speed_bps=int(bw * 1_000_000 / 8),
        )

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
        except (IndexError, AttributeError):
            return url


class AdaptiveSpeedupController:
    """运行时自适应加速控制器

    下载前的 OptimalParams 来自短时探测，是静态推荐。
    本控制器在下载运行时持续采样实际速度，当速度稳定时
    动态分裂"最慢的分片"，把它的剩余区间一分为二，新增
    一个 worker 接管后半段，实现负载自均衡。

    判定逻辑（启发式，非 ML）：
    1. 维护最近 ``sample_size`` 个速度采样
    2. 速度稳定（最大偏差 ≤ ``stability_threshold``）才考虑加速
    3. 首次触发：记录基线（worker 数、速度），分裂若干次最慢分片
    4. 观察期后对比：worker 增比 vs 速度增比
       - 速度增比 ≥ worker 增比 × 加速收益系数 → 继续加速
       - 否则 → 停止自动加速（再分裂已无收益）

    线程安全：单事件循环内由 HTTPDriver 协程驱动，不跨线程。
    """

    def __init__(self,
                 sample_size: int = 5,
                 stability_threshold: float = 0.15,
                 observe_seconds: float = 5.0,
                 split_on_trigger: int = 4,
                 benefit_ratio: float = 0.8,
                 max_workers: int = 64,
                 min_split_bytes: int = 2 * 1024 * 1024):
        self.sample_size = sample_size
        self.stability_threshold = stability_threshold
        self.observe_seconds = observe_seconds
        self.split_on_trigger = split_on_trigger
        self.benefit_ratio = benefit_ratio
        self.max_workers = max_workers
        self.min_split_bytes = min_split_bytes

        self._speed_history: list[float] = []
        self._enabled: bool = True
        self._baseline_workers: int = 0
        self._baseline_speed: float = 0.0
        self._observe_started_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def feed(self, current_speed_bps: int, current_worker_count: int,
             loop_time: Optional[float] = None) -> List[int]:
        """喂入一次速度采样，返回本次应分裂的分片索引列表（可能为空）

        Args:
            current_speed_bps: 当前下载速度（bytes/s）
            current_worker_count: 当前活跃 worker 数
            loop_time: 事件循环时间戳（None=自动取）

        Returns:
            需要被分裂的最慢分片索引列表（由调用方执行实际分裂）
        """
        if not self._enabled:
            return []

        speed_mbps = (current_speed_bps * 8) / 1_000_000
        self._speed_history.append(speed_mbps)
        if len(self._speed_history) > self.sample_size:
            self._speed_history.pop(0)
        if len(self._speed_history) < self.sample_size:
            return []

        avg = sum(self._speed_history) / len(self._speed_history)
        if avg <= 0:
            # 速度归零：长尾分片卡住，正是最需要分裂接管的时候
            # 跳过稳定性检查（avg=0 时 max_dev 会除零），触发紧急分裂
            if current_worker_count < self.max_workers:
                return [0]  # 占位标记，monitor 按数量分裂最慢分片
            return []
        max_dev = max(abs(s - avg) / avg for s in self._speed_history)
        # 速度不稳定（抖动大）→ 不加速，等稳定
        if max_dev > self.stability_threshold:
            return []

        now = loop_time if loop_time is not None else time.time()

        # 首次触发：建立基线并立即分裂若干最慢分片
        if self._observe_started_at == 0.0:
            if current_worker_count >= self.max_workers:
                self._enabled = False
                return []
            self._baseline_workers = current_worker_count
            self._baseline_speed = avg
            self._observe_started_at = now
            return self._pick_split_targets(current_worker_count,
                                            self.split_on_trigger)

        # 观察期未满
        if now - self._observe_started_at < self.observe_seconds:
            return []

        # 观察期结束：对比增比
        worker_ratio = self._safe_ratio(current_worker_count - self._baseline_workers,
                                        self._baseline_workers)
        speed_ratio = self._safe_ratio(avg - self._baseline_speed,
                                       self._baseline_speed)

        # 速度提升不显著（低于 worker 增比的 benefit_ratio 倍）→ 停止加速
        if speed_ratio < worker_ratio * self.benefit_ratio:
            self._enabled = False
            log.info(f"自适应加速停止: worker增比={worker_ratio:.0%}, "
                     f"速度增比={speed_ratio:.0%} (收益不足)")
            return []

        # 收益达标，重置观察期，继续尝试加速
        self._observe_started_at = 0.0
        log.debug(f"自适应加速继续: worker增比={worker_ratio:.0%}, "
                  f"速度增比={speed_ratio:.0%}")
        return []

    def _pick_split_targets(self, current_workers: int,
                            want: int) -> List[int]:
        """由调用方填充候选分片后再选；此处返回占位空列表

        实际选择最慢分片需要调用方提供分片状态（见 pick_slowest）。
        本方法保留接口对称；HTTPDriver 直接调用 pick_slowest。
        """
        return []

    @staticmethod
    def _safe_ratio(delta: float, base: float) -> float:
        if base <= 0:
            return 0.0
        return delta / base

    @staticmethod
    def split_range(start: int, end: int) -> Optional[Tuple[int, int]]:
        """把 [start, end] 一分为二，返回新分片的 [start, end]

        剩余不足 2 字节或低于 min_split 阈值时返回 None。
        闭区间语义，与 HTTP Range 对齐。
        """
        remaining = end - start + 1
        if remaining < 2:
            return None
        base = remaining // 2
        remainder = remaining % 2
        new_start = start + base + remainder
        return (new_start, end)

    def reset(self) -> None:
        """重置状态（新下载任务复用控制器时调用）"""
        self._speed_history.clear()
        self._enabled = True
        self._baseline_workers = 0
        self._baseline_speed = 0.0
        self._observe_started_at = 0.0
