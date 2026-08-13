"""Draco 内置函数注册

把 draco_funcs 下的函数注册到 FunctionRegistry（builtin=True）。
启动时由 CLI 调用 register_builtins(registry)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import archive_ops, file_ops, net_ops, text_ops

if TYPE_CHECKING:
    from ..agent.function_registry import FunctionRegistry


def register_builtins(registry: "FunctionRegistry") -> int:
    """注册所有内置 Draco 函数

    Args:
        registry: FunctionRegistry 实例

    Returns:
        注册的函数数量
    """
    count = 0
    # 文件操作
    for func in (file_ops.ffind, file_ops.frename, file_ops.ftree):
        registry.register(func.__name__, func, builtin=True)
        count += 1
    # 网络操作
    for func in (net_ops.pcheck, net_ops.http, net_ops.dns):
        registry.register(func.__name__, func, builtin=True)
        count += 1
    # 归档操作（tar/zip）
    for func in (archive_ops.ftar, archive_ops.fzip):
        registry.register(func.__name__, func, builtin=True)
        count += 1
    # 文本操作（grep/hash/json）
    for func in (text_ops.fgrep, text_ops.fhash, text_ops.fjson):
        registry.register(func.__name__, func, builtin=True)
        count += 1
    return count
