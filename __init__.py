"""
DracoDownloader - Agent 原生下载器
完全自主开发，无 GPL 依赖

Features:
  - HTTP/HTTPS 多分片下载（动态最优分片/线程数）
  - FTP/FTPS 下载
  - M3U8/HLS 流下载（AES-128 解密）
  - BitTorrent / 磁力链接下载（纯自研协议栈）
  - 自动最优镜像站选择
  - 动态最优分片数/线程数计算
"""

from .core import DracoDownloader, DownloadResult, ProgressEvent
from .scheduler import Scheduler
from .engine import DownloadEngine
from .progress import ProgressManager
from .logger import get_logger
from .mirror_selector import (
    MirrorSelector, SmartMirrorDownloader,
    MirrorProbeResult, MIRROR_CATEGORIES,
    CN_MIRRORS, GLOBAL_MIRRORS, PYPI_MIRRORS,
)
from .optimizer import (
    DownloadOptimizer, OptimalShardCalculator,
    OptimalThreadCalculator, BandwidthProbe,
    NetworkProfile, OptimalParams,
)

__version__ = "1.2.0"
__all__ = [
    "DracoDownloader",
    "DownloadResult",
    "ProgressEvent",
    "Scheduler",
    "DownloadEngine",
    "ProgressManager",
    "get_logger",
    # Mirror selector
    "MirrorSelector",
    "SmartMirrorDownloader",
    "MirrorProbeResult",
    "MIRROR_CATEGORIES",
    "CN_MIRRORS",
    "GLOBAL_MIRRORS",
    "PYPI_MIRRORS",
    # Optimizer
    "DownloadOptimizer",
    "OptimalShardCalculator",
    "OptimalThreadCalculator",
    "BandwidthProbe",
    "NetworkProfile",
    "OptimalParams",
]
