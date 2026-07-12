"""
Peer 通信协议实现 - BEP 3 / BEP 6 / BEP 10
纯自研 BT 协议实现，带超时管理和重试
"""

import asyncio
import struct
import hashlib
import random
from typing import Optional, List, Tuple, Dict, Set, Callable
from dataclasses import dataclass
import time

from ..logger import get_logger

log = get_logger('peer.protocol')


# BT 协议消息 ID
MSG_CHOKE = 0
MSG_UNCHOKE = 1
MSG_INTERESTED = 2
MSG_NOT_INTERESTED = 3
MSG_HAVE = 4
MSG_BITFIELD = 5
MSG_REQUEST = 6
MSG_PIECE = 7
MSG_CANCEL = 8
MSG_PORT = 9

# 默认块大小 (16KB)
DEFAULT_BLOCK_SIZE = 16 * 1024
# 最大请求队列
MAX_PIPELINED_REQUESTS = 5
# 最大 payload 大小
MAX_PAYLOAD_SIZE = 256 * 1024
# 默认超时
CONNECT_TIMEOUT = 15
HANDSHAKE_TIMEOUT = 10
READ_TIMEOUT = 60
WRITE_TIMEOUT = 30


@dataclass
class Peer:
    """Peer 信息"""
    ip: str
    port: int
    peer_id: Optional[bytes] = None
    am_choking: bool = True
    am_interested: bool = False
    peer_choking: bool = True
    peer_interested: bool = False
    bitfield: bytes = b''
    pieces_count: int = 0
    last_seen: float = 0.0
    speed: float = 0.0  # B/s
    fail_count: int = 0   # 连续失败次数

    def __hash__(self):
        return hash((self.ip, self.port))

    def __eq__(self, other):
        if not isinstance(other, Peer):
            return False
        return self.ip == other.ip and self.port == other.port

    @property
    def is_dead(self) -> bool:
        """连续失败超过阈值视为死节点"""
        return self.fail_count >= 3

    def has_piece(self, index: int) -> bool:
        if not self.bitfield or index >= self.pieces_count:
            return False
        byte_idx = index // 8
        bit_idx = 7 - (index % 8)
        if byte_idx >= len(self.bitfield):
            return False
        return bool(self.bitfield[byte_idx] & (1 << bit_idx))

    def pieces_available(self, total_pieces: int) -> List[int]:
        result = []
        for i in range(total_pieces):
            if self.has_piece(i):
                result.append(i)
        return result


