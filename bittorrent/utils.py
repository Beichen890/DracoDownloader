"""
BitTorrent 工具函数
"""

from pathlib import PurePosixPath, Path

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


def sanitize_torrent_path_parts(path_parts) -> str:
    """
    安全地将 BitTorrent multi-file 路径段拼接为相对路径。

    防护目标：Zip-slip / 路径穿越
    - 过滤空段、'.'、'..'
    - 拒绝段内包含 '/' 或 '\\'（单个段按规范不应含分隔符，
      若存在会让 Path(...) 在连接时解析嵌套相对组件，逃逸下载目录）
    - 拒绝以分隔符开头的段（绝对路径注入）
    - 拒绝 Windows 盘符段（如 "C:"）
    - 最终用 PurePosixPath 归一化，并校验结果仍停留在当前目录树下
      （PurePosixPath 不访问 FS，仅做词法规约）

    Returns:
        安全的相对路径字符串；若全部被过滤则返回 "unnamed"。
    """
    safe_parts = []
    for p in path_parts:
        part = p.decode() if isinstance(p, bytes) else str(p)

        # 空段 / 当前目录 / 上级目录 直接丢弃
        if part in ('', '.', '..'):
            continue

        # 绝对路径或 UNC 前缀
        if part.startswith('/') or part.startswith('\\'):
            continue

        # Windows 盘符（C:  D:  等；长度>=2 且第 2 字符为冒号，首字符为字母）
        if (len(part) >= 2 and part[1] == ':'
                and part[0].isalpha()):
            continue

        # 单个段内嵌入分隔符 → 恶意构造的嵌套相对路径
        # 例：path=['subdir/../../../etc/passwd'] 在 join + Path / 运算下
        # 会把 '..' 当真实组件解析，跳出下载目录。
        if '/' in part or '\\' in part:
            continue

        safe_parts.append(part)

    raw = '/'.join(safe_parts) or 'unnamed'

    # 归一化 + 二次防御：确认结果不会逃逸到父目录或变为绝对路径
    try:
        normalized = PurePosixPath(raw)
        # PurePosixPath 不做 FS 访问；迭代组件确认根组件不是 ".."
        parts_list = list(normalized.parts)
        # 剔除开头的 '.'
        while parts_list and parts_list[0] == '.':
            parts_list.pop(0)
        if not parts_list or parts_list[0] == '..' or normalized.is_absolute():
            return 'unnamed'
    except Exception:
        return 'unnamed'

    return raw


__all__ = [
    "encode_nodes_compact",
    "decode_nodes_compact",
    "generate_peer_id",
    "sha1_hash",
    "sanitize_torrent_path_parts",
]
