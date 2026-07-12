"""
协议驱动模块
"""

from .base import ProtocolDriver, DownloadHandle
from .http import HTTPDriver
from .ftp import FTPDriver
from .m3u8 import M3U8Driver
from .torrent import TorrentDriver


class ProtocolRouter:
    """协议路由器"""

    def __init__(self):
        self._drivers = []
        self._register_defaults()

    def _register_defaults(self):
        # 更具体的协议先注册（M3U8 优先于 HTTP，因为 M3U8 URL 也是 HTTP URL）
        self.register(M3U8Driver())      # .m3u8 结尾的 HTTP URL
        self.register(HTTPDriver())      # http:// / https://
        self.register(FTPDriver())       # ftp:// / ftps://
        self.register(TorrentDriver())   # magnet: / .torrent

    def register(self, driver):
        self._drivers.append(driver)

    def route(self, url: str):
        for driver in self._drivers:
            if driver.match(url):
                return driver
        return None

    def list_supported(self) -> list[str]:
        return [d.__class__.__name__ for d in self._drivers]


__all__ = [
    "ProtocolDriver",
    "DownloadHandle",
    "HTTPDriver",
    "FTPDriver",
    "M3U8Driver",
    "TorrentDriver",
    "ProtocolRouter"
]
