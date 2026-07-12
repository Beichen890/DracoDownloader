"""
协议驱动基类
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Callable, TYPE_CHECKING
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from ..core import ProgressEvent


@dataclass
class DownloadHandle:
    """下载句柄"""
    url: str
    output_path: str
    headers: Dict[str, str] = field(default_factory=dict)
    proxy: Optional[str] = None
    total_size: int = 0
    downloaded: int = 0
    progress: float = 0.0
    speed: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    paused: bool = False
    cancelled: bool = False
    # core 层设置的回调，供 _execute_download 使用
    progress_callback: Optional[Callable[..., None]] = None


class ProtocolDriver(ABC):
    """协议驱动抽象基类"""

    @abstractmethod
    def match(self, url: str) -> bool:
        """是否匹配该协议"""
        pass

    @abstractmethod
    async def probe(self, url: str) -> Dict[str, Any]:
        """
        探测文件信息

        Returns:
            {
                'size': int,           # 文件大小
                'supports_range': bool, # 是否支持断点续传
                'filename': str,       # 文件名
                'segments': int,       # M3U8 分片数 (可选)
                'duration': float,     # M3U8 时长 (可选)
                'is_live': bool,       # 是否直播 (可选)
            }
        """
        pass

    @abstractmethod
    async def download(self, handle: DownloadHandle,
                       callback: Optional[Callable[[int, int, int], None]] = None):
        """
        执行下载

        Args:
            handle: 下载句柄
            callback: 进度回调 (downloaded, total, speed)
        """
        pass

    @abstractmethod
    async def resume(self, handle: DownloadHandle,
                     callback: Optional[Callable[[int, int, int], None]] = None):
        """断点续传"""
        pass
