"""
BitTorrent/磁力链接协议驱动 - 使用自研 BT 实现
纯自研，无 GPL 依赖
"""

import asyncio
import os
from pathlib import Path
from typing import Optional, Dict, Any, Callable

from .base import ProtocolDriver, DownloadHandle
from ..bittorrent.downloader import BTDownloader
from ..bittorrent.magnet import MagnetParser


class TorrentDriver(ProtocolDriver):
    """
    BitTorrent 驱动 - 纯自研实现
    
    使用自研的 BTDownloader (DHT Kademlia + Peer Wire Protocol)
    完全自主开发，无任何 GPL 依赖
    """

    def __init__(self):
        self._active_downloads: Dict[str, BTDownloader] = {}

    def match(self, url: str) -> bool:
        return url.startswith('magnet:') or url.endswith('.torrent')

    async def probe(self, url: str) -> Dict[str, Any]:
        """探测种子信息"""
        if url.startswith('magnet:'):
            magnet = MagnetParser.parse(url)
            if not magnet:
                raise ValueError("Invalid magnet link")
            return {
                'size': 0,  # 磁力链接在获取元数据前大小未知
                'filename': magnet.display_name or 'magnet_download',
                'is_torrent': True,
                'info_hash': magnet.info_hash_hex,
                'has_files': False,
            }
        else:
            # .torrent 文件
            from ..bittorrent.bencode import decode_torrent
            with open(url, 'rb') as f:
                data = f.read()
            torrent = decode_torrent(data)
            parsed = torrent.get('_parsed', {})
            info = torrent.get('info', {})

            name = info.get('name', b'torrent')
            if isinstance(name, bytes):
                name = name.decode('utf-8', errors='replace')

            files = info.get('files', [])
            file_list = []
            if files:
                for f_info in files:
                    path_parts = f_info.get('path', [])
                    if isinstance(path_parts, list):
                        f_path = '/'.join(p.decode() if isinstance(p, bytes) else p for p in path_parts)
                    else:
                        f_path = str(path_parts)
                    file_list.append({
                        'path': f_path,
                        'length': f_info.get('length', 0),
                    })

            return {
                'size': parsed.get('total_size', 0),
                'filename': name,
                'is_torrent': True,
                'files': file_list,
                'piece_count': parsed.get('piece_count', 0),
                'piece_length': parsed.get('piece_length', 0),
                'has_files': bool(file_list),
            }

    async def download(self, handle: DownloadHandle,
                       callback: Optional[Callable[[int, int, int], None]] = None):
        """使用自研 BT 下载器下载"""
        output_path = Path(handle.output_path)

        # 创建下载器
        downloader = BTDownloader(
            source=handle.url,
            output_path=str(output_path),
            progress_callback=callback,
        )

        # 尝试获取 trackers 信息
        try:
            metadata = handle.metadata or {}
            if not metadata.get('has_files', False) and handle.url.endswith('.torrent'):
                # 从 .torrent 文件重新 probe 会拿到完整信息
                pass
        except Exception:
            pass

        # 存储活跃下载
        task_key = str(handle.url) + str(output_path)
        self._active_downloads[task_key] = downloader

        try:
            await downloader.start()
        finally:
            self._active_downloads.pop(task_key, None)

        # BT 下载完成后，检查文件是否存在
        if not output_path.exists():
            # 可能是目录形式（多文件）
            if output_path.is_dir() or not output_path.suffix:
                pass  # 多文件已下载到子目录中

    async def resume(self, handle: DownloadHandle,
                     callback: Optional[Callable[[int, int, int], None]] = None):
        """BT 断点续传"""
        await self.download(handle, callback)

    def cancel(self, task_key: str):
        """取消下载"""
        if task_key in self._active_downloads:
            downloader = self._active_downloads[task_key]
            asyncio.create_task(downloader.stop())
