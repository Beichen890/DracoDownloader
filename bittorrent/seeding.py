"""
BT 做种策略

下载完成后根据分享率 / 时长决定何时停止做种。
参考 Ghost Downloader 3 的双限速策略，用纯 asyncio 实现。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable

from ..logger import get_logger

log = get_logger('bittorrent.seeding')


@dataclass
class SeedingPolicy:
    """做种策略配置

    Attributes:
        enabled: 是否启用做种
        ratio_limit: 分享率上限（0=不限，1.0=上传量等于下载量）
        time_limit: 做种时长上限（秒，0=不限）
        min_seed_time: 最小做种时长（秒，即使达到 ratio 也要做种到此时长）
    """
    enabled: bool = False
    ratio_limit: float = 0.0
    time_limit: float = 0.0
    min_seed_time: float = 0.0


@dataclass
class SeedingStats:
    """做种运行时统计"""
    started_at: float = 0.0
    uploaded: int = 0
    downloaded: int = 0

    @property
    def elapsed(self) -> float:
        if self.started_at == 0:
            return 0.0
        return time.time() - self.started_at

    @property
    def ratio(self) -> float:
        if self.downloaded <= 0:
            return 0.0
        return self.uploaded / self.downloaded


class SeedingController:
    """做种控制器

    用法:
        policy = SeedingPolicy(enabled=True, ratio_limit=1.0, time_limit=3600)
        controller = SeedingController(policy)
        controller.start(downloaded_bytes=total_size)
        # ... 上传循环中:
        controller.update_upload(uploaded_bytes)
        if controller.should_stop():
            break
    """

    def __init__(self, policy: SeedingPolicy):
        self.policy = policy
        self.stats = SeedingStats()

    def start(self, downloaded: int = 0):
        """开始做种计时"""
        self.stats = SeedingStats(
            started_at=time.time(),
            downloaded=downloaded,
        )
        log.info(
            f"做种开始 (ratio_limit={self.policy.ratio_limit}, "
            f"time_limit={self.policy.time_limit}s)"
        )

    def update_upload(self, uploaded: int):
        """更新已上传字节数"""
        self.stats.uploaded = uploaded

    def should_stop(self) -> bool:
        """判断是否应该停止做种

        停止条件（任一满足即可）:
        1. 做种被禁用
        2. 达到分享率上限（且超过最小做种时长）
        3. 达到做种时长上限
        """
        if not self.policy.enabled:
            return True

        elapsed = self.stats.elapsed
        ratio = self.stats.ratio

        # 最小做种时长未到，继续做种
        if elapsed < self.policy.min_seed_time:
            return False

        # 时长上限
        if self.policy.time_limit > 0 and elapsed >= self.policy.time_limit:
            log.info(f"做种达到时长上限 {self.policy.time_limit}s，停止")
            return True

        # 分享率上限
        if (self.policy.ratio_limit > 0
                and ratio >= self.policy.ratio_limit
                and self.policy.min_seed_time <= 0):
            log.info(f"做种达到分享率 {ratio:.2f} (limit={self.policy.ratio_limit})，停止")
            return True

        return False

    async def wait_until_stop(self, poll_interval: float = 5.0,
                              upload_reader: Optional[Callable[[], Awaitable[int]]] = None):
        """阻塞等待做种结束

        Args:
            poll_interval: 轮询间隔（秒）
            upload_reader: 异步读取已上传字节数的回调
        """
        if not self.policy.enabled:
            return

        while not self.should_stop():
            if upload_reader is not None:
                try:
                    uploaded = await upload_reader()
                    self.update_upload(uploaded)
                except Exception as e:
                    log.debug(f"读取上传量失败: {e}")
            await asyncio.sleep(poll_interval)

        log.info(
            f"做种结束: 时长 {self.stats.elapsed:.0f}s, "
            f"上传 {self.stats.uploaded / 1024 / 1024:.1f} MB, "
            f"分享率 {self.stats.ratio:.2f}"
        )


__all__ = [
    "SeedingPolicy",
    "SeedingStats",
    "SeedingController",
]
