"""输出区 Widget — 渲染命令输出/错误/阻断/AI 建议

基于 textual RichLog，支持 rich renderable + 自动滚动 + 语法高亮。
"""

from __future__ import annotations

from textual.widgets import RichLog


class OutputPanel(RichLog):
    """输出区：渲染 stdout/stderr/错误/建议

    markup=True 启用 rich markup，auto_scroll 跟随末尾。
    """

    DEFAULT_CSS = """
    OutputPanel {
        border: round $accent;
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__(markup=True, auto_scroll=True, wrap=True, name="output")
        self.border_title = "输出"
        self._last_exit_code: int | None = None
        # 文本缓存: 便于测试/调试内省 (仅缓存字符串内容, 不影响渲染)
        self._log_text: list[str] = []

    def write(self, content, width=None, expand=False, shrink=True, scroll_end=None):  # type: ignore[override]
        """覆写 RichLog.write: 额外缓存字符串内容供内省"""
        if isinstance(content, str):
            self._log_text.append(content)
        return super().write(content, width, expand, shrink, scroll_end)

    @property
    def text(self) -> str:
        """已写入的字符串内容 (含 rich markup 标签, 供测试/调试)"""
        return "".join(self._log_text)

    # --- 基础渲染 ---
    def write_stdout(self, text: str) -> None:
        """渲染 stdout（纯文本，保留换行）"""
        if not text:
            return
        if not text.endswith("\n"):
            text += "\n"
        # 用 print 走 RichLog 的 markup=False 路径避免误解析
        self.write(text)

    def write_stderr(self, text: str) -> None:
        """渲染 stderr（红色）"""
        if not text:
            return
        if not text.endswith("\n"):
            text += "\n"
        self.write(f"[red]{self._escape(text)}[/]")

    def write_exit_code(self, code: int | None, alias: str | None = None) -> None:
        """渲染非零退出码提示"""
        if code in (0, None):
            return
        self._last_exit_code = code
        suffix = f"（别名 {alias}）" if alias else ""
        self.write(f"[yellow]退出码 {code}{suffix}[/]")

    def write_error(self, code: str, message: str) -> None:
        """渲染 DracoError"""
        self.write(f"[bold red]{code}[/] {self._escape(message)}")

    def write_block(self, reason: str) -> None:
        """渲染风控阻断"""
        self.write(f"[bold yellow]已阻断:[/] {self._escape(reason or '被钩子阻断')}")

    def write_info(self, msg: str) -> None:
        """渲染普通信息"""
        self.write(f"[dim]{self._escape(msg)}[/]")

    def write_success(self, msg: str) -> None:
        """渲染成功信息"""
        self.write(f"[green]{self._escape(msg)}[/]")

    def write_code(self, code: str, lexer: str = "python") -> None:
        """渲染语法高亮代码块"""
        try:
            from rich.syntax import Syntax

            self.write(Syntax(code, lexer, theme="monokai", word_wrap=True))
        except Exception:
            self.write(self._escape(code))

    def write_suggestion(self, suggested_command: str = "", analysis: str = "", suggestions: list[str] | None = None) -> None:
        """渲染 AI 纠错建议/感知建议"""
        if analysis:
            self.write(f"[cyan]AI 分析:[/] {self._escape(analysis)}")
        if suggested_command:
            self.write(f"[cyan]建议命令:[/] [green]{self._escape(suggested_command)}[/]")
        for s in (suggestions or []):
            self.write(f"[dim cyan]💡 {self._escape(s)}[/]")

    def write_agent_result(self, func_name: str, success: bool, error: str = "", stdout: str = "") -> None:
        """渲染 P4 Agent 通道结果"""
        mark = "✅" if success else "❌"
        color = "green" if success else "red"
        line = f"[{color}]{mark} Agent {func_name}[/]"
        if error:
            line += f": {self._escape(error)}"
        self.write(line)
        if stdout:
            self.write_stdout(stdout)

    def clear_output(self) -> None:
        """清屏"""
        self.clear()
        self._last_exit_code = None
        self._log_text.clear()

    @property
    def last_exit_code(self) -> int | None:
        return self._last_exit_code

    @staticmethod
    def _escape(text: str) -> str:
        """转义 rich markup 特殊字符"""
        from rich.markup import escape

        return escape(text)
