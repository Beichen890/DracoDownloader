"""
磁力链接解析器 - 纯 Python
"""

import re
import urllib.parse
import hashlib
import base64
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field


@dataclass
class MagnetLink:
    """磁力链接数据结构"""
    info_hash: bytes
    info_hash_hex: str
    display_name: str = ""
    trackers: List[str] = field(default_factory=list)
    ascii_name: str = ""
    file_size: int = 0
    file_names: List[str] = field(default_factory=list)
    seeds: int = 0
    peers: int = 0


class MagnetParser:
    """磁力链接解析器 - BEP 9"""

    @classmethod
    def parse(cls, magnet_uri: str) -> Optional[MagnetLink]:
        """
        解析磁力链接

        Args:
            magnet_uri: 磁力链接字符串

        Returns:
            MagnetLink 对象，或 None 如果解析失败
        """
        if not magnet_uri or not magnet_uri.startswith('magnet:'):
            return None

        # 解析查询参数
        parsed = urllib.parse.urlparse(magnet_uri)
        if not parsed.query:
            return None

        params = urllib.parse.parse_qs(parsed.query)

        # 提取 info_hash (xt参数)
        xt_values = params.get('xt', [])
        info_hash = None
        info_hash_hex = None

        for xt in xt_values:
            if xt.startswith('urn:btih:'):
                info_hash_hex = xt[9:]
                break

        if not info_hash_hex:
            return None

        # 处理不同长度的 hash (40 字符 SHA1 或 32 字符 Base32)
        if len(info_hash_hex) == 32:
            # Base32 编码 → 解码为 bytes
            try:
                info_hash = base64.b32decode(info_hash_hex.upper())
            except Exception:
                return None
        elif len(info_hash_hex) == 40:
            # 十六进制 → bytes
            try:
                info_hash = bytes.fromhex(info_hash_hex)
            except ValueError:
                return None
        else:
            return None

        if len(info_hash) != 20:
            return None

        # 提取显示名称 (dn参数)
        display_name = params.get('dn', [''])[0]
        if isinstance(display_name, bytes):
            display_name = display_name.decode('utf-8', errors='replace')

        # 提取 trackers (tr参数)
        trackers = params.get('tr', [])
        trackers = [t.decode() if isinstance(t, bytes) else t for t in trackers]

        # 提取备用名称 (as参数)
        ascii_name = params.get('as', [''])[0]
        if isinstance(ascii_name, bytes):
            ascii_name = ascii_name.decode('utf-8', errors='replace')

        return MagnetLink(
            info_hash=info_hash,
            info_hash_hex=info_hash_hex.lower(),
            display_name=display_name or 'magnet_download',
            trackers=trackers,
            ascii_name=ascii_name,
        )

    @classmethod
    def create(cls, info_hash: bytes, name: str = "", trackers: List[str] = None) -> str:
        """创建磁力链接"""
        params = [f"xt=urn:btih:{info_hash.hex()}"]
        if name:
            params.append(f"dn={urllib.parse.quote(name)}")
        if trackers:
            for t in trackers:
                params.append(f"tr={urllib.parse.quote(t)}")
        return "magnet:?" + "&".join(params)
