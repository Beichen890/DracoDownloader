"""
BT 下载器 - 纯自研完整实现
支持磁力链接和 .torrent 文件，带连接池和重试
"""

import asyncio
import hashlib
import struct
import random
import os
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple, Callable
from dataclasses import dataclass, field
import time

from ..logger import get_logger
from .magnet import MagnetParser, MagnetLink
from .dht import DHTClient
from .peer import Peer, PeerConnection, DEFAULT_BLOCK_SIZE, MSG_UNCHOKE
from .bencode import decode_torrent, info_hash as calc_info_hash

log = get_logger('bittorrent.downloader')


@dataclass
class Piece:
    """Piece 信息"""
    index: int
    length: int
    hash: Optional[bytes] = None  # None = magnet link
    blocks: Dict[int, bytes] = field(default_factory=dict)
    downloaded: int = 0
    verified: bool = False
    failed_attempts: int = 0

    def add_block(self, offset: int, data: bytes):
        if offset in self.blocks:
            return
        self.blocks[offset] = data
        self.downloaded += len(data)

    def is_complete(self) -> bool:
        return self.downloaded >= self.length

    def verify(self) -> bool:
        if not self.is_complete():
            return False
        if self.hash is None:
            return True
        data = b''
        for offset in sorted(self.blocks.keys()):
            data += self.blocks[offset]
        data = data[:self.length]
        return hashlib.sha1(data).digest() == self.hash

    def get_data(self) -> bytes:
        data = b''
        for offset in sorted(self.blocks.keys()):
            data += self.blocks[offset]
        return data[:self.length]

    def reset(self):
        """重置 piece（校验失败后重试）"""
        self.blocks.clear()
        self.downloaded = 0
        self.failed_attempts += 1
        self.verified = False


@dataclass
class TorrentMeta:
    """种子元信息"""
    info_hash: bytes
    info_hash_hex: str
    name: str = ""
    piece_length: int = 0
    pieces_hashes: List[bytes] = field(default_factory=list)
    total_size: int = 0
    is_multi_file: bool = False
    files: List[Dict] = field(default_factory=list)
    trackers: List[str] = field(default_factory=list)

    @property
    def is_magnet(self) -> bool:
        return self.total_size == 0 or len(self.pieces_hashes) == 0


class ConnectionPool:
    """Peer 连接池 - 管理并发连接数"""

    def __init__(self, max_connections: int = 20):
        self._semaphore = asyncio.Semaphore(max_connections)
        self._connections: List[PeerConnection] = []
        self._max = max_connections

    async def acquire(self, peer: Peer, info_hash: bytes,
                      peer_id: bytes) -> Optional[PeerConnection]:
        """获取一个 peer 连接（带并发限制）"""
        if peer.is_dead:
            return None

        async with self._semaphore:
            conn = PeerConnection(peer, info_hash, peer_id)
            ok = await conn.connect()
            if ok and conn.is_handshook:
                self._connections.append(conn)
                return conn
            await conn.close()
            return None

    def release(self, conn: PeerConnection):
        """释放连接"""
        if conn in self._connections:
            self._connections.remove(conn)

    async def close_all(self):
        """关闭所有连接"""
        for conn in self._connections:
            await conn.close()
        self._connections.clear()

    @property
    def active_count(self) -> int:
        return len(self._connections)

    @property
    def remaining(self) -> int:
        return self._max - len(self._connections)


