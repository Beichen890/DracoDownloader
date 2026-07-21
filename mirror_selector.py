"""
Mirror Selector — 自动寻找最优镜像站

通过多维度探测（延迟、带宽、可用性）自动选择最快的镜像站点。
支持自定义镜像列表，内置常用开源软件镜像站。
"""

import asyncio
import time
import socket
from typing import Optional, Dict, List, Tuple, Callable
from dataclasses import dataclass, field
from statistics import median, stdev

from .logger import get_logger

log = get_logger('mirror')

# === 常用镜像站列表（按地区/用途分类） ===

# 中国大陆常用镜像站
CN_MIRRORS = [
    # 华为云
    {"name": "Huawei Cloud (CN)", "base_url": "https://mirrors.huaweicloud.com", "region": "cn"},
    # 阿里云
    {"name": "Alibaba Cloud (CN)", "base_url": "https://mirrors.aliyun.com", "region": "cn"},
    # 腾讯云
    {"name": "Tencent Cloud (CN)", "base_url": "https://mirrors.cloud.tencent.com", "region": "cn"},
    # 清华大学 TUNA
    {"name": "Tsinghua TUNA (CN)", "base_url": "https://mirrors.tuna.tsinghua.edu.cn", "region": "cn"},
    # 中国科学技术大学 USTC
    {"name": "USTC (CN)", "base_url": "https://mirrors.ustc.edu.cn", "region": "cn"},
    # 网易
    {"name": "Netease (CN)", "base_url": "https://mirrors.163.com", "region": "cn"},
    # 上海交通大学 SJTUG
    {"name": "SJTU (CN)", "base_url": "https://mirrors.sjtug.sjtu.edu.cn", "region": "cn"},
    # 北京外国语大学 BFSU
    {"name": "BFSU (CN)", "base_url": "https://mirrors.bfsu.edu.cn", "region": "cn"},
]

# 国际常用镜像站
GLOBAL_MIRRORS = [
    {"name": "Cloudflare (Global)", "base_url": "https://cdnjs.cloudflare.com", "region": "global"},
    {"name": "GitHub Releases", "base_url": "https://github.com", "region": "global"},
    {"name": "jsDelivr (Global)", "base_url": "https://cdn.jsdelivr.net", "region": "global"},
    {"name": "Unpkg (Global)", "base_url": "https://unpkg.com", "region": "global"},
]

# Python 包镜像站
PYPI_MIRRORS = [
    {"name": "PyPI Official", "base_url": "https://pypi.org", "region": "global"},
    {"name": "Tsinghua PyPI", "base_url": "https://pypi.tuna.tsinghua.edu.cn", "region": "cn"},
    {"name": "Aliyun PyPI", "base_url": "https://mirrors.aliyun.com/pypi", "region": "cn"},
    {"name": "Huawei PyPI", "base_url": "https://repo.huaweicloud.com/repository/pypi", "region": "cn"},
    {"name": "Tencent PyPI", "base_url": "https://mirrors.cloud.tencent.com/pypi", "region": "cn"},
    {"name": "USTC PyPI", "base_url": "https://pypi.mirrors.ustc.edu.cn", "region": "cn"},
    {"name": "Douban PyPI", "base_url": "https://pypi.doubanio.com", "region": "cn"},
]


@dataclass
class MirrorProbeResult:
    """镜像探测结果"""
    name: str
    base_url: str
    region: str
    latency_ms: float = 0.0          # TCP连接延迟（毫秒）
    dns_resolve_ms: float = 0.0      # DNS解析时间（毫秒）
    bandwidth_mbps: float = 0.0      # 测得的带宽（Mbps）
    http_status: int = 0             # HTTP响应状态码
    alive: bool = False              # 是否可用
    error: Optional[str] = None      # 错误信息
    score: float = 0.0               # 综合评分（越低越好）


