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
  - TaskStep 步骤化管线（Agent 可观测）
  - 集中化错误目录（Agent 程序化处理）
  - 配置系统（环境变量驱动 + 校验器）
"""

from .core import DracoDownloader, DownloadResult, ProgressEvent
from .scheduler import Scheduler
from .engine import DownloadEngine
from .progress import ProgressManager
from .logger import get_logger
from .errors import (
    DracoError, make_error,
    ERR_UNSUPPORTED_PROTOCOL, ERR_PROBE_FAILED, ERR_HTTP_STATUS,
    ERR_RANGE_NOT_SUPPORTED, ERR_DOWNLOAD_FAILED, ERR_MERGE_FAILED,
    ERR_VERIFY_FAILED, ERR_TIMEOUT, ERR_CANCELLED,
    ERR_NETWORK, ERR_BT_NO_PEERS, ERR_BT_METADATA,
)
from .steps import (
    TaskStep, StepPipeline, StepStatus, StepResult,
    build_standard_pipeline,
    STEP_PROBE, STEP_DOWNLOAD, STEP_MERGE, STEP_VERIFY, STEP_SEED,
)
from .config import (
    DracoConfig, ConfigItem, get_global_config,
    RangeValidator, ChoiceValidator, IntValidator, BoolValidator, PathValidator,
)
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

__version__ = "1.3.2"
__all__ = [
    "DracoDownloader",
    "DownloadResult",
    "ProgressEvent",
    "Scheduler",
    "DownloadEngine",
    "ProgressManager",
    "get_logger",
    # 错误目录
    "DracoError", "make_error",
    "ERR_UNSUPPORTED_PROTOCOL", "ERR_PROBE_FAILED", "ERR_HTTP_STATUS",
    "ERR_RANGE_NOT_SUPPORTED", "ERR_DOWNLOAD_FAILED", "ERR_MERGE_FAILED",
    "ERR_VERIFY_FAILED", "ERR_TIMEOUT", "ERR_CANCELLED",
    "ERR_NETWORK", "ERR_BT_NO_PEERS", "ERR_BT_METADATA",
    # 步骤管线
    "TaskStep", "StepPipeline", "StepStatus", "StepResult",
    "build_standard_pipeline",
    "STEP_PROBE", "STEP_DOWNLOAD", "STEP_MERGE", "STEP_VERIFY", "STEP_SEED",
    # 配置系统
    "DracoConfig", "ConfigItem", "get_global_config",
    "RangeValidator", "ChoiceValidator", "IntValidator", "BoolValidator", "PathValidator",
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
