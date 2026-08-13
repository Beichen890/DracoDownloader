"""状态栏 Widget — 底部一行显示平台/cwd/会话/退出码/主题/AI/Agent"""

from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    """底部状态栏"""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("", name="status")
        self._platform: str = ""
        self._cwd: str = ""
        self._session: str = ""
        self._exit_code: int | None = None
        self._theme: str = ""
        self._ai: bool = False
        self._agent: bool = False
        self._func_count: int = 0

    def update_state(
        self,
        *,
        platform: str | None = None,
        cwd: str | None = None,
        session: str | None = None,
        exit_code: int | None = None,
        theme: str | None = None,
        ai: bool | None = None,
        agent: bool | None = None,
        func_count: int | None = None,
        clear_exit: bool = False,
    ) -> None:
        if platform is not None:
            self._platform = platform
        if cwd is not None:
            self._cwd = self._short_cwd(cwd)
        if session is not None:
            self._session = session[:8]
        if theme is not None:
            self._theme = theme
        if ai is not None:
            self._ai = ai
        if agent is not None:
            self._agent = agent
        if func_count is not None:
            self._func_count = func_count
        if not clear_exit and exit_code is not None:
            self._exit_code = exit_code
        if clear_exit:
            self._exit_code = None
        self._refresh()

    def _short_cwd(self, cwd: str) -> str:
        import os

        home = os.path.expanduser("~")
        if cwd.startswith(home):
            return "~" + cwd[len(home):]
        return cwd

    def _refresh(self) -> None:
        parts = [f"[bold]{self._platform or '?'}[/]"]
        if self._cwd:
            parts.append(self._cwd)
        if self._session:
            parts.append(f"sess={self._session}")
        if self._theme:
            parts.append(f"theme={self._theme}")
        if self._ai:
            parts.append("[green]AI[/]")
        if self._agent:
            parts.append(f"[magenta]Agent({self._func_count})[/]")
        if self._exit_code is not None:
            color = "green" if self._exit_code == 0 else "red"
            parts.append(f"[{color}]×{self._exit_code}[/]")
        self.update(" | ".join(parts))
