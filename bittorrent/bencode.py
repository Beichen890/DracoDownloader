"""
纯 Python Bencode 编解码器 (BEP 3)

Bencode 是 BitTorrent 协议的数据编码格式:
- 整数: i<decimal>e
- 字符串: <length>:<bytes>
- 列表: l<items>e
- 字典: d<key-value pairs>e
"""

import re
from typing import Union, List, Dict, Any


def encode(data: Any) -> bytes:
    """将 Python 对象编码为 bencode 字节串"""
    if isinstance(data, int):
        return b'i' + str(data).encode() + b'e'
    elif isinstance(data, bytes):
        return str(len(data)).encode() + b':' + data
    elif isinstance(data, str):
        encoded = data.encode('utf-8')
        return str(len(encoded)).encode() + b':' + encoded
    elif isinstance(data, list):
        parts = [encode(item) for item in data]
        return b'l' + b''.join(parts) + b'e'
    elif isinstance(data, dict):
        # 字典的 key 必须按字节序排序
        parts = []
        for k in sorted(data.keys()):
            parts.append(encode(k))
            parts.append(encode(data[k]))
        return b'd' + b''.join(parts) + b'e'
    elif isinstance(data, bool):
        return encode(int(data))
    else:
        raise TypeError(f"Cannot bencode type: {type(data)}")


def decode(data: bytes) -> Any:
    """将 bencode 字节串解码为 Python 对象"""
    result, rest = _decode_next(data, 0)
    return result


def _decode_next(data: bytes, pos: int) -> tuple:
    """内部递归解码，返回 (值, 下一个位置)"""
    if pos >= len(data):
        raise ValueError("Unexpected end of data")

    c = data[pos:pos + 1]

    if c == b'i':
        # 整数: i123e
        end = data.index(b'e', pos)
        value = int(data[pos + 1:end])
        return value, end + 1

    elif c == b'l':
        # 列表: l<items>e
        result = []
        pos += 1
        while pos < len(data) and data[pos:pos + 1] != b'e':
            item, pos = _decode_next(data, pos)
            result.append(item)
        return result, pos + 1

    elif c == b'd':
        # 字典: d<key-value pairs>e
        result = {}
        pos += 1
        while pos < len(data) and data[pos:pos + 1] != b'e':
            key, pos = _decode_next(data, pos)
            value, pos = _decode_next(data, pos)
            # key 可能是 bytes 或 str
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='replace')
            result[key] = value
        return result, pos + 1

    elif c in b'0123456789':
        # 字符串: <length>:<bytes>
        colon = data.index(b':', pos)
        length = int(data[pos:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end

    else:
        raise ValueError(f"Invalid bencode at position {pos}: byte {c}")


def decode_torrent(data: bytes) -> Dict[str, Any]:
    """解码 .torrent 文件（元信息文件）"""
    result = decode(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid torrent file: root must be a dictionary")

    # 解析 info 字典
    info = result.get('info', {})
    if isinstance(info, dict):
        # 提取常用字段
        pieces = info.get('pieces', b'')
        piece_length = info.get('piece length', 0)
        # 如果是多文件 torrent
        files = info.get('files', [])
        if files:
            total_size = sum(f.get('length', 0) for f in files)
        else:
            total_size = info.get('length', 0)

        result['_parsed'] = {
            'piece_length': piece_length,
            'total_size': total_size,
            'piece_count': len(pieces) // 20 if pieces else 0,
            'is_multi_file': bool(files),
        }

    return result


def info_hash(data: bytes) -> bytes:
    """计算 info_hash (BEP 3) - info 字典的 SHA1"""
    import hashlib

    # 找到 "4:info" 字典键 (找最后一个 "4:infod" 模式，因为 info 键的值是字典)
    # 在标准 torrent 中结构是 d...4:infod...e...e
    marker = b'4:infod'
    pos = data.rfind(marker)
    if pos < 0:
        # 回退：找 "4:info"
        marker = b'4:info'
        pos = data.rfind(marker)
        if pos < 0:
            raise ValueError("Cannot find info dictionary in torrent data")

    # 跳过 bencoded 字符串 "info"
    # "4:infod" → 跳过 "4:info" 得到 "d..."
    info_start = pos + len(marker) - 1  # 指向 'd'

    if info_start >= len(data) or data[info_start:info_start + 1] != b'd':
        raise ValueError("Info dict must start with 'd'")

    # 跳过 info 字典的值到它的结尾
    depth = 1
    i = info_start + 1
    while i < len(data) and depth > 0:
        c = data[i:i + 1]
        if c == b'e':
            depth -= 1
            i += 1
        elif c == b'd' or c == b'l':
            depth += 1
            i += 1
        elif c == b'i':
            i = data.index(b'e', i) + 1
        elif c in b'0123456789':
            colon = data.index(b':', i)
            length = int(data[i:colon])
            i = colon + length + 1
        else:
            i += 1

    info_encoded = data[info_start:i]
    return hashlib.sha1(info_encoded).digest()


