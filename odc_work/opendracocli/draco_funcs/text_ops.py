"""文本处理类 Draco 函数

跨平台的 grep/哈希/JSON 处理——用 Python 标准库实现，
不依赖 grep/openssl/jq 等命令（Windows 无这些命令）。
"""

from __future__ import annotations

import hashlib
import json as _json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def fgrep(
    ctx,
    pattern: str,
    path: str = "",
    ignore_case: bool = False,
    regex: bool = False,
    invert: bool = False,
    line_numbers: bool = False,
    count: bool = False,
    max_count: int = 0,
    cwd: str = "",
) -> int:
    """文本搜索（替代 grep 命令，跨平台）

    用 re + 文件读取实现，Windows/Linux 一致。支持从 stdin 读取。

    用法:
        echo "hello\\nworld" | fgrep world           # 从 stdin 搜索
        fgrep "TODO" path=src                         # 在 src 目录递归搜索
        fgrep "log" path=app.py line_numbers=true     # 显示行号
        fgrep "test" path=tests ignore_case=true      # 忽略大小写
        fgrep "^def " path=src regex=true             # 正则模式
        fgrep "debug" path=src invert=true            # 反向匹配（不含 debug 的行）

    Args:
        ctx: AgentContext
        pattern: 搜索模式（默认字面量；regex=true 时为正则）
        path: 文件或目录路径；空则从 stdin 读取
        ignore_case: 忽略大小写
        regex: 启用正则模式
        invert: 反向匹配（输出不匹配的行）
        line_numbers: 输出带行号
        count: 只输出匹配行数
        max_count: 最多输出 N 个匹配（0=不限）
        cwd: 工作目录

    Returns:
        匹配的行数（用于退出码：0=有匹配，1=无匹配）
    """
    base_cwd = cwd or (ctx.cwd if ctx and hasattr(ctx, "cwd") else os.getcwd())
    flags = re.IGNORECASE if ignore_case else 0
    if not regex:
        pattern = re.escape(pattern)
    try:
        regex_obj = re.compile(pattern, flags)
    except re.error as e:
        print(f"fgrep: 无效模式: {e}", file=sys.stderr)
        return 2

    total_matches = 0
    printed = 0

    def search_text(text: str, name: str = "") -> int:
        nonlocal total_matches, printed
        matched = 0
        for i, line in enumerate(text.splitlines(), 1):
            is_match = bool(regex_obj.search(line))
            if is_match == invert:
                continue
            matched += 1
            total_matches += 1
            if count:
                continue
            if max_count and printed >= max_count:
                continue
            prefix = ""
            if name:
                prefix += f"{name}:"
            if line_numbers:
                prefix += f"{i}:"
            print(f"{prefix}{line}")
            printed += 1
        return matched

    # 无 path：从 stdin 读取
    if not path:
        stdin_text = ""
        if ctx and hasattr(ctx, "stdin") and ctx.stdin:
            stdin_text = ctx.stdin
        else:
            try:
                stdin_text = sys.stdin.read()
            except Exception:
                stdin_text = ""
        search_text(stdin_text)
    else:
        target = path if os.path.isabs(path) else os.path.join(base_cwd, path)
        if os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as fh:
                    search_text(fh.read())
            except OSError as e:
                print(f"fgrep: {path}: {e}", file=sys.stderr)
                return 2
        elif os.path.isdir(target):
            for dirpath, dirnames, filenames in os.walk(target):
                # 跳过隐藏目录和常见二进制目录
                dirnames[:] = [d for d in dirnames if not d.startswith(".")
                               and d not in ("__pycache__", "node_modules", ".git")]
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                    except OSError:
                        continue
                    if search_text(text, os.path.relpath(full, base_cwd)):
                        pass
        else:
            print(f"fgrep: {path}: 没有那个文件或目录", file=sys.stderr)
            return 2

    if count:
        print(total_matches)
    # 通过 ctx.exit_code 对齐 GNU grep 约定（0=有匹配，1=无匹配，2=错误）
    if ctx is not None and hasattr(ctx, "exit_code"):
        ctx.exit_code = 0 if total_matches > 0 else 1
    return 0 if total_matches > 0 else 1


