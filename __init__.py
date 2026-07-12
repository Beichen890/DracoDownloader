"""
DracoDownloader - Agent 原生下载器
完全自主开发，无 GPL 依赖
"""

from .core import DracoDownloader, DownloadResult, ProgressEvent
from .scheduler import Scheduler
from .engine import DownloadEngine
from .progress import ProgressManager
from .logger import get_logger

__version__ = "1.1.0"
__all__ = [
    "DracoDownloader",
    "DownloadResult",
    "ProgressEvent",
    "Scheduler",
    "DownloadEngine",
    "ProgressManager",
    "get_logger",
]
