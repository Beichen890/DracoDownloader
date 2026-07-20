"""
BT Web Tracker 模块

从公开的 Web Tracker 列表服务获取额外 tracker，与种子自带的 tracker
合并去重，提高 BT 下载的 peer 发现成功率。

参考 Ghost Downloader 3 的设计，但完全用标准库 + aiohttp 实现，
不引入第三方 BT 库。
"""

import asyncio
import json
import time
import urllib.parse
from typing import List, Set, Optional
from dataclasses import dataclass, field

import aiohttp

from ..logger import get_logger

log = get_logger('bittorrent.trackers')


# 公开的 Web Tracker 列表 API（返回 JSON 数组）
# 这些是社区维护的 tracker 列表聚合服务，均为宽松许可
_DEFAULT_TRACKER_LIST_URLS: List[str] = [
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
]

# 内置的常用 tracker 作为兜底（不依赖网络可达性）
FALLBACK_TRACKERS: List[str] = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "wss://tracker.openwebtorrent.com",
    "https://tracker.tamersunion.org:443/announce",
]


@dataclass
class TrackerCache:
    """tracker 缓存条目"""
    trackers: List[str] = field(default_factory=list)
    updated_at: float = 0.0
    source: str = ""


class WebTrackerFetcher:
    """从 Web 获取并缓存 tracker 列表

    特性:
    - 异步拉取多个 tracker 列表源
    - 带内存缓存（默认 1 小时 TTL）
    - 拉取失败时回退到内置 FALLBACK_TRACKERS
    - 支持 UDP/HTTP/HTTPS/WebSocket 协议过滤
    """

    def __init__(self,
                 cache_ttl: float = 3600.0,
                 list_urls: Optional[List[str]] = None,
                 timeout: float = 15.0):
        """
        Args:
            cache_ttl: 缓存有效期（秒）
            list_urls: tracker 列表 API URL（None=使用默认）
            timeout: 单源拉取超时（秒）
        """
        self._cache_ttl = cache_ttl
        self._list_urls = list_urls or list(_DEFAULT_TRACKER_LIST_URLS)
        self._timeout = timeout
        self._cache: Optional[TrackerCache] = None
        self._lock = asyncio.Lock()

    async def fetch(self, force_refresh: bool = False) -> List[str]:
        """获取 tracker 列表（带缓存）

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            去重后的 tracker URL 列表
        """
        async with self._lock:
            if (not force_refresh
                    and self._cache is not None
                    and time.time() - self._cache.updated_at < self._cache_ttl):
                log.debug(f"使用缓存 tracker 列表 ({len(self._cache.trackers)} 条)")
                return list(self._cache.trackers)

            trackers = await self._fetch_all_sources()
            self._cache = TrackerCache(
                trackers=trackers,
                updated_at=time.time(),
                source="merged",
            )
            log.info(f"Web Tracker 列表刷新: {len(trackers)} 条")
            return trackers

    async def _fetch_all_sources(self) -> List[str]:
        """并发拉取所有源并合并去重"""
        unique: Set[str] = set()
        tasks = [self._fetch_one(url) for url in self._list_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                log.debug(f"tracker 源拉取失败: {result}")
                continue
            for t in result:
                if self._is_valid_tracker(t):
                    unique.add(t)

        # 始终兜底
        for t in FALLBACK_TRACKERS:
            unique.add(t)

        return sorted(unique)

    async def _fetch_one(self, url: str) -> List[str]:
        """拉取单个源"""
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
                # 这些列表每行一个 tracker URL
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                return lines

    @staticmethod
    def _is_valid_tracker(url: str) -> bool:
        """校验 tracker URL 格式"""
        if not url or len(url) < 10:
            return False
        return url.startswith(('udp://', 'http://', 'https://', 'wss://', 'ws://'))


def merge_trackers(existing: List[str],
                   web_trackers: List[str],
                   announce_to_all: bool = True) -> List[str]:
    """合并 tracker 列表并去重

    参考Ghost Downloader 3 的 mergeTrackers 设计。

    Args:
        existing: 种子自带或 magnet 中的 tracker 列表
        web_trackers: 从 Web 获取的 tracker 列表
        announce_to_all: True=合并所有 tracker；False=只保留 existing

    Returns:
        去重后的 tracker 列表（existing 优先排在前面）
    """
    if not announce_to_all:
        return _dedupe(existing)

    seen: Set[str] = set()
    result: List[str] = []

    # existing 优先
    for t in existing:
        if isinstance(t, bytes):
            t = t.decode('utf-8', errors='replace')
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            result.append(t)

    # 追加 web tracker
    for t in web_trackers:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            result.append(t)

    return result


def _dedupe(trackers: List[str]) -> List[str]:
    """去重保持顺序"""
    seen: Set[str] = set()
    result: List[str] = []
    for t in trackers:
        if isinstance(t, bytes):
            t = t.decode('utf-8', errors='replace')
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


# 全局单例
_global_fetcher: Optional[WebTrackerFetcher] = None


def get_web_tracker_fetcher() -> WebTrackerFetcher:
    """获取全局 WebTrackerFetcher 实例"""
    global _global_fetcher
    if _global_fetcher is None:
        _global_fetcher = WebTrackerFetcher()
    return _global_fetcher


async def enrich_trackers(existing: List[str],
                          enable_web: bool = True,
                          announce_to_all: bool = True) -> List[str]:
    """便捷入口：合并种子自带 tracker 与 Web tracker

    Args:
        existing: 种子自带 tracker 列表
        enable_web: 是否启用 Web tracker 合并
        announce_to_all: 是否合并所有 tracker

    Returns:
        合并去重后的 tracker 列表
    """
    if not enable_web:
        return _dedupe(existing)

    try:
        fetcher = get_web_tracker_fetcher()
        web_trackers = await fetcher.fetch()
        return merge_trackers(existing, web_trackers, announce_to_all)
    except Exception as e:
        log.warning(f"Web tracker 获取失败，仅使用自带: {e}")
        return _dedupe(existing)


__all__ = [
    "WebTrackerFetcher",
    "TrackerCache",
    "FALLBACK_TRACKERS",
    "merge_trackers",
    "enrich_trackers",
    "get_web_tracker_fetcher",
]