def fhash(
    ctx,
    file: str,
    algo: str = "sha256",
    cwd: str = "",
) -> str:
    """文件哈希（替代 sha256sum/md5sum 等命令，跨平台）

    用 hashlib 实现，Windows/Linux 一致。

    用法:
        fhash file.txt                # SHA-256 哈希
        fhash file.txt algo=md5       # MD5 哈希
        fhash file.txt algo=sha1

    Args:
        ctx: AgentContext
        file: 文件路径
        algo: 哈希算法（md5/sha1/sha256/sha512）
        cwd: 工作目录

    Returns:
        十六进制哈希字符串
    """
    base_cwd = cwd or (ctx.cwd if ctx and hasattr(ctx, "cwd") else os.getcwd())
    target = file if os.path.isabs(file) else os.path.join(base_cwd, file)

    algo_lower = algo.lower()
    try:
        h = hashlib.new(algo_lower)
    except ValueError as e:
        print(f"fhash: 不支持的算法 {algo}: {e}", file=sys.stderr)
        return ""

    try:
        # 流式读取，支持大文件
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        print(f"fhash: {file}: {e}", file=sys.stderr)
        return ""

    digest = h.hexdigest()
    print(f"{digest}  {file}")
    return digest


def fjson(
    ctx,
    file: str = "",
    query: str = "",
    indent: int = 2,
    raw: bool = False,
    cwd: str = "",
) -> Any:
    """JSON 查询/格式化（替代 jq 命令，跨平台）

    用 json + 简单点路径查询实现。支持从 stdin 或文件读取。

    用法:
        echo '{"a":{"b":1}}' | fjson                    # 美化输出
        echo '{"a":{"b":1}}' | fjson query=a.b          # 查询 a.b
        fjson data.json query=users                     # 查询文件中的 users
        fjson data.json query=users.0.name              # 数组索引
        fjson data.json raw=true                        # 原始紧凑输出

    Args:
        ctx: AgentContext
        file: JSON 文件路径；空则从 stdin 读取
        query: 点路径查询（如 a.b.0.c）；空则返回整个 JSON
        indent: 缩进空格数
        raw: True=紧凑输出（无空格），False=美化输出
        cwd: 工作目录

    Returns:
        查询结果（可能是 dict/list/scalar）
    """
    base_cwd = cwd or (ctx.cwd if ctx and hasattr(ctx, "cwd") else os.getcwd())

    if file:
        target = file if os.path.isabs(file) else os.path.join(base_cwd, file)
        try:
            with open(target, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            print(f"fjson: {file}: {e}", file=sys.stderr)
            return None
    else:
        if ctx and hasattr(ctx, "stdin") and ctx.stdin:
            text = ctx.stdin
        else:
            try:
                text = sys.stdin.read()
            except Exception:
                text = ""

    try:
        data = _json.loads(text)
    except ValueError as e:
        print(f"fjson: 解析失败: {e}", file=sys.stderr)
        return None

    # 点路径查询
    if query:
        cur: Any = data
        for part in query.split("."):
            if part == "":
                continue
            if isinstance(cur, list):
                try:
                    idx = int(part)
                    cur = cur[idx]
                except (ValueError, IndexError):
                    print(f"fjson: 数组索引无效: {part}", file=sys.stderr)
                    return None
            elif isinstance(cur, dict):
                if part not in cur:
                    print(f"fjson: 键不存在: {part}", file=sys.stderr)
                    return None
                cur = cur[part]
            else:
                print(f"fjson: 无法在标量上查询: {part}", file=sys.stderr)
                return None
        data = cur

    # 输出
    if isinstance(data, (dict, list)):
        if raw:
            print(_json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        else:
            print(_json.dumps(data, ensure_ascii=False, indent=indent))
    else:
        print(data)
    return data
