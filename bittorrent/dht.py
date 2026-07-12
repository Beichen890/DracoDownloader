"""
DHT (Kademlia) 网络实现 - BEP 5 / BEP 9
纯自研实现，使用 bencoding 通信
"""

import asyncio
import socket
import random
import hashlib
import struct
import os
from typing import Optional, Dict, List, Tuple, Set, Callable
from dataclasses import dataclass, field
import time

from .bencode import encode, decode
from ..logger import get_logger

log = get_logger('bittorrent.dht')

# 默认 DHT 引导节点 (BEP 5 / BEP 42)
# 可通过环境变量 DRACO_DHT_BOOTSTRAP_NODES 覆盖
# 格式: "host1:port1,host2:port2,..."
DEFAULT_BOOTSTRAP_NODES = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]


def _parse_bootstrap_nodes(value: Optional[str] = None) -> List[Tuple[str, int]]:
    """
    解析引导节点列表

    来源优先级:
    1. 显式传入 (已由调用方处理)
    2. 环境变量 DRACO_DHT_BOOTSTRAP_NODES
    3. 默认列表

    Args:
        value: 可选的逗号分隔节点字符串 "host:port,..."

    Returns:
        节点列表 [(host, port), ...]
    """
    if value is None:
        value = os.environ.get('DRACO_DHT_BOOTSTRAP_NODES', '')

    if not value:
        return DEFAULT_BOOTSTRAP_NODES

    nodes = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue
        if ':' in part:
            host, port_str = part.rsplit(':', 1)
            try:
                port = int(port_str.strip())
                nodes.append((host.strip(), port))
            except ValueError:
                log.warning(f"Invalid bootstrap node: {part}")
    return nodes or DEFAULT_BOOTSTRAP_NODES

# DHT KRPC 消息类型
QUERY = "q"
RESPONSE = "r"
ERROR = "e"

# DHT 节点 ID 长度 (160 bit = 20 bytes)
NODE_ID_LENGTH = 20
# Kademlia K 值 (每个 bucket 最多 K 个节点)
K = 8
# 并发查找 α 值
ALPHA = 3
# Bucket 大小
BUCKET_SIZE = 20


@dataclass
class Node:
    """DHT 节点"""
    id: bytes
    ip: str
    port: int

    def __hash__(self):
        return hash((self.ip, self.port))

    def __eq__(self, other):
        if not isinstance(other, Node):
            return False
        return self.ip == other.ip and self.port == other.port

    def to_compact(self) -> bytes:
        """紧凑格式: 节点ID(20) + IP(4) + Port(2)"""
        try:
            ip_bytes = socket.inet_aton(self.ip)
        except OSError:
            ip_bytes = b'\x00' * 4
        port_bytes = struct.pack('!H', self.port)
        return self.id + ip_bytes + port_bytes


@dataclass
class RoutingTable:
    """Kademlia 路由表"""
    own_id: bytes
    buckets: List[List[Node]] = field(default_factory=list)

    def __post_init__(self):
        # 初始创建 160 个 bucket
        self.buckets = [[] for _ in range(NODE_ID_LENGTH * 8)]

    def bucket_index(self, node_id: bytes) -> int:
        """计算节点 ID 对应的 bucket 索引 (XOR 距离最高位)"""
        xor = bytes(a ^ b for a, b in zip(self.own_id, node_id))
        if xor == b'\x00' * NODE_ID_LENGTH:
            return 0
        for i, byte in enumerate(xor):
            if byte != 0:
                bit = 8 - (byte.bit_length() - 1)
                return i * 8 + bit
        return 0

    def add_node(self, node: Node) -> bool:
        """添加节点到路由表"""
        if node.id == self.own_id or node.id == b'\x00' * 20:
            return False
        idx = self.bucket_index(node.id)
        bucket = self.buckets[idx]

        # 检查是否已存在
        for n in bucket:
            if n.id == node.id:
                n.last_seen = time.time()
                return True

        # 如果 bucket 未满，直接添加
        if len(bucket) < BUCKET_SIZE:
            node.last_seen = time.time()
            bucket.append(node)
            return True

        return False

    def get_closest_nodes(self, target_id: bytes, count: int = K) -> List[Node]:
        """获取最接近目标的 K 个节点"""
        candidates = []
        for bucket in self.buckets:
            candidates.extend(bucket)

        # 按 XOR 距离排序
        def distance(node: Node) -> int:
            xor = bytes(a ^ b for a, b in zip(node.id, target_id))
            return int.from_bytes(xor, 'big')

        candidates.sort(key=distance)
        return candidates[:count]

    def remove_node(self, node: Node) -> bool:
        """从路由表中移除节点"""
        for bucket in self.buckets:
            for i, n in enumerate(bucket):
                if n.id == node.id:
                    bucket.pop(i)
                    return True
        return False


