"""内置模板: figlet

用 pyfiglet 把 "OpenDracoCLI" 渲染成 ASCII 大字 (可选依赖, 缺失则回退 minimal)。
顶部大字 + 下方信息行 (版本/平台/python/session/uptime), ANSI 青色。
pyfiglet 缺失时 from .minimal import render 复用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..loader import StartupContext

# ANSI 颜色
CYAN = "\033[36m"
RESET = "\033[0m"

# pyfiglet 为可选依赖, 缺失时整模块回退 minimal
try:
    import pyfiglet  # type: ignore
    _HAS_PYFIGLET = True
except Exception:
    _HAS_PYFIGLET = False


def _info_lines(ctx: StartupContext) -> list[str]:
    """构造信息行, 空字段跳过"""
    lines: list[str] = []
    head = f"OpenDracoCLI v{ctx.version}"
    if ctx.platform:
        head += f" ({ctx.platform})"
    lines.append(head)
    parts: list[str] = []
    if ctx.python_version:
        parts.append(f"Python {ctx.python_version}")
    sid = (ctx.session_id or "")[:8]
    if sid:
        parts.append(f"Session {sid}")
    if parts:
        lines.append("  ".join(parts))
    if ctx.uptime_hint:
        lines.append(ctx.uptime_hint)
    return lines


if _HAS_PYFIGLET:
    def render(ctx: StartupContext) -> str:
        """figlet 模板渲染 (pyfiglet 可用时); 任何失败回退 minimal"""
        try:
            big = pyfiglet.figlet_format("OpenDracoCLI", font="standard")
        except Exception:
            # figlet 渲染失败时回退 minimal
            from .minimal import render as _minimal_render
            return _minimal_render(ctx)
        big = (big or "").rstrip("\n")
        if not big:
            from .minimal import render as _minimal_render
            return _minimal_render(ctx)
        body = "\n".join(_info_lines(ctx))
        return f"{CYAN}{big}{RESET}\n{body}"
else:
    # pyfiglet 缺失: 直接复用 minimal 的 render
    from .minimal import render  # noqa: F401
