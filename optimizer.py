"""
Download Optimizer — 动态最优分片数和线程数计算

通过评估网络条件（延迟、带宽）、文件大小和系统资源，
动态计算最优并发参数，最大化下载效率。
"""

import asyncio
import time
import os
import math
from typing import Optional, Dict, Any, Tuple
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

    def __init__(self, probe_size: int = 5 * 1024 * 1024,  # 5MB测速文件
                 probe_timeout: float = 15.0,
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
                bw_start = time.time()
                downloaded = 0

                async with session.get(url) as resp:
                    if resp.status == 200:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            downloaded += len(chunk)
                            elapsed = time.time() - bw_start
                            if elapsed >= 3.0 or downloaded >= self.probe_size:
                                break

                elapsed = time.time() - bw_start
                if elapsed > 0 and downloaded >= self.min_download:
                    profile.download_speed_bps = int(downloaded * 8 / elapsed)
                    profile.bandwidth_mbps = (downloaded * 8) / (elapsed * 1_000_000)
                    profile.bandwidth_confidence = min(
                        1.0,
                        max(0.3, downloaded / self.probe_size)
                    )
                    log.info(f"带宽测量: {downloaded/1024/1024:.1f}MB in {elapsed:.1f}s, "
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
            try:
                profile = await self.bandwidth_probe.measure(url)
                self._last_profile = profile
            except Exception as e:
                log.warning(f"网络探测失败: {e}")
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