class DHTProtocol(asyncio.DatagramProtocol):
    """DHT KRPC 协议实现"""

    def __init__(self, dht: 'DHTClient'):
        self.dht = dht
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        self.transport = None

    def datagram_received(self, data, addr):
        """接收 UDP 数据包"""
        if self.dht._running:
            asyncio.create_task(self.dht.handle_message(data, addr))

    def send_message(self, data: bytes, addr: Tuple[str, int]):
        """发送 UDP 消息"""
        if self.transport:
            self.transport.sendto(data, addr)


class DHTClient:
    """DHT 客户端 - 纯自研 Kademlia 实现"""

    def __init__(self, bootstrap_nodes: List[Tuple[str, int]] = None):
        self.node_id = self._generate_node_id()
        self.routing_table = RoutingTable(self.node_id)
        self.transactions: Dict[str, asyncio.Future] = {}
        self._transaction_counter = 0
        self._port = 0
        self._protocol: Optional[DHTProtocol] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._running = False
        self._bootstrap_done = False
        self._last_bootstrap = 0.0

        # 引导节点：构造函数参数 > 环境变量 > 默认值
        if bootstrap_nodes is not None:
            self.bootstrap_nodes = bootstrap_nodes
        else:
            self.bootstrap_nodes = _parse_bootstrap_nodes()
        log.info(f"DHT bootstrap nodes: {self.bootstrap_nodes}")

    def _generate_node_id(self) -> bytes:
        """生成随机节点 ID"""
        return hashlib.sha1(random.randbytes(20)).digest()

    def _generate_transaction_id(self) -> str:
        """生成事务 ID (bencodable string)"""
        self._transaction_counter += 1
        return f"t{self._transaction_counter}"

    async def start(self, port: int = 0):
        """启动 DHT 服务器"""
        self._port = port or random.randint(49152, 65535)
        self._protocol = DHTProtocol(self)

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self._protocol,
            local_addr=('0.0.0.0', self._port)
        )
        self._transport = transport
        self._running = True

        # 加入 DHT 网络
        await self.bootstrap()

    async def bootstrap(self):
        """通过引导节点加入 DHT 网络"""
        now = time.time()
        if self._bootstrap_done and now - self._last_bootstrap < 60:
            return
        self._last_bootstrap = now

        results = await asyncio.gather(*[
            self._bootstrap_node(ip, port)
            for ip, port in self.bootstrap_nodes
        ], return_exceptions=True)

        node_count = 0
        for result in results:
            if isinstance(result, list):
                for node in result:
                    if self.routing_table.add_node(node):
                        node_count += 1
            elif isinstance(result, asyncio.TimeoutError):
                pass  # Expected for unreachable nodes
            elif isinstance(result, (OSError, ConnectionError)):
                pass  # Network issues

        if node_count > 0:
            log.info(f"DHT bootstrap: {node_count} nodes in routing table")
        self._bootstrap_done = True

    async def _bootstrap_node(self, ip: str, port: int) -> List[Node]:
        """向单个引导节点发送 find_node"""
        try:
            addr = (ip, port)
            result = await self.find_node(self.node_id, addr)
            if 'nodes' in result and result['nodes']:
                return decode_nodes_compact(result['nodes'])
            if 'values' in result:
                return []
        except Exception:
            pass
        return []

    async def find_node(self, target_id: bytes, addr: Tuple[str, int]) -> Dict:
        """find_node 查询 - 目标可以是节点 ID 或 info_hash"""
        tid = self._generate_transaction_id()

        # 构建 bencoded 请求 (BEP 5)
        msg = {
            "t": tid,
            "y": QUERY,
            "q": "find_node",
            "a": {
                "id": self.node_id,
                "target": target_id,
            }
        }

        data = encode(msg)
        self._protocol.send_message(data, addr)

        future = asyncio.get_running_loop().create_future()
        self.transactions[tid] = future

        try:
            result = await asyncio.wait_for(future, timeout=3)
            return result or {}
        except (asyncio.TimeoutError, Exception):
            return {}

    async def get_peers(self, info_hash: bytes, addr: Tuple[str, int]) -> Dict:
        """get_peers 查询 (BEP 5) - 获取拥有 info_hash 的 peers"""
        tid = self._generate_transaction_id()

        msg = {
            "t": tid,
            "y": QUERY,
            "q": "get_peers",
            "a": {
                "id": self.node_id,
                "info_hash": info_hash,
            }
        }

        data = encode(msg)
        self._protocol.send_message(data, addr)

        future = asyncio.get_running_loop().create_future()
        self.transactions[tid] = future

        try:
            result = await asyncio.wait_for(future, timeout=3)
            return result or {}
        except (asyncio.TimeoutError, Exception):
            return {}

    async def announce_peer(self, info_hash: bytes, addr: Tuple[str, int],
                            token: bytes, implied_port: int = 1):
        """announce_peer 查询 (BEP 5)"""
        tid = self._generate_transaction_id()

        msg = {
            "t": tid,
            "y": QUERY,
            "q": "announce_peer",
            "a": {
                "id": self.node_id,
                "info_hash": info_hash,
                "port": self._port,
                "token": token,
                "implied_port": implied_port,
            }
        }

        data = encode(msg)
        self._protocol.send_message(data, addr)

    async def ping(self, addr: Tuple[str, int]) -> bool:
        """ping 节点"""
        tid = self._generate_transaction_id()

        msg = {
            "t": tid,
            "y": QUERY,
            "q": "ping",
            "a": {
                "id": self.node_id,
            }
        }

        data = encode(msg)
        self._protocol.send_message(data, addr)

        future = asyncio.get_running_loop().create_future()
        self.transactions[tid] = future

        try:
            await asyncio.wait_for(future, timeout=3)
            return True
        except asyncio.TimeoutError:
            return False

    async def handle_message(self, raw_data: bytes, addr: Tuple[str, int]):
        """处理收到的 DHT 消息"""
        try:
            msg = decode(raw_data)
            if not isinstance(msg, dict):
                return
        except Exception:
            return

        msg_type = msg.get('y')
        tid = msg.get('t')
        if not msg_type or not tid:
            return
        # 确保 tid 是字符串
        if isinstance(tid, bytes):
            tid = tid.decode('utf-8', errors='replace')

        # 将节点加入路由表
        if 'a' in msg and 'id' in msg['a']:
            node_id = msg['a']['id']
            if isinstance(node_id, bytes) and len(node_id) == 20:
                peer_node = Node(node_id, addr[0], addr[1])
                self.routing_table.add_node(peer_node)

        if msg_type == RESPONSE:
            # 响应消息
            if tid in self.transactions and not self.transactions[tid].done():
                rdata = msg.get('r', {})
                if isinstance(rdata, dict):
                    # 解码 nodes 字段 (从 bytes)
                    if 'nodes' in rdata and isinstance(rdata['nodes'], bytes):
                        pass  # already bytes from bencode
                    # 解码 values (peers)
                    if 'values' in rdata and isinstance(rdata['values'], list):
                        pass  # already list
                self.transactions[tid].set_result(rdata)

        elif msg_type == QUERY:
            # 查询消息
            query = msg.get('q')
            a_data = msg.get('a', {})

            if query == 'ping':
                response = {
                    "t": tid,
                    "y": RESPONSE,
                    "r": {"id": self.node_id}
                }
                self._protocol.send_message(encode(response), addr)

            elif query == 'find_node':
                target = a_data.get('target', b'')
                if isinstance(target, bytes) and len(target) == 20:
                    nodes = self.routing_table.get_closest_nodes(target, K)
                    response = {
                        "t": tid,
                        "y": RESPONSE,
                        "r": {
                            "id": self.node_id,
                            "nodes": encode_nodes_compact(nodes),
                        }
                    }
                    self._protocol.send_message(encode(response), addr)

            elif query == 'get_peers':
                info_hash = a_data.get('info_hash', b'')
                if isinstance(info_hash, bytes) and len(info_hash) == 20:
                    # 返回 token (简化: 使用 info_hash 的前4字节)
                    token = hashlib.sha1(self.node_id + info_hash).digest()[:4]
                    nodes = self.routing_table.get_closest_nodes(info_hash, K)
                    response = {
                        "t": tid,
                        "y": RESPONSE,
                        "r": {
                            "id": self.node_id,
                            "nodes": encode_nodes_compact(nodes),
                            "token": token,
                        }
                    }
                    self._protocol.send_message(encode(response), addr)

    async def find_peers(self, info_hash: bytes, max_peers: int = 50) -> List[Tuple[str, int]]:
        """通过 DHT 网络递归查找拥有指定 info_hash 的 peers"""
        found_peers: List[Tuple[str, int]] = []
        queried: Set[str] = set()
        to_query: List[Node] = []

        # 从路由表找最接近的节点
        for node in self.routing_table.get_closest_nodes(info_hash, K * 2):
            key = f"{node.ip}:{node.port}"
            if key not in queried:
                to_query.append(node)
                queried.add(key)

        # 迭代查找
        for _ in range(8):  # 最多 8 轮
            if not to_query or len(found_peers) >= max_peers:
                break

            batch = to_query[:ALPHA]
            to_query = to_query[ALPHA:]

            tasks = []
            for node in batch:
                tasks.append(self._query_get_peers(info_hash, node))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            new_nodes = []
            for result in results:
                if isinstance(result, Exception):
                    continue
                if result is None:
                    continue
                peers, nodes = result

                # 收集 peers
                for p in peers:
                    if p not in found_peers:
                        found_peers.append(p)
                        if len(found_peers) >= max_peers:
                            break

                # 收集新节点
                for n in nodes:
                    key = f"{n.ip}:{n.port}"
                    if key not in queried:
                        new_nodes.append(n)
                        queried.add(key)
                        self.routing_table.add_node(n)

            to_query.extend(new_nodes)

        return found_peers

    async def _query_get_peers(self, info_hash: bytes,
                                node: Node) -> Optional[Tuple[List[Tuple[str, int]], List[Node]]]:
        """向单个节点查询 get_peers"""
        try:
            result = await self.get_peers(info_hash, (node.ip, node.port))
            peers = []
            nodes = []

            if 'values' in result:
                for val in result['values']:
                    if isinstance(val, bytes) and len(val) == 6:
                        ip = '.'.join(str(b) for b in val[:4])
                        port = struct.unpack('!H', val[4:6])[0]
                        peers.append((ip, port))

            if 'nodes' in result:
                nodes_data = result['nodes']
                if isinstance(nodes_data, bytes):
                    nodes = decode_nodes_compact(nodes_data)

            return (peers, nodes)
        except Exception:
            return None

    async def close(self):
        """关闭 DHT 服务器"""
        self._running = False
        self._protocol = None
        if hasattr(self, '_transport') and self._transport:
            self._transport.close()
            self._transport = None

    @property
    def port(self) -> int:
        return self._port


def encode_nodes_compact(nodes: List[Node]) -> bytes:
    """将节点列表编码为紧凑格式"""
    result = b''
    for node in nodes[:K]:
        result += node.to_compact()
    return result


def decode_nodes_compact(data: bytes) -> List[Node]:
    """解码紧凑格式的节点列表"""
    nodes = []
    for i in range(0, len(data), 26):
        if i + 26 > len(data):
            break
        node_id = data[i:i + 20]
        ip = socket.inet_ntoa(data[i + 20:i + 24])
        port = struct.unpack('!H', data[i + 24:i + 26])[0]
        if node_id != b'\x00' * 20:
            node = Node(node_id, ip, port)
            nodes.append(node)
    return nodes