class PeerConnection:
    """Peer TCP 连接 - 带超时和重试"""

    def __init__(self, peer: Peer, info_hash: bytes, peer_id: bytes):
        self.peer = peer
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._handshook = False
        self._am_choking = True
        self._peer_choking = True
        self._am_interested = False
        self._peer_interested = False
        self._bitfield = b''
        self._pieces = 0
        self._block_size = DEFAULT_BLOCK_SIZE
        self._download_callback: Optional[Callable[[int, int, bytes], None]] = None
        self._stats = {'bytes_read': 0, 'messages': 0, 'read_errors': 0}

    async def connect(self) -> bool:
        """连接到 Peer，返回是否成功"""
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.peer.ip, self.peer.port),
                timeout=CONNECT_TIMEOUT
            )
            self._connected = True
            await self._handshake()
            return True
        except (OSError, asyncio.TimeoutError, ConnectionRefusedError,
                ConnectionResetError, ConnectionAbortedError) as e:
            self.peer.fail_count += 1
            log.debug(f"Connection failed to {self.peer.ip}:{self.peer.port}: {e}")
            return False
        except Exception as e:
            self.peer.fail_count += 1
            log.warning(f"Unexpected connection error to {self.peer.ip}:{self.peer.port}: {e}")
            return False

    async def _handshake(self):
        """BT 握手协议 (BEP 3)"""
        pstr = b'BitTorrent protocol'
        reserved = b'\x00\x00\x00\x00\x00\x10\x00\x00'

        handshake = struct.pack('!B', len(pstr)) + pstr + reserved + self.info_hash + self.peer_id
        self.writer.write(handshake)
        await asyncio.wait_for(self.writer.drain(), timeout=WRITE_TIMEOUT)

        try:
            resp = await asyncio.wait_for(
                self.reader.readexactly(68),
                timeout=HANDSHAKE_TIMEOUT
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
            raise ConnectionError(f"Handshake failed: {e}")

        pstrlen = resp[0]
        if pstrlen != 19:
            raise ConnectionError(f"Invalid pstrlen: {pstrlen}")

        received_info_hash = resp[28:48]
        if received_info_hash != self.info_hash:
            raise ConnectionError("Info hash mismatch")

        received_peer_id = resp[48:68]
        self.peer.peer_id = received_peer_id
        self._handshook = True
        self.peer.last_seen = time.time()

        msg = await self._receive_message()
        if msg:
            msg_id, payload = msg
            if msg_id == MSG_BITFIELD:
                self._bitfield = payload
                self._pieces = sum(bin(b).count('1') for b in payload)
                self.peer.bitfield = payload
                self.peer.pieces_count = len(payload) * 8
            elif msg_id == MSG_UNCHOKE:
                self._peer_choking = False

    async def _receive_message(self) -> Optional[Tuple[Optional[int], bytes]]:
        """接收一条 BT 消息"""
        try:
            length_data = await asyncio.wait_for(
                self.reader.readexactly(4),
                timeout=READ_TIMEOUT
            )
            length = struct.unpack('!I', length_data)[0]

            if length == 0:
                return (None, b'')

            if length > MAX_PAYLOAD_SIZE:
                log.warning(f"Oversized message ({length}) from {self.peer.ip}:{self.peer.port}")
                return None

            data = await asyncio.wait_for(
                self.reader.readexactly(length),
                timeout=READ_TIMEOUT
            )

            self._stats['messages'] += 1
            self._stats['bytes_read'] += length + 4
            msg_id = data[0]
            payload = data[1:]

            if msg_id == MSG_CHOKE:
                self._peer_choking = True
            elif msg_id == MSG_UNCHOKE:
                self._peer_choking = False
            elif msg_id == MSG_INTERESTED:
                self._peer_interested = True
            elif msg_id == MSG_NOT_INTERESTED:
                self._peer_interested = False
            elif msg_id == MSG_HAVE:
                piece_index = struct.unpack('!I', payload[:4])[0]
                self._update_bitfield_for_have(piece_index)
            elif msg_id == MSG_BITFIELD:
                self._bitfield = payload
                self._pieces = sum(bin(b).count('1') for b in payload)
                self.peer.bitfield = payload
                self.peer.pieces_count = len(payload) * 8
            elif msg_id == MSG_PIECE:
                if self._download_callback:
                    index = struct.unpack('!I', payload[:4])[0]
                    begin = struct.unpack('!I', payload[4:8])[0]
                    block = payload[8:]
                    self._download_callback(index, begin, block)

            return (msg_id, payload)

        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionResetError) as e:
            self._stats['read_errors'] += 1
            return None
        except struct.error as e:
            log.debug(f"Struct error from {self.peer.ip}:{self.peer.port}: {e}")
            return None

    def _update_bitfield_for_have(self, piece_index: int):
        byte_idx = piece_index // 8
        bit_idx = 7 - (piece_index % 8)
        if byte_idx >= len(self._bitfield):
            self._bitfield = self._bitfield.ljust(byte_idx + 1, b'\x00')
        self._bitfield = (self._bitfield[:byte_idx] +
                          bytes([self._bitfield[byte_idx] | (1 << bit_idx)]) +
                          self._bitfield[byte_idx + 1:])
        self._pieces += 1

    async def send_choke(self):
        self._am_choking = True
        self.writer.write(struct.pack('!IB', 1, MSG_CHOKE))
        await self._safe_drain()

    async def send_unchoke(self):
        self._am_choking = False
        self.writer.write(struct.pack('!IB', 1, MSG_UNCHOKE))
        await self._safe_drain()

    async def send_interested(self):
        self._am_interested = True
        self.writer.write(struct.pack('!IB', 1, MSG_INTERESTED))
        await self._safe_drain()

    async def send_not_interested(self):
        self._am_interested = False
        self.writer.write(struct.pack('!IB', 1, MSG_NOT_INTERESTED))
        await self._safe_drain()

    async def send_request(self, piece_index: int, offset: int, length: int = DEFAULT_BLOCK_SIZE):
        payload = struct.pack('!III', piece_index, offset, length)
        msg = struct.pack('!IB', 13, MSG_REQUEST) + payload
        self.writer.write(msg)
        await self._safe_drain()

    async def send_have(self, piece_index: int):
        payload = struct.pack('!I', piece_index)
        msg = struct.pack('!IB', 5, MSG_HAVE) + payload
        self.writer.write(msg)
        await self._safe_drain()

    async def send_cancel(self, piece_index: int, offset: int, length: int):
        payload = struct.pack('!III', piece_index, offset, length)
        msg = struct.pack('!IB', 13, MSG_CANCEL) + payload
        self.writer.write(msg)
        await self._safe_drain()

    def set_download_callback(self, callback: Callable[[int, int, bytes], None]):
        self._download_callback = callback

    async def keep_alive(self):
        try:
            self.writer.write(b'\x00\x00\x00\x00')
            await self._safe_drain()
        except Exception:
            pass

    async def _safe_drain(self):
        """带超时的 drain，避免死等"""
        try:
            await asyncio.wait_for(self.writer.drain(), timeout=WRITE_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            raise ConnectionError("Write timeout")

    async def close(self):
        self._connected = False
        if self.writer:
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=5)
            except Exception:
                pass
            self.writer = None
        self.reader = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_handshook(self) -> bool:
        return self._handshook

    @property
    def is_choking(self) -> bool:
        return self._peer_choking

    @property
    def is_interested(self) -> bool:
        return self._am_interested

    @property
    def pieces(self) -> int:
        return self._pieces

    @property
    def bitfield(self) -> bytes:
        return self._bitfield

    @property
    def stats(self) -> dict:
        return dict(self._stats)