class MirrorSelector:
    """
    镜像选择器 — 自动探测并选择最优镜像站

    用法：
        selector = MirrorSelector()
        best = await selector.select_best(mirror_list)
        print(f"最佳镜像: {best.name} ({best.latency_ms:.0f}ms)")
    """

    def __init__(self,
                 probe_timeout: float = 5.0,
                 dns_servers: Optional[List[str]] = None,
                 concurrency: int = 10,
                 probe_download_size: int = 1024 * 1024,  # 1MB 测速文件
                 bandwidth_test: bool = True):
        """
        Args:
            probe_timeout: 单次探测超时（秒）
            dns_servers: 自定义DNS服务器
            concurrency: 并行探测数
            probe_download_size: 测速下载大小（字节）
            bandwidth_test: 是否进行带宽测试
        """
        self.probe_timeout = probe_timeout
        self.dns_servers = dns_servers
        self.concurrency = concurrency
        self.probe_download_size = probe_download_size
        self.bandwidth_test = bandwidth_test

    async def select_best(self,
                          mirrors: Optional[List[Dict[str, str]]] = None,
                          probe_path: str = "/") -> MirrorProbeResult:
        """
        选择最优镜像站

        Args:
            mirrors: 镜像站列表 [{name, base_url, region}]
            probe_path: 探测路径

        Returns:
            最优镜像探测结果
        """
        if mirrors is None:
            mirrors = CN_MIRRORS + GLOBAL_MIRRORS

        if not mirrors:
            raise ValueError("镜像列表为空")

        # 并行探测所有镜像
        results = await self._probe_all(mirrors, probe_path)

        # 过滤可用镜像
        alive = [r for r in results if r.alive]
        if not alive:
            # 所有镜像都不可用，返回评分最低的（最少错误的）
            log.warning("没有可用镜像站，返回错误最少的")
            results.sort(key=lambda r: 0 if r.error else 1)
            return results[0]

        # 按综合评分排序
        self._calculate_scores(alive)
        alive.sort(key=lambda r: r.score)

        best = alive[0]
        log.info(f"最优镜像: {best.name} ({best.base_url}), "
                 f"延迟={best.latency_ms:.0f}ms, "
                 f"带宽={best.bandwidth_mbps:.1f}Mbps, "
                 f"评分={best.score:.2f}")
        return best

    async def _probe_all(self,
                         mirrors: List[Dict[str, str]],
                         probe_path: str) -> List[MirrorProbeResult]:
        """并行探测所有镜像站"""
        semaphore = asyncio.Semaphore(self.concurrency)

        async def probe_one(mirror: Dict[str, str]) -> MirrorProbeResult:
            async with semaphore:
                return await self._probe_single(mirror, probe_path)

        tasks = [probe_one(m) for m in mirrors]
        return await asyncio.gather(*tasks)

    async def _probe_single(self,
                            mirror: Dict[str, str],
                            probe_path: str) -> MirrorProbeResult:
        """探测单个镜像站"""
        result = MirrorProbeResult(
            name=mirror["name"],
            base_url=mirror["base_url"],
            region=mirror.get("region", "unknown"),
        )

        import aiohttp

        test_url = f"{mirror['base_url'].rstrip('/')}{probe_path}"

        try:
            # 阶段1: DNS解析延迟测试
            dns_start = time.time()
            try:
                host = test_url.split("://")[1].split("/")[0].split(":")[0]
                await asyncio.get_event_loop().getaddrinfo(host, 80)
                result.dns_resolve_ms = (time.time() - dns_start) * 1000
            except Exception:
                result.dns_resolve_ms = 9999.0

            timeout = aiohttp.ClientTimeout(total=self.probe_timeout, connect=5)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 阶段2: HTTP HEAD 探测
                head_start = time.time()
                async with session.head(test_url, allow_redirects=True) as resp:
                    result.latency_ms = (time.time() - head_start) * 1000
                    result.http_status = resp.status
                    result.alive = resp.status < 500

                if not result.alive:
                    return result

                # 阶段3: 带宽测试（下载小文件测速）
                if self.bandwidth_test and result.alive:
                    try:
                        bw_start = time.time()
                        downloaded = 0
                        async with session.get(test_url) as resp:
                            if resp.status == 200:
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    downloaded += len(chunk)
                                    elapsed = time.time() - bw_start
                                    if elapsed >= 2.0 or downloaded >= self.probe_download_size:
                                        break
                        elapsed = time.time() - bw_start
                        if elapsed > 0 and downloaded > 0:
                            result.bandwidth_mbps = (downloaded * 8) / (elapsed * 1_000_000)
                    except Exception as e:
                        log.debug(f"带宽测试失败 {result.name}: {e}")

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            result.error = str(e)
            result.alive = False
            log.debug(f"镜像不可达 {result.name}: {e}")

        except Exception as e:
            result.error = str(e)
            result.alive = False
            log.warning(f"探测镜像异常 {result.name}: {e}")

        return result

    def _calculate_scores(self, results: List[MirrorProbeResult]):
        """计算综合评分（越低越好）"""
        if not results:
            return

        # 提取各维度数值
        latencies = [r.latency_ms for r in results if r.latency_ms > 0]
        bandwidths = [r.bandwidth_mbps for r in results if r.bandwidth_mbps > 0]
        dns_times = [r.dns_resolve_ms for r in results if r.dns_resolve_ms > 0]

        max_latency = max(latencies) if latencies else 1
        max_bandwidth = max(bandwidths) if bandwidths else 1
        max_dns = max(dns_times) if dns_times else 1

        for r in results:
            # 评分权重: 延迟(40%) + 带宽(40%) + DNS时间(20%)
            latency_score = (r.latency_ms / max_latency) * 0.4 if max_latency > 0 else 0.4
            bandwidth_score = (1 - (r.bandwidth_mbps / max_bandwidth)) * 0.4 if max_bandwidth > 0 else 0.4
            dns_score = (r.dns_resolve_ms / max_dns) * 0.2 if max_dns > 0 else 0.2

            # 可用性惩罚
            availability_penalty = 0 if r.alive else 10.0

            r.score = latency_score + bandwidth_score + dns_score + availability_penalty


