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
from ..bittorrent.loaders import resolve as resolve_source, ResolvedTorrent
from ..bittorrent.trackers import enrich_trackers
from ..bittorrent.seeding import SeedingPolicy
from ..config import get_global_config
from ..logger import get_logger

log = get_logger('protocols.torrent')


class TorrentDriver(ProtocolDriver):
    """
    BitTorrent 驱动 - 纯自研实现

    使用自研的 BTDownloader (DHT Kademlia + Peer Wire Protocol)
    完全自主开发，无任何 GPL 依赖

    支持:
    - 多源加载（本地文件 / HTTP URL / 磁力链接）
    - Web Tracker 自动合并
    - 顺序下载（边下边看）
    - 做种策略（分享率/时长限制）
    """

    def __init__(self):
        self._active_downloads: Dict[str, BTDownloader] = {}

    def match(self, url: str) -> bool:
        return url.startswith('magnet:') or url.endswith('.torrent')

    async def probe(self, url: str) -> Dict[str, Any]:
        """探测种子信息（使用统一的多源加载器）"""
        try:
            resolved: ResolvedTorrent = await resolve_source(url)
        except Exception as e:
            log.warning(f"探测失败，回退到旧路径: {e}")
            return await self._legacy_probe(url)

        if resolved.is_magnet:
            return {
                'size': 0,
                'filename': resolved.name or 'magnet_download',
                'is_torrent': True,
                'info_hash': resolved.info_hash_hex,
                'has_files': False,
            }

        return {
            'size': resolved.total_size,
            'filename': resolved.name,
            'is_torrent': True,
            'files': resolved.files,
            'piece_count': len(resolved.pieces_hashes),
            'piece_length': resolved.piece_length,
            'has_files': bool(resolved.files),
            'trackers': resolved.trackers,
        }

    async def _legacy_probe(self, url: str) -> Dict[str, Any]:
        """旧版探测（兜底）"""
        if url.startswith('magnet:'):
            magnet = MagnetParser.parse(url)
            if not magnet:
                raise ValueError("Invalid magnet link")
            return {
                'size': 0,
                'filename': magnet.display_name or 'magnet_download',
                'is_torrent': True,
                'info_hash': magnet.info_hash_hex,
                'has_files': False,
            }
        else:
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
        config = get_global_config()
        output_path = Path(handle.output_path)

        # 获取额外 tracker（Web Tracker 合并）
        extra_trackers: list[str] = []
        if config.get('bt_web_trackers_enabled'):
            try:
                # 从 handle.metadata 或重新 probe 获取自带 tracker
                own_trackers = []
                if handle.metadata:
                    own_trackers = handle.metadata.get('trackers', [])
                extra_trackers = await enrich_trackers(
                    own_trackers,
                    enable_web=True,
                    announce_to_all=True,
                )
                log.info(f"合并后 tracker 数: {len(extra_trackers)}")
            except Exception as e:
                log.debug(f"Web tracker 合并失败: {e}")

        # 做种策略
        seeding_policy = None
        if config.get('bt_seeding_enabled'):
            seeding_policy = SeedingPolicy(
                enabled=True,
                ratio_limit=config.get('bt_seeding_ratio_limit'),
                time_limit=config.get('bt_seeding_time_limit'),
            )

        # 创建下载器
        downloader = BTDownloader(
            source=handle.url,
            output_path=str(output_path),
            progress_callback=callback,
            sequential=config.get('bt_enable_sequential'),
            seeding_policy=seeding_policy,
            extra_trackers=extra_trackers,
        )

        # 存储活跃下载
        task_key = str(handle.url) + str(output_path)
        self._active_downloads[task_key] = downloader

        try:
            await downloader.start()
        finally:
            self._active_downloads.pop(task_key, None)

        # BT 下载完成后，检查文件是否存在
        if not output_path.exists():
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
