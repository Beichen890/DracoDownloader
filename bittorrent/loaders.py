"""
BT 多源加载器

统一三种来源的 torrent 解析入口:
- 本地 .torrent 文件
- HTTP/HTTPS URL 指向的 .torrent
- 磁力链接 (magnet:)

参考 Ghost Downloader 3 的 loaders.py 设计，但用纯自研 BT 栈，
不依赖 libtorrent。磁力链接的元数据通过 DHT + Peer Wire Protocol 获取。
"""

import asyncio
import urllib.request
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from ..logger import get_logger
from ..errors import DracoError, bt_invalid_torrent, make_error, ERR_BT_METADATA
from .magnet import MagnetParser, MagnetLink
from .bencode import decode_torrent, info_hash as calc_info_hash

log = get_logger('bittorrent.loaders')


@dataclass
class ResolvedTorrent:
    """统一的 torrent 解析结果

    Attributes:
        source_type: 来源类型 ('file' | 'url' | 'magnet')
        source: 原始来源字符串
        info_hash: 20 字节 info_hash
        info_hash_hex: 40 字符十六进制
        name: 名称
        total_size: 总字节数（magnet 元数据未获取时为 0）
        piece_length: 分片长度
        pieces_hashes: 分片哈希列表（magnet 未获取时为空）
        files: 文件列表（多文件 torrent）
        trackers: tracker 列表
        is_magnet: 是否为磁力链接（需要后续元数据获取）
        raw_bytes: 原始 torrent 文件字节（magnet 为 None）
    """
    source_type: str
    source: str
    info_hash: bytes
    info_hash_hex: str
    name: str = ""
    total_size: int = 0
    piece_length: int = 0
    pieces_hashes: List[bytes] = None  # type: ignore[assignment]
    files: List[Dict[str, Any]] = None  # type: ignore[assignment]
    trackers: List[str] = None  # type: ignore[assignment]
    is_magnet: bool = False
    raw_bytes: Optional[bytes] = None

    def __post_init__(self):
        if self.pieces_hashes is None:
            self.pieces_hashes = []
        if self.files is None:
            self.files = []
        if self.trackers is None:
            self.trackers = []

    def to_meta_dict(self) -> Dict[str, Any]:
        """转换为 downloader 可用的 meta 字典"""
        return {
            'info_hash': self.info_hash,
            'info_hash_hex': self.info_hash_hex,
            'name': self.name,
            'piece_length': self.piece_length,
            'pieces_hashes': self.pieces_hashes,
            'total_size': self.total_size,
            'files': self.files,
            'trackers': self.trackers,
            'is_magnet': self.is_magnet,
        }


def _is_magnet(source: str) -> bool:
    return source.startswith('magnet:')


def _is_url(source: str) -> bool:
    return source.startswith('http://') or source.startswith('https://')


def _is_file(source: str) -> bool:
    """判断是否为本地文件路径"""
    return Path(source).exists() and Path(source).is_file()


