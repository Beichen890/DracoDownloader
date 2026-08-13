"""Renderer 协议与 SimpleRenderer 降级实现 (P5)

Renderer 是渲染层抽象。TextualRenderer (在 app.py 内) 是默认实现，
SimpleRenderer 用 rich 直接 print，用于非 TTY 降级 / textual 不可用 / CI 环境。

内核层永远不直接调 Renderer，只通过事件总线产出 RenderContext 数据。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..history.models import CommandRecord
from .context import (
    KIND_AGENT_PROGRESS,
    KIND_BLOCK,
    KIND_ERROR,
    KIND_HISTORY,
    KIND_INFO,
    KIND_OUTPUT,
    KIND_STATUS,
    KIND_SUGGESTION,
    AgentTaskInfo,
    RenderContext,
)


@runtime_checkable
class Renderer(Protocol):
    """渲染层抽象协议"""

    def render(self, ctx: RenderContext) -> None:
        """渲染一个 RenderContext (主入口)"""
        ...

    def render_output(self, ctx: RenderContext) -> None:
        """渲染命令输出 (KIND_OUTPUT)"""
        ...

    def render_error(self, ctx: RenderContext) -> None:
        """渲染错误 (KIND_ERROR / KIND_BLOCK)"""
        ...

    def render_history(self, records: list[CommandRecord]) -> None:
        """渲染历史列表"""
        ...

    def render_status(self, ctx: RenderContext) -> None:
        """渲染状态栏更新 (KIND_STATUS)"""
        ...

    def render_info(self, msg: str) -> None:
        """渲染普通信息行"""
        ...


class SimpleRenderer:
    """纯 rich/print 降级渲染器

    用于非 TTY 环境 (CI/管道) 或 textual 不可用时。
    不维护任何状态，每次调用直接输出到 stdout/stderr。
    """

    def __init__(self, use_rich: bool = True) -> None:
        self._use_rich = use_rich
        self._console = None
        if use_rich:
            try:
                from rich.console import Console

                self._console = Console()
            except ImportError:
                self._console = None
                self._use_rich = False

    # --- 主入口 ---
    def render(self, ctx: RenderContext) -> None:
        if ctx.kind == KIND_OUTPUT:
            self.render_output(ctx)
        elif ctx.kind in (KIND_ERROR, KIND_BLOCK):
            self.render_error(ctx)
        elif ctx.kind == KIND_STATUS:
            self.render_status(ctx)
        elif ctx.kind == KIND_SUGGESTION:
            self._render_suggestion(ctx)
        elif ctx.kind == KIND_AGENT_PROGRESS:
            self._render_agent(ctx)
        elif ctx.kind == KIND_INFO:
            self.render_info(ctx.raw_input or "")
        # KIND_HISTORY 由 render_history 单独入口处理

    # --- 输出 ---
    def render_output(self, ctx: RenderContext) -> None:
        import sys

        if ctx.stdout:
            sys.stdout.write(ctx.stdout)
            if not ctx.stdout.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        if ctx.stderr:
            sys.stderr.write(ctx.stderr)
            if not ctx.stderr.endswith("\n"):
                sys.stderr.write("\n")
            sys.stderr.flush()
        if ctx.exit_code not in (0, None):
            self.render_info(f"退出码 {ctx.exit_code}")

    def render_error(self, ctx: RenderContext) -> None:
        import sys

        if ctx.kind == KIND_BLOCK:
            msg = ctx.block_reason or "被钩子阻断"
            if self._use_rich and self._console is not None:
                self._console.print(f"[bold yellow]已阻断:[/] {msg}")
            else:
                print(f"已阻断: {msg}", file=sys.stderr)
            return
        # KIND_ERROR
        if self._use_rich and self._console is not None:
            self._console.print(
                f"[bold red]{ctx.error_code}[/] {ctx.error_message}"
            )
        else:
            print(f"[{ctx.error_code}] {ctx.error_message}", file=sys.stderr)

    def render_history(self, records: list[CommandRecord]) -> None:
        if not records:
            self.render_info("（无历史记录）")
            return
        self.render_info(f"最近 {len(records)} 条历史:")
        for r in reversed(records):  # 时间正序显示
            status = (
                "OK"
                if r.exit_code == 0
                else (f"×{r.exit_code}" if r.exit_code is not None else "?")
            )
            print(f"  [{status}] {r.raw_input}")

    def render_status(self, ctx: RenderContext) -> None:
        # SimpleRenderer 不维护常驻状态栏，仅打印一行摘要
        parts = [ctx.platform or "?", ctx.cwd or "?"]
        if ctx.theme_name:
            parts.append(f"theme={ctx.theme_name}")
        if ctx.ai_enabled:
            parts.append("AI=on")
        if ctx.agent_enabled:
            parts.append("Agent=on")
        self.render_info(" | ".join(parts))

    def render_info(self, msg: str) -> None:
        if self._use_rich and self._console is not None:
            self._console.print(f"[dim]{msg}[/]")
        else:
            print(msg)

    # --- 内部 ---
    def _render_suggestion(self, ctx: RenderContext) -> None:
        if self._use_rich and self._console is not None:
            if ctx.suggested_command:
                self._console.print(
                    f"[cyan]建议命令:[/] [green]{ctx.suggested_command}[/]"
                )
            for s in ctx.suggestions:
                self._console.print(f"[dim cyan]💡 {s}[/]")
        else:
            if ctx.suggested_command:
                print(f"建议命令: {ctx.suggested_command}")
            for s in ctx.suggestions:
                print(f"💡 {s}")

    def _render_agent(self, ctx: RenderContext) -> None:
        task = ctx.agent_task
        if task is None:
            return
        mark = "✅" if task.success else "❌"
        if self._use_rich and self._console is not None:
            color = "green" if task.success else "red"
            self._console.print(
                f"[{color}]{mark} Agent {task.func_name}[/]"
                + (f": {task.error}" if task.error else "")
            )
        else:
            print(f"{mark} Agent {task.func_name}" + (f": {task.error}" if task.error else ""))
        if task.stdout:
            import sys

            sys.stdout.write(task.stdout)
            if not task.stdout.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