class BTDownloader:
    """自研 BitTorrent 下载器"""

    DEFAULT_PIECE_LENGTH = 256 * 1024
    MAX_RETRIES_PER_PIECE = 3
    MAX_CONNECTIONS = 20

    def __init__(self, source: str, output_path: str,
                 progress_callback: Optional[Callable[[int, int, int], None]] = None,
                 sequential: bool = False,
                 seeding_policy: Optional['SeedingPolicy'] = None,
                 extra_trackers: Optional[List[str]] = None):
        """初始化 BT 下载器

        Args:
            source: magnet 链接或 .torrent 文件路径
            output_path: 输出路径
            progress_callback: 进度回调 (downloaded, total, speed)
            sequential: 是否顺序下载（边下边看场景，牺牲稀缺优先策略）
            seeding_policy: 做种策略（None=不做种）
            extra_trackers: 额外的 tracker 列表（如 Web Tracker 合并结果）
        """
        self.source = source
        self.output_path = Path(output_path)
        self.progress_callback = progress_callback
        self.is_magnet = source.startswith('magnet:')
        self.sequential = sequential
        self.seeding_policy = seeding_policy
        self.extra_trackers = extra_trackers or []

        self.meta: Optional[TorrentMeta] = None

        self.pieces: Dict[int, Piece] = {}
        self._piece_count = 0
        self._piece_length = self.DEFAULT_PIECE_LENGTH
        self._total_size = 0
        self._downloaded = 0
        self._peer_id = self._generate_peer_id()
        self._running = False
        self._completed_pieces: Set[int] = set()
        self._requested_blocks: Set[Tuple[int, int]] = set()
        self._in_progress_pieces: Set[int] = set()

        # 稀缺分片优先策略数据结构
        self._piece_rarity: Dict[int, int] = {}  # piece_index → peer_count
        self._peer_piece_map: Dict[str, Set[int]] = {}  # peer_key → set of piece indices

        self.dht: Optional[DHTClient] = None
        self.peers: List[Peer] = []
        self._pool = ConnectionPool(self.MAX_CONNECTIONS)

        self._start_time = 0.0

        if self.is_magnet:
            self._parse_magnet()
        else:
            self._parse_torrent_file(source)

    def _generate_peer_id(self) -> bytes:
        prefix = b'-DD0001-'
        random_part = ''.join(random.choices(
            '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=12)).encode()
        return prefix + random_part

    def _parse_magnet(self):
        magnet = MagnetParser.parse(self.source)
        if not magnet:
            raise ValueError("Invalid magnet link")

        self.meta = TorrentMeta(
            info_hash=magnet.info_hash,
            info_hash_hex=magnet.info_hash_hex,
            name=magnet.display_name,
            trackers=magnet.trackers,
        )
        if not self.output_path.suffix:
            self.output_path = self.output_path / (magnet.display_name or 'download')

    def _parse_torrent_file(self, path: str):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"Torrent file not found: {path}")
        except PermissionError:
            raise PermissionError(f"Cannot read torrent file: {path}")

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

        self.meta = TorrentMeta(
            info_hash=info_hash,
            info_hash_hex=info_hash.hex(),
            name=name,
            piece_length=info.get('piece length', 0),
            pieces_hashes=pieces_hashes,
            total_size=total_size,
            is_multi_file=is_multi,
            files=files,
            trackers=torrent.get('announce-list', [torrent.get('announce', '')]),
        )
        if not self.output_path.suffix:
            self.output_path = self.output_path / name

    async def start(self):
        """开始下载"""
        if not self.meta:
            raise RuntimeError("No metadata loaded")

        self._running = True
        self._start_time = time.time()

        self._total_size = self.meta.total_size
        self._piece_length = self.meta.piece_length or self.DEFAULT_PIECE_LENGTH
        self._piece_count = len(self.meta.pieces_hashes)

        is_magnet = self.meta.is_magnet

        # 磁力链接允许 total_size=0
        if not is_magnet and self._total_size == 0:
            raise ValueError("Invalid torrent: zero size")

        # 初始化 pieces
        if not is_magnet:
            for idx, phash in enumerate(self.meta.pieces_hashes):
                start = idx * self._piece_length
                length = min(self._piece_length, self._total_size - start) if self._piece_length > 0 else 0
                self.pieces[idx] = Piece(index=idx, length=length, hash=phash)

        # 创建输出文件
        self._create_output_file()

        # 启动 DHT
        self.dht = DHTClient()
        try:
            await asyncio.wait_for(self.dht.start(), timeout=10)
        except (asyncio.TimeoutError, OSError) as e:
            log.warning(f"DHT start timeout: {e}")

        # 发现 peers
        log.info(f"Finding peers for {self.meta.info_hash_hex[:12]}...")
        found = await self._discover_peers()
        self.peers = found
        log.info(f"Found {len(self.peers)} peers")

        # 下载循环
        await self._download_loop()

        # 清理
        await self._cleanup()
        if self._piece_count > 0:
            log.info(f"Download complete: {len(self._completed_pieces)}/{self._piece_count} pieces")

        # 做种阶段（若启用）
        if self.seeding_policy is not None and self.seeding_policy.enabled:
            await self._seed_phase()

    async def _discover_peers(self) -> List[Peer]:
        """发现 peers（DHT + HTTP tracker + 额外 tracker）"""
        found_set: Set[Tuple[str, int]] = set()

        # 合并种子自带 tracker 和额外 tracker（如 Web Tracker）
        all_trackers: List[str] = []
        for t in self.meta.trackers:
            if isinstance(t, bytes):
                t = t.decode('utf-8', errors='replace')
            if t:
                all_trackers.append(t)
        for t in self.extra_trackers:
            if isinstance(t, bytes):
                t = t.decode('utf-8', errors='replace')
            if t and t not in all_trackers:
                all_trackers.append(t)

        # DHT
        if self.dht:
            try:
                dht_peers = await asyncio.wait_for(
                    self.dht.find_peers(self.meta.info_hash, max_peers=50),
                    timeout=15
                )
                for p in dht_peers:
                    found_set.add(p)
                log.debug(f"DHT found {len(dht_peers)} peers")
            except (asyncio.TimeoutError, Exception) as e:
                log.debug(f"DHT peer discovery: {e}")

        # HTTP trackers (包括自带的和额外的)
        for tracker_url in all_trackers:
            if isinstance(tracker_url, bytes):
                tracker_url = tracker_url.decode('utf-8', errors='replace')
            if tracker_url and tracker_url.startswith('http'):
                try:
                    peers = await asyncio.wait_for(
                        self._tracker_announce(tracker_url),
                        timeout=8
                    )
                    for p in peers:
                        found_set.add(p)
                except (asyncio.TimeoutError, Exception) as e:
                    log.debug(f"Tracker {tracker_url.split('/')[2] if '://' in tracker_url else tracker_url}: {e}")

        # 去重、过滤无效 IP
        result = []
        for ip, port in found_set:
            if port <= 0 or port > 65535:
                continue
            if ip.startswith(('0.', '127.', '255.')):
                continue
            result.append(Peer(ip=ip, port=port))

        return result

    async def _tracker_announce(self, tracker_url: str) -> List[Tuple[str, int]]:
        """HTTP tracker announce"""
        import urllib.parse
        import aiohttp
        from .bencode import decode as bencode_decode

        params = {
            'info_hash': self.meta.info_hash,
            'peer_id': self._peer_id,
            'port': self.dht.port if self.dht else 6881,
            'uploaded': 0,
            'downloaded': self._downloaded,
            'left': max(0, self._total_size - self._downloaded),
            'compact': 1,
            'event': 'started',
        }

        encoded_params = []
        for k, v in params.items():
            if isinstance(v, bytes):
                encoded_v = ''.join(f'%{b:02x}' for b in v)
            else:
                encoded_v = str(v)
            encoded_params.append(f'{k}={encoded_v}')

        url = tracker_url + '?' + '&'.join(encoded_params)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url, headers={'User-Agent': 'DracoDownloader/1.0'}) as resp:
                if resp.status != 200:
                    return []
                data = await resp.read()
                response = bencode_decode(data) if data else {}
                if isinstance(response, dict):
                    peers_raw = response.get('peers', b'')
                    if isinstance(peers_raw, bytes):
                        peers = []
                        for i in range(0, len(peers_raw), 6):
                            if i + 6 <= len(peers_raw):
                                ip = '.'.join(str(b) for b in peers_raw[i:i+4])
                                port = struct.unpack('!H', peers_raw[i+4:i+6])[0]
                                peers.append((ip, port))
                        return peers
        return []

    def _create_output_file(self):
        """创建输出文件"""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        if self.meta.is_magnet:
            with open(self.output_path, 'wb') as f:
                pass
        elif self.meta.is_multi_file:
            for file_info in self.meta.files:
                file_path = self.output_path.parent / file_info['path']
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'wb') as f:
                    try:
                        f.truncate(file_info['length'])
                    except OSError:
                        f.write(b'\x00' * file_info['length'])
        else:
            with open(self.output_path, 'wb') as f:
                f.truncate(self._total_size)

    async def _download_loop(self):
        """主下载循环 - 连接 peers 并请求 pieces"""
        if not self.peers:
            log.warning("No peers found, cannot download")
            return

        # 创建连接任务
        connect_tasks = []
        for peer in self.peers[:self.MAX_CONNECTIONS]:
            connect_tasks.append(self._handle_peer(peer))

        # 并发连接 peers
        done = await asyncio.gather(*connect_tasks, return_exceptions=True)

        # 统计连接结果
        connected = sum(1 for r in done if r is True)
        log.info(f"Connected to {connected}/{len(connect_tasks)} peers")

        # 等待剩余下载完成
        max_wait = 30 if self.meta.is_magnet else 15
        waited = 0
        while self._running and waited < max_wait:
            if self._piece_count > 0 and len(self._completed_pieces) >= self._piece_count:
                break
            # 如果长时间无进展则退出
            if waited >= max_wait - 5 and len(self._completed_pieces) == 0:
                break
            await asyncio.sleep(1)
            self._report_progress()
            waited += 1

    def _update_rarity_from_peer(self, peer: Peer):
        """
        根据 peer 的 bitfield 更新稀缺度统计

        统计拥有每个 piece 的 peer 数量，实现稀缺分片优先策略。
        稀缺度 = 拥有该 piece 的 peer 数量，值越低越稀缺。
        """
        peer_key = f"{peer.ip}:{peer.port}"
        has_pieces = set()

        for idx in range(self._piece_count):
            if peer.has_piece(idx):
                has_pieces.add(idx)

        # 移除旧的记录
        old_pieces = self._peer_piece_map.get(peer_key, set())
        for idx in old_pieces:
            if idx in self._piece_rarity:
                self._piece_rarity[idx] = max(0, self._piece_rarity[idx] - 1)

        # 添加新的记录
        for idx in has_pieces:
            self._piece_rarity[idx] = self._piece_rarity.get(idx, 0) + 1

        self._peer_piece_map[peer_key] = has_pieces

    def _select_rarest_piece(self, peer: Peer) -> Optional[int]:
        """
        稀缺分片优先算法 - 选择 peer 拥有但下载最少的 piece

        策略优先级：
        1. 顺序模式（self.sequential=True）→ 按索引顺序选第一个未完成 piece
        2. 稀缺优先：选择 peer 拥有且最稀缺的 piece
        3. 若无稀缺数据，回退到顺序选择
        4. 跳过已完成或 in-progress 的 piece
        """
        # 顺序模式：边下边看场景，按 piece 索引顺序下载
        if self.sequential:
            for idx in range(self._piece_count):
                if idx in self._completed_pieces:
                    continue
                if idx in self._in_progress_pieces:
                    continue
                if idx not in self.pieces:
                    continue
                if peer.has_piece(idx):
                    return idx
            return None

        # 收集该 peer 可用的、未完成的 pieces
        candidates = []
        for idx in range(self._piece_count):
            if idx in self._completed_pieces:
                continue
            if idx in self._in_progress_pieces:
                continue
            if idx not in self.pieces:
                continue
            if not peer.has_piece(idx):
                continue

            # 稀缺度：值越低越稀缺
            rarity = self._piece_rarity.get(idx, self._piece_count)
            candidates.append((rarity, idx))

        if not candidates:
            return None

        # 按稀缺度升序排序（最稀缺的排最前）
        candidates.sort(key=lambda x: (x[0], x[1]))
        selected = candidates[0][1]
        return selected

    async def _handle_peer(self, peer: Peer) -> bool:
        """处理单个 peer 连接和下载"""
        conn = await self._pool.acquire(peer, self.meta.info_hash, self._peer_id)
        if conn is None:
            return False

        conn.set_download_callback(self._on_piece_data)

        try:
            # 发送 interested
            await conn.send_interested()

            # 等待 unchoke（最多 15s）
            unchoked = False
            for _ in range(15):
                msg = await conn._receive_message()
                if msg is None:
                    break
                if msg[0] == MSG_UNCHOKE:
                    unchoked = True
                    break

            if not unchoked:
                return True  # connected but choked, still useful

            # 更新稀缺度
            self._update_rarity_from_peer(peer)

            # 稀缺分片优先策略请求 pieces
            requested_count = 0
            while self._running and requested_count < self._piece_count:
                idx = self._select_rarest_piece(peer)
                if idx is None:
                    break  # 没有更多可请求的 piece

                self._in_progress_pieces.add(idx)
                piece = self.pieces[idx]
                await self._request_piece(conn, piece)
                requested_count += 1

            return True

        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            log.debug(f"Peer {peer.ip}:{peer.port} error: {e}")
            return False
        finally:
            self._pool.release(conn)
            await conn.close()

    def _update_piece_count_from_peer(self, n_pieces: int):
        """从 peer bitfield 更新 piece 数（磁力链接）"""
        if not self.meta.is_magnet:
            return
        if n_pieces <= self._piece_count:
            return

        old_count = self._piece_count
        self._piece_count = n_pieces
        self._total_size = n_pieces * self._piece_length

        for idx in range(old_count, n_pieces):
            if idx not in self.pieces:
                self.pieces[idx] = Piece(index=idx, length=self._piece_length, hash=None)

        log.info(f"Updated piece count: {n_pieces} (from peer)")

    def _ensure_piece(self, index: int, block_size: int = DEFAULT_BLOCK_SIZE):
        if index not in self.pieces:
            self.pieces[index] = Piece(index=index, length=self._piece_length, hash=None)
            if index >= self._piece_count:
                self._piece_count = index + 1
                self._total_size = max(self._total_size, (index + 1) * self._piece_length)

    async def _request_piece(self, conn: PeerConnection, piece: Piece):
        """请求一个 piece 的所有块"""
        remaining = piece.length
        offset = 0
        block_size = DEFAULT_BLOCK_SIZE

        while remaining > 0 and self._running and conn.is_connected:
            if conn.is_choking:
                await asyncio.sleep(0.5)
                continue

            size = min(block_size, remaining)
            key = (piece.index, offset)
            if key not in self._requested_blocks:
                self._requested_blocks.add(key)
                try:
                    await conn.send_request(piece.index, offset, size)
                except (ConnectionError, OSError):
                    break

            offset += size
            remaining -= size

            try:
                msg = await conn._receive_message()
                if msg is None:
                    break
            except Exception:
                break

    def _on_piece_data(self, index: int, offset: int, data: bytes):
        """接收到 piece 数据回调"""
        if index not in self.pieces:
            self._ensure_piece(index, len(data))

        piece = self.pieces[index]

        expected_size = min(DEFAULT_BLOCK_SIZE, piece.length - offset)
        if len(data) != expected_size:
            log.warning(f"Piece {index} block size mismatch: expected {expected_size}, got {len(data)}")
            return

        if offset in piece.blocks:
            log.debug(f"Piece {index} block at offset {offset} already exists")
            return

        piece.add_block(offset, data)
        self._downloaded += len(data)

        if piece.is_complete() and index not in self._completed_pieces:
            if piece.verify():
                self._completed_pieces.add(index)
                self._in_progress_pieces.discard(index)
                self._write_piece(index, piece)
                self._report_progress()
                log.info(f"Piece {index}/{self._piece_count - 1} completed")
            else:
                log.warning(f"Piece {index} hash mismatch, retrying")
                piece.reset()
                if piece.failed_attempts >= self.MAX_RETRIES_PER_PIECE:
                    log.error(f"Piece {index} failed after {piece.failed_attempts} attempts")
                    self._in_progress_pieces.discard(index)

    def _write_piece(self, index: int, piece: Piece):
        """写入 piece 到文件"""
        data = piece.get_data()
        if not data:
            return

        if self.meta.is_multi_file:
            offset = index * self._piece_length
            self._write_to_files(offset, data)
        else:
            try:
                with open(self.output_path, 'r+b') as f:
                    seek_pos = index * self._piece_length
                    f.seek(seek_pos)
                    f.write(data)
            except (FileNotFoundError, OSError):
                with open(self.output_path, 'wb') as f:
                    f.seek(index * self._piece_length)
                    f.write(data)

    def _write_to_files(self, offset: int, data: bytes):
        """多文件写入"""
        data_end = offset + len(data)

        for i, file_info in enumerate(self.meta.files):
            file_path = self.output_path.parent / file_info['path']
            f_start = sum(f['length'] for f in self.meta.files[:i])
            f_end = f_start + file_info['length']

            if data_end <= f_start:
                break
            if offset >= f_end:
                continue

            overlap_start = max(offset, f_start)
            overlap_end = min(data_end, f_end)

            if overlap_end <= overlap_start:
                continue

            file_write_start = overlap_start - f_start
            file_write_end = overlap_end - f_start
            data_read_start = overlap_start - offset
            data_read_end = overlap_end - offset

            file_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                with open(file_path, 'r+b') as f:
                    f.seek(file_write_start)
                    f.write(data[data_read_start:data_read_end])
            except FileNotFoundError:
                with open(file_path, 'wb') as f:
                    f.write(b'\x00' * file_info['length'])
                    f.seek(file_write_start)
                    f.write(data[data_read_start:data_read_end])

    def _report_progress(self):
        """报告进度"""
        if self._piece_count > 0:
            pct = len(self._completed_pieces) / self._piece_count * 100
            elapsed = time.time() - self._start_time
            speed = self._downloaded / elapsed if elapsed > 0 else 0
            if self._total_size > 0:
                total_mb = self._total_size / (1024 * 1024)
                downloaded_mb = self._downloaded / (1024 * 1024)
                log.info(f"{pct:.1f}% ({downloaded_mb:.1f}/{total_mb:.1f} MB) "
                        f"{speed/1024:.1f} KB/s "
                        f"peers={self._pool.active_count}")
            else:
                log.info(f"{self._downloaded / 1024:.1f} KB @ {speed/1024:.1f} KB/s")
            if self.progress_callback:
                self.progress_callback(self._downloaded,
                                       self._total_size or self._downloaded,
                                       int(speed))
        elif self._downloaded > 0:
            elapsed = time.time() - self._start_time
            speed = self._downloaded / elapsed if elapsed > 0 else 0
            if self.progress_callback:
                self.progress_callback(self._downloaded, self._downloaded, int(speed))

    async def stop(self):
        """停止下载"""
        self._running = False
        await self._cleanup()

    async def _seed_phase(self):
        """做种阶段 - 根据策略做种，完成后退出"""
        from .seeding import SeedingController
        controller = SeedingController(self.seeding_policy)
        controller.start(downloaded=self._total_size)
        log.info(
            f"开始做种 (ratio_limit={self.seeding_policy.ratio_limit}, "
            f"time_limit={self.seeding_policy.time_limit}s)"
        )
        # 做种期间不主动断开 peer 连接（实际 BT 做种需要实现上传协议，
        # 当前自研栈以下载为主，这里实现时长/分享率门控逻辑）
        try:
            await controller.wait_until_stop(
                poll_interval=5.0,
                upload_reader=None,
            )
        except asyncio.CancelledError:
            log.info("做种被取消")
            raise
        finally:
            log.info("做种阶段结束")

    async def _cleanup(self):
        """清理资源"""
        self._running = False
        await self._pool.close_all()
        if self.dht:
            await self.dht.close()
            self.dht = None

    @property
    def progress(self) -> float:
        if self._total_size == 0:
            return 0
        return self._downloaded / self._total_size * 100

    @property
    def speed(self) -> int:
        elapsed = time.time() - self._start_time
        if elapsed < 1:
            return 0
        return int(self._downloaded / elapsed)

    @property
    def is_running(self) -> bool:
        return self._running