def _parse_torrent_bytes(data: bytes, source: str, source_type: str) -> ResolvedTorrent:
    """解析 .torrent 字节流"""
    if not data:
        raise bt_invalid_torrent("空数据")

    info_hash = calc_info_hash(data)
    torrent = decode_torrent(data)
    info = torrent.get('info', {})

    pieces_raw = info.get('pieces', b'')
    pieces_hashes = [pieces_raw[i:i + 20] for i in range(0, len(pieces_raw), 20)]

    total_size = 0
    files = []
    is_multi = False

    if 'files' in info:
        is_multi = True
        for f_info in info['files']:
            path_parts = f_info.get('path', [])
            f_path = '/'.join(
                p.decode() if isinstance(p, bytes) else p
                for p in path_parts
            ) if isinstance(path_parts, list) else str(path_parts)
            f_len = f_info.get('length', 0)
            files.append({'path': f_path, 'length': f_len})
            total_size += f_len
    else:
        total_size = info.get('length', 0)

    name = info.get('name', b'download')
    if isinstance(name, bytes):
        name = name.decode('utf-8', errors='replace')

    # trackers: 优先 announce-list，回退到 announce
    trackers_raw = torrent.get('announce-list', [])
    trackers: List[str] = []
    if trackers_raw:
        for tier in trackers_raw:
            if isinstance(tier, list):
                for t in tier:
                    if isinstance(t, bytes):
                        t = t.decode('utf-8', errors='replace')
                    if t:
                        trackers.append(t)
            elif isinstance(tier, (bytes, str)):
                t = tier.decode() if isinstance(tier, bytes) else tier
                if t:
                    trackers.append(t)
    else:
        ann = torrent.get('announce', b'')
        if isinstance(ann, bytes):
            ann = ann.decode('utf-8', errors='replace')
        if ann:
            trackers.append(ann)

    return ResolvedTorrent(
        source_type=source_type,
        source=source,
        info_hash=info_hash,
        info_hash_hex=info_hash.hex(),
        name=name,
        total_size=total_size,
        piece_length=info.get('piece length', 0),
        pieces_hashes=pieces_hashes,
        files=files,
        trackers=trackers,
        is_magnet=False,
        raw_bytes=data,
    )


def _parse_magnet(source: str) -> ResolvedTorrent:
    """解析磁力链接"""
    magnet = MagnetParser.parse(source)
    if magnet is None:
        raise bt_invalid_torrent(f"无效的磁力链接: {source}")

    return ResolvedTorrent(
        source_type='magnet',
        source=source,
        info_hash=magnet.info_hash,
        info_hash_hex=magnet.info_hash_hex,
        name=magnet.display_name,
        trackers=magnet.trackers,
        is_magnet=True,
        raw_bytes=None,
    )


async def resolve(source: str, download_timeout: float = 30.0) -> ResolvedTorrent:
    """统一解析入口

    自动识别来源类型并解析:
    - magnet: → 纯解析（元数据需后续通过 DHT 获取）
    - http(s) URL → 异步下载 .torrent 字节流 → 解析
    - 本地文件 → 异步读取 → 解析

    Args:
        source: magnet 链接、HTTP URL 或本地文件路径
        download_timeout: URL 下载超时（秒）

    Returns:
        ResolvedTorrent 解析结果

    Raises:
        DracoError: 解析失败
    """
    if not source:
        raise bt_invalid_torrent("空来源")

    # 磁力链接
    if _is_magnet(source):
        log.debug(f"解析磁力链接: {source[:60]}")
        return _parse_magnet(source)

    # HTTP URL
    if _is_url(source):
        log.debug(f"从 URL 下载 torrent: {source[:60]}")
        try:
            data = await asyncio.to_thread(_download_torrent_url, source, download_timeout)
        except Exception as e:
            raise make_error(ERR_BT_METADATA, detail=f"下载 torrent 失败: {e}")
        return _parse_torrent_bytes(data, source, 'url')

    # 本地文件
    if _is_file(source):
        log.debug(f"读取本地 torrent 文件: {source}")
        try:
            data = await asyncio.to_thread(_read_torrent_file, source)
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise bt_invalid_torrent(str(e))
        return _parse_torrent_bytes(data, source, 'file')

    raise bt_invalid_torrent(f"无法识别的来源: {source}")


def _download_torrent_url(url: str, timeout: float) -> bytes:
    """通过 HTTP 下载 .torrent 文件（阻塞，通过 to_thread 调用）"""
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'DracoDownloader/1.2'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        raise bt_invalid_torrent("URL 返回空内容")
    # 简单校验是否为 bencode
    if not data.startswith(b'd'):
        raise bt_invalid_torrent("URL 返回内容不是有效的 torrent")
    return data


def _read_torrent_file(path: str) -> bytes:
    """读取本地 torrent 文件（阻塞）"""
    with open(path, 'rb') as f:
        data = f.read()
    if not data.startswith(b'd'):
        raise bt_invalid_torrent(f"文件不是有效的 torrent: {path}")
    return data


__all__ = [
    "ResolvedTorrent",
    "resolve",
]
