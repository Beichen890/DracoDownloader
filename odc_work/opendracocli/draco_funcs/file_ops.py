"""文件操作类 Draco 函数

跨平台一致的文件搜索、批量重命名、目录树——用 Python 标准库实现，
不依赖 find/du/tree 等 Unix 命令（Windows 无这些命令）。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, List


def ffind(ctx, pattern: str = "*", path: str = ".", dtype: str = "all") -> List[str]:
    """跨平台文件搜索（替代 find 命令）

    用 fnmatch + os.walk 实现，Windows/Linux 行为一致。

    用法:
        ffind "*.py"                 # 当前目录递归找 .py 文件
        ffind "*.py" path=src        # 在 src 目录找
        ffind "*" dtype=dir          # 只找目录
        ffind "test_*" path=tests dtype=file

    Args:
        ctx: AgentContext
        pattern: 文件名匹配模式（支持 * ? []）
        path: 搜索根目录（默认当前目录）
        dtype: all | file | dir，筛选类型

    Returns:
        匹配的相对路径列表
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        print(f"ffind: {path} 不是目录")
        return []

    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dtype in ("all", "dir"):
            for d in dirnames:
                if fnmatch.fnmatch(d, pattern):
                    rel = os.path.relpath(os.path.join(dirpath, d), root)
                    results.append(rel)
        if dtype in ("all", "file"):
            for f in filenames:
                if fnmatch.fnmatch(f, pattern):
                    rel = os.path.relpath(os.path.join(dirpath, f), root)
                    results.append(rel)

    results.sort()
    for r in results:
        print(r)
    print(f"\n共 {len(results)} 项")
    return results


def frename(ctx, pattern: str, replacement: str, path: str = ".", dry_run: bool = True) -> int:
    r"""批量重命名文件（正则替换，跨平台）

    用 Python re 模块实现，比 sed/rename 命令更可控且跨平台。

    用法:
        frename r"\.txt$" ".md"           # 预览：txt→md
        frename r"\.txt$" ".md" dry_run=false  # 实际执行
        frename r"img_(\d+)" r"photo_\1" path=imgs dry_run=false

    Args:
        ctx: AgentContext
        pattern: 正则模式（匹配文件名）
        replacement: 替换串（支持 \1 反向引用）
        path: 目标目录
        dry_run: 预演模式（默认 True，只打印不执行）

    Returns:
        重命名的文件数
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        print(f"frename: {path} 不是目录")
        return 0

    regex = re.compile(pattern)
    count = 0

    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        new_name = regex.sub(replacement, entry.name)
        if new_name == entry.name:
            continue
        new_path = entry.parent / new_name
        action = "DRY" if dry_run else "OK"
        print(f"[{action}] {entry.name} -> {new_name}")
        if not dry_run:
            entry.rename(new_path)
        count += 1

    print(f"\n共 {count} 个文件{'（预演，未实际执行）' if dry_run else '已重命名'}")
    return count


def ftree(ctx, path: str = ".", max_depth: int = 3, show_hidden: bool = False) -> str:
    """目录树展示（替代 tree 命令）

    纯 Python 实现，Windows/Linux 一致。带深度限制和隐藏文件控制。

    用法:
        ftree                    # 当前目录树（深度3）
        ftree path=src max_depth=5
        ftree path=. show_hidden=true

    Args:
        ctx: AgentContext
        path: 根目录
        max_depth: 最大递归深度
        show_hidden: 是否显示隐藏文件/目录

    Returns:
        树形字符串
    """
    root = Path(path).expanduser()
    if not root.exists():
        print(f"ftree: {path} 不存在")
        return ""

    lines: List[str] = [root.name if root.is_dir() else str(root)]

    def _walk(p: Path, prefix: str, depth: int) -> None:
        if depth >= max_depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return

        visible = [e for e in entries if show_hidden or not e.name.startswith(".")]
        for i, entry in enumerate(visible):
            is_last = i == len(visible) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            if entry.is_dir():
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, depth + 1)

    if root.is_dir():
        _walk(root, "", 0)

    output = "\n".join(lines)
    print(output)
    return output
