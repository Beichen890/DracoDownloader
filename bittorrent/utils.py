"""
BitTorrent 工具函数
"""

from .dht import encode_nodes_compact, decode_nodes_compact


def generate_peer_id() -> bytes:
    """生成 Peer ID (BEP 20)"""
    import random
    prefix = b'-DD0001-'
    random_part = ''.join(random.choices(
        '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=12)).encode()
    return prefix + random_part


def sha1_hash(data: bytes) -> bytes:
    """计算 SHA1 哈希"""
    import hashlib
    return hashlib.sha1(data).digest()


__all__ = [
    "encode_nodes_compact",
    "decode_nodes_compact",
    "generate_peer_id",
    "sha1_hash",
]