class SmartMirrorDownloader:
    """
    智能镜像下载器 — 自动选择最优镜像并下载

    用法：
        downloader = SmartMirrorDownloader()
        result = await downloader.download_with_mirror(
            url="https://example.com/file.zip",
            mirrors=CN_MIRRORS,
            output_path="./downloads/file.zip"
        )
    """

    def __init__(self,
                 selector: Optional[MirrorSelector] = None,
                 fallback_to_original: bool = True,
                 cache_best: bool = True,
                 cache_ttl: int = 300):  # 5分钟缓存
        self.selector = selector or MirrorSelector()
        self.fallback_to_original = fallback_to_original
        self.cache_best = cache_best
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[MirrorProbeResult, float]] = {}

    async def select_mirror(self,
                            original_url: str,
                            mirrors: Optional[List[Dict[str, str]]] = None,
                            probe_path: Optional[str] = None) -> Optional[str]:
        """
        选择最优镜像URL

        Args:
            original_url: 原始URL
            mirrors: 镜像列表
            probe_path: 探测路径

        Returns:
            最优镜像URL，如无可用镜像则返回原始URL
        """
        # 检查缓存
        cache_key = original_url.split("//")[1].split("/")[0] if "//" in original_url else original_url
        if self.cache_best and cache_key in self._cache:
            cached_result, cached_time = self._cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                log.info(f"使用缓存镜像: {cached_result.name}")
                return self._build_mirror_url(cached_result.base_url, original_url)

        # 探测最优镜像
        if probe_path is None:
            probe_path = "/" + "/".join(original_url.split("/")[3:]) if len(original_url.split("/")) > 3 else "/"

        try:
            best = await self.selector.select_best(mirrors, probe_path)

            # 缓存
            if self.cache_best:
                self._cache[cache_key] = (best, time.time())

            if best.alive:
                mirror_url = self._build_mirror_url(best.base_url, original_url)
                log.info(f"选择镜像: {best.name} -> {mirror_url}")
                return mirror_url
        except Exception as e:
            log.warning(f"镜像选择失败: {e}")

        # 回退到原始URL
        if self.fallback_to_original:
            log.info("回退到原始URL")
            return original_url

        return None

    @staticmethod
    def _build_mirror_url(mirror_base: str, original_url: str) -> str:
        """构建镜像URL"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(original_url)
            path = parsed.path
            if not path:
                path = "/"
            base = mirror_base.rstrip("/")
            return f"{base}{path}"
        except Exception:
            if "://" in original_url:
                path = "/" + "/".join(original_url.split("/")[3:])
                if not path or path == "/":
                    path = "/"
            else:
                path = original_url

            base = mirror_base.rstrip("/")
            return f"{base}{path}"

    def clear_cache(self):
        """清除镜像缓存"""
        self._cache.clear()

    def get_preferred_mirrors(self, region: str = "cn") -> List[Dict[str, str]]:
        """获取首选镜像列表"""
        if region == "cn":
            return CN_MIRRORS
        elif region == "global":
            return GLOBAL_MIRRORS
        return CN_MIRRORS + GLOBAL_MIRRORS


# 预置镜像源分类
MIRROR_CATEGORIES = {
    "cn": CN_MIRRORS,
    "global": GLOBAL_MIRRORS,
    "pypi": PYPI_MIRRORS,
}
