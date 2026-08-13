"""内置模板: neofetch_style

仿 neofetch 风格: 左侧小龙 ASCII art (~8 行), 右侧 key: value 列出
version/platform/python/session/cwd/uptime。用 ANSI 颜色区分 key(青) 和 value(白)。
last_session_summary 非空时在下方显示 "上次会话: ..."。
信息行字段缺失 (空串) 则该项不显示; session_id 取前 8 位。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..loader import StartupContext

# ANSI 颜色
CYAN = "\033[36m"
WHITE = "\033[37m"
BOLD = "\033[1m"
RESET = "\033[0m"

# 左侧小龙 ASCII art (9 行)
_DRAGON = r"""       /\       /\
      /  \     /  \
     /    \___/    \
    /  /\  / _ \   \
   /  /  \/  __/   /
  /__/ \__\___/\__/
    \   \ \/ /  /
     \   \/--/  /
      \__/    \__/
"""


def _kv(key: str, value: str) -> str:
    """构造一行 key(青): value(白)"""
    return f"{CYAN}{key}{RESET}{WHITE}{value}{RESET}"


def render(ctx: StartupContext) -> str:
    """渲染 neofetch_style 模板: 左龙 art + 右信息列表"""
    art_lines = _DRAGON.strip("\n").split("\n")
    art_width = max((len(line) for line in art_lines), default=0)

    # 右侧信息列表
    info_lines: list[str] = []
    info_lines.append(f"{CYAN}{BOLD}OpenDracoCLI{RESET}")
    info_lines.append(f"{CYAN}{'─' * 16}{RESET}")
    if ctx.version:
        info_lines.append(_kv("version:  ", ctx.version))
    if ctx.platform:
        info_lines.append(_kv("platform: ", ctx.platform))
    if ctx.python_version:
        info_lines.append(_kv("python:   ", ctx.python_version))
    sid = (ctx.session_id or "")[:8]
    if sid:
        info_lines.append(_kv("session:  ", sid))
    if ctx.cwd:
        info_lines.append(_kv("cwd:      ", ctx.cwd))
    if ctx.uptime_hint:
        info_lines.append(_kv("uptime:   ", ctx.uptime_hint))

    # 左右合并: 左侧 art (青色, 固定宽度), 右侧 info
    n = max(len(art_lines), len(info_lines))
    merged: list[str] = []
    for i in range(n):
        left = (art_lines[i] if i < len(art_lines) else "").ljust(art_width + 2)
        right = info_lines[i] if i < len(info_lines) else ""
        merged.append(f"{CYAN}{left}{RESET}{right}")

    # 上次会话摘要 (非空时追加)
    if ctx.last_session_summary:
        merged.append("")
        merged.append(f"{CYAN}上次会话:{RESET} {WHITE}{ctx.last_session_summary}{RESET}")

    return "\n".join(merged)
