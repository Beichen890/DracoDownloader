"""内置模板: minimal

最简启动画面, 纯文本无 ANSI。信息行字段缺失 (空串) 则该行不显示。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..loader import StartupContext


def _info_lines(ctx: StartupContext) -> list[str]:
    """构造信息行, 空字段跳过"""
    lines: list[str] = []
    # 第一行: 版本 + 平台
    head = f"OpenDracoCLI v{ctx.version}"
    if ctx.platform:
        head += f" ({ctx.platform})"
    lines.append(head)
    # 第二行: Python + Session (两空格分隔)
    parts: list[str] = []
    if ctx.python_version:
        parts.append(f"Python {ctx.python_version}")
    sid = (ctx.session_id or "")[:8]
    if sid:
        parts.append(f"Session {sid}")
    if parts:
        lines.append("  ".join(parts))
    # 第三行: 启动耗时
    if ctx.uptime_hint:
        lines.append(ctx.uptime_hint)
    return lines


def render(ctx: StartupContext) -> str:
    """渲染 minimal 模板 (纯文本)"""
    return "\n".join(_info_lines(ctx))
