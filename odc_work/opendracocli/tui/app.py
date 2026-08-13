"""DracoApp — textual TUI 主应用 (P5)

P5 把渲染层从 prompt_toolkit+rich 迁移到 textual。本模块是 textual App 实现，
复用 P1-P4 的底层组件 (ShellPipeline/HistoryStore/AliasManager/RiskAssessor/
Confirmer/Authenticator/SandboxExecutor + P3 AI 组件 + P4 Agent 组件)，
内核 (shell/hooks/events) 零改动。

cli.py 的 OpenDracoCLI 类保留为 simple 引擎 (非 TTY 降级 + 测试目标)，本类是 textual 引擎。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Label, Input as TInput

from ..agent.arg_parser import ArgParseError, parse_call_args
from ..agent.code_generator import CodeGenerator, GeneratedFunction
from ..agent.code_runner import CodeRunner
from ..agent.context import AgentContext  # noqa: F401  (re-export 友好)
from ..agent.function_registry import (
    RegisteredFunction,
    get_global_registry as get_global_func_registry,
    reset_global_registry as reset_global_func_registry,
)
from ..agent.python_executor import AgentExecResult, PythonChannelExecutor
from ..agent.templates import get_template, list_templates
from ..ai.character import RoleRegistry, get_global_registry as get_global_role_registry
from ..ai.client import LLMClient, get_global_client, reset_global_client
from ..ai.corrector import Corrector
from ..ai.emotion import EmotionEngine
from ..ai.perception import PerceptionEngine
from ..ai.risk_advisor import RiskAdvisor
from ..aliases.manager import AliasManager, get_global_manager
from ..config import DracoConfig, get_global_config
from ..errors import DracoError, make_error
from ..events import EVT_SESSION_ENDED, EVT_SESSION_STARTED, Event, get_global_bus
from ..history.store import HistoryStore, get_global_store
from ..hooks.ai_risk_hook import AIRiskHook
from ..hooks.correction_hook import CorrectionHook
from ..hooks.perception_hook import PerceptionHook
from ..hooks.registry import HookRegistry, get_global_registry as get_global_hook_registry
from ..hooks.risk_hook import RiskAssessmentHook
from ..logger import get_logger
from ..security.authenticator import Authenticator
from ..security.confirmer import Confirmer
from ..security.risk_assessor import RiskAssessor, get_global_assessor
from ..security.sandbox import SandboxExecutor
from ..shell.pipeline import ShellPipeline
from .context import KIND_AGENT_PROGRESS, RenderContext
from .keymap import KeymapConfig, load_keymap
from .renderer import SimpleRenderer
from .startup.animator import StartupAnimator
from .startup.loader import StartupContext, StartupLoader, load_startup_config
from .startup.renderer import StartupRenderer
from .theme import ThemeProvider
from .widgets.history_sidebar import HistoryEntry, HistorySidebar
from .widgets.input_panel import InputPanel
from .widgets.output_panel import OutputPanel
from .widgets.status_bar import StatusBar

log = get_logger("tui.app")


# === 弹窗 Screen ===


class ConfirmScreen(ModalScreen[bool]):
    """yes/no 确认弹窗 (P2 风控 / AI 建议应用)"""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen > Label {
        width: 60;
        max-width: 80%;
        padding: 1 2;
        background: $boost;
        border: round $warning;
    }
    """

    BINDINGS = [
        Binding("y", "yes", "确认"),
        Binding("n,escape", "no", "拒绝"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        yield Label(self._prompt + "\n\n[y] 确认  [n] 拒绝")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PasswordScreen(ModalScreen[Optional[str]]):
    """密码输入弹窗 (P2 critical 身份验证)"""

    DEFAULT_CSS = """
    PasswordScreen {
        align: center middle;
    }
    PasswordScreen > Vertical {
        width: 50;
        padding: 1 2;
        background: $boost;
        border: round $error;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical

        with Vertical():
            yield Label(self._prompt)
            yield TInput(password=True, placeholder="密码")

    def on_mount(self) -> None:
        self.query_one(TInput).focus()

    def on_input_submitted(self, event: TInput.Submitted) -> None:
        self.dismiss(event.value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# === textual 化的 Confirmer / Authenticator ===


class TextualConfirmer(Confirmer):
    """把 ask_yes 走 textual ModalScreen (而非 prompt_toolkit)"""

    def __init__(self, app: "DracoApp") -> None:
        super().__init__()
        self._app = app

    async def ask_yes(self, prompt: str, default: bool = False) -> bool:
        if self._test_callback is not None:
            return self._test_callback(prompt, default)
        try:
            return bool(await self._app.push_screen_wait(ConfirmScreen(prompt)))
        except Exception:
            log.exception("textual confirm failed, fallback to default=%s", default)
            return default


class TextualAuthenticator(Authenticator):
    """把密码输入走 textual ModalScreen"""

    def __init__(self, app: "DracoApp", *, hash_path: Path, iterations: int) -> None:
        super().__init__(hash_path=hash_path, iterations=iterations)
        self._app = app

    async def _prompt_password(self, prompt: str) -> Optional[str]:
        if self._test_callback is not None:
            return self._test_callback()
        try:
            return await self._app.push_screen_wait(PasswordScreen(prompt))
        except Exception:
            log.exception("textual password input failed")
            return None


# === 主 App ===


class DracoApp(App):
    """OpenDracoCLI textual TUI 主应用"""

    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("ctrl+t", "switch_theme", "主题"),
        Binding("ctrl+h", "toggle_history", "历史栏"),
        Binding("ctrl+l", "clear_output", "清屏"),
        Binding("ctrl+d", "quit", "退出"),
        Binding("ctrl+e", "toggle_ai", "AI"),
        Binding("ctrl+a", "toggle_agent", "Agent"),
    ]

    def __init__(
        self,
        config: Optional[DracoConfig] = None,
        *,
        skip_startup: bool = False,
    ) -> None:
        super().__init__()
        self._config = config or get_global_config()
        self._skip_startup = skip_startup
        self._session_id = str(uuid.uuid4())
        self._running = False

        # P1 组件
        self._aliases = get_global_manager()
        self._history = get_global_store()

        # P2 组件 (用 textual 化的 confirmer/authenticator)
        self._assessor = get_global_assessor(self._config)
        self._confirmer = TextualConfirmer(self)
        self._authenticator = TextualAuthenticator(
            self,
            hash_path=self._config.auth_hash_file_resolved,
            iterations=self._config.pbkdf2_iterations,
        )
        self._sandbox = SandboxExecutor(
            inner=None, writable_paths=self._config.sandbox_writable_paths_resolved
        )
        self._pipeline = ShellPipeline(
            config=self._config,
            alias_manager=self._aliases,
            history_store=self._history,
        )
        self._sandbox._inner = self._pipeline._executor
        self._pipeline.set_sandbox_executor(self._sandbox)

        # P2 风控钩子
        self._risk_hook = RiskAssessmentHook(
            assessor=self._assessor,
            confirmer=self._confirmer,
            authenticator=self._authenticator,
            config=self._config,
        )
        get_global_hook_registry().register_pre(self._risk_hook)

        # P3 AI (条件接线)
        self._ai_enabled = bool(self._config.ai_enabled)
        self._llm_client: Optional[LLMClient] = None
        self._role_registry: Optional[RoleRegistry] = None
        self._emotion_engine: Optional[EmotionEngine] = None
        self._perception: Optional[PerceptionEngine] = None
        self._corrector: Optional[Corrector] = None
        self._risk_advisor: Optional[RiskAdvisor] = None
        self._correction_hook: Optional[CorrectionHook] = None
        self._perception_hook: Optional[PerceptionHook] = None
        self._ai_risk_hook: Optional[AIRiskHook] = None
        if self._ai_enabled:
            self._setup_ai()

        # P4 Agent (条件接线)
        self._agent_enabled = bool(self._config.agent_enabled)
        self._func_registry = None
        self._py_executor: Optional[PythonChannelExecutor] = None
        self._code_generator: Optional[CodeGenerator] = None
        self._code_runner: Optional[CodeRunner] = None
        if self._agent_enabled:
            self._setup_agent()

        # P5 主题 + 启动 + keymap
        self._theme_provider = ThemeProvider()
        if self._config.custom_themes_dir_resolved.exists():
            try:
                self._theme_provider.register_custom_dir(self._config.custom_themes_dir_resolved)
            except Exception:
                log.exception("register custom themes dir failed")
        self._keymap = load_keymap(self._config.keymap_file_resolved)
        self._startup_loader = StartupLoader()
        if self._config.startup_custom_templates_dir_resolved.exists():
            try:
                self._startup_loader.register_custom_dir(
                    self._config.startup_custom_templates_dir_resolved
                )
            except Exception:
                log.exception("register custom startup templates failed")
        self._startup_renderer = StartupRenderer(self._startup_loader)

        self._simple = SimpleRenderer(use_rich=False)  # 内部逻辑复用其格式化 (不实际 print)

    # === P3 / P4 接线 (搬自 cli.py，渲染改走 widgets) ===

    def _setup_ai(self) -> None:
        self._llm_client = get_global_client(self._config)
        self._role_registry = get_global_role_registry()
        self._emotion_engine = EmotionEngine(
            weights=self._config.emotion_weights, inertia=self._config.emotion_inertia
        )
        self._perception = PerceptionEngine(
            context_file=self._config.ai_context_file_resolved,
            emotion_engine=self._emotion_engine,
        )
        self._corrector = Corrector(
            self._llm_client,
            platform=self._config.current_platform,
            native_shell=self._config.native_shell[0],
        )
        self._risk_advisor = RiskAdvisor(
            self._llm_client,
            platform=self._config.current_platform,
            native_shell=self._config.native_shell[0],
        )
        self._correction_hook = CorrectionHook(
            client=self._llm_client,
            corrector=self._corrector,
            role_registry=self._role_registry,
            perception=self._perception,
            confirmer=self._confirmer,
            config=self._config,
        )
        get_global_hook_registry().register_post(self._correction_hook)
        if self._config.ai_perception_enabled:
            self._perception_hook = PerceptionHook(perception=self._perception, config=self._config)
            get_global_hook_registry().register_post(self._perception_hook)
        if self._config.ai_risk_enabled:
            self._ai_risk_hook = AIRiskHook(advisor=self._risk_advisor, config=self._config)
            get_global_hook_registry().register_pre(self._ai_risk_hook)
        log.info("P3 AI 已启用 (model=%s)", self._config.ai_model)

    def _setup_agent(self) -> None:
        self._code_runner = CodeRunner()
        self._func_registry = get_global_func_registry()
        self._func_registry.set_user_file(self._config.functions_file_resolved)
        try:
            n = self._func_registry.load_user_file(self._config.functions_file_resolved)
            if n > 0:
                log.info("P4 加载 %d 个用户函数", n)
        except DracoError as e:
            log.warning("加载用户函数失败: %s", e)
        self._py_executor = PythonChannelExecutor(
            config=self._config,
            history_store=self._history,
            shell_runner=self._shell_runner_for_agent,
        )
        client = self._llm_client or get_global_client(self._config)
        self._code_generator = CodeGenerator(
            client,
            platform=self._config.current_platform,
            max_tokens=self._config.agent_max_gen_tokens,
            runner=self._code_runner,
        )
        log.info("P4 Agent 已启用 (functions=%s)", self._config.functions_file)

    async def _shell_runner_for_agent(self, cmd: str):
        return await self._pipeline.run(cmd, session_id=self._session_id)

    # === 布局 ===

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False, name="OpenDracoCLI")
        with Horizontal(id="main"):
            yield OutputPanel()
            yield HistorySidebar()
        yield InputPanel()
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        # 主题 CSS 注入
        try:
            theme = self._theme_provider.load(self._config.theme)
            base_css = self._load_base_css()
            self.stylesheet.add_source(base_css)
            self.stylesheet.add_source(theme.css)
        except Exception:
            log.exception("theme load failed, using default")
        # 启动界面
        if self._config.startup_enabled and not self._skip_startup:
            self._show_startup()
        # 状态栏初始
        self._refresh_status()
        # 历史侧栏加载
        self._load_history_sidebar()
        # 会话事件
        get_global_bus().publish(
            Event(
                type=EVT_SESSION_STARTED,
                payload={"session_id": self._session_id, "platform": self._config.current_platform},
            )
        )
        self._running = True
        # 焦点
        self.query_one(InputPanel).focus()

    @staticmethod
    def _load_base_css() -> str:
        """读取 base.tcss"""
        p = Path(__file__).parent / "styles" / "base.tcss"
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    # === 启动界面 ===

    def _show_startup(self) -> None:
        """在 OutputPanel 显示启动画面 (简短停留)"""
        try:
            started = time.perf_counter()
            ctx = StartupContext(
                version=self._version(),
                platform=self._config.current_platform,
                python_version=self._python_version(),
                session_id=self._session_id,
                cwd=os.getcwd(),
                config_path=str(self._config.startup_config_resolved),
                last_session_summary=self._last_session_summary(),
                uptime_hint="",
            )
            text = self._startup_renderer.render(self._config.startup_template, ctx)
            out = self.query_one(OutputPanel)
            # 简单动画：spinner 首帧 + 文本
            frame = StartupAnimator.frame(self._config.startup_animation, text, 0, 1)
            out.write(frame if frame else text)
            duration = self._config.startup_duration_ms
            if duration > 0:
                self.set_timer(duration / 1000.0, lambda: out.write(""))
        except Exception:
            log.exception("startup screen failed")

    @staticmethod
    def _version() -> str:
        try:
            from . import __version__

            return __version__
        except Exception:
            return "0.1.0"

    @staticmethod
    def _python_version() -> str:
        return ".".join(map(str, sys.version_info[:3]))

    def _last_session_summary(self) -> str:
        try:
            recs = self._history.recent(limit=3, session_id=None)
            if not recs:
                return ""
            return " | ".join(r.raw_input for r in recs)
        except Exception:
            return ""

    # === 历史侧栏 ===

    def _load_history_sidebar(self) -> None:
        try:
            recs = self._history.recent(limit=50, session_id=None)
            entries = [HistoryEntry(r.raw_input, r.exit_code, r.id) for r in recs]
            self.query_one(HistorySidebar).load_entries(entries)
        except Exception:
            log.exception("load history sidebar failed")

    def on_history_sidebar_history_selected(self, event: HistorySidebar.HistorySelected) -> None:
        """历史条目选中 → 回填输入框"""
        self.query_one(InputPanel).fill(event.raw_input)

    # === 命令执行 ===

    async def on_input_panel_submitted(self, event: InputPanel.Submitted) -> None:
        """回车提交命令"""
        text = event.value.strip()
        inp = self.query_one(InputPanel)
        inp.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        await self._exec_command(text)

    async def _exec_command(self, text: str) -> None:
        """通道路由：先 P4 函数，否则 shell"""
        if self._agent_enabled and self._func_registry is not None:
            tokens = text.split()
            first = tokens[0] if tokens else ""
            reg = self._func_registry.match(first)
            if reg is not None:
                rest = text[len(first):].strip()
                await self._exec_agent(text, reg, rest)
                return
        await self._exec_shell(text)

    async def _exec_shell(self, text: str) -> None:
        """shell 通道 (P1-P3 管线)"""
        out = self.query_one(OutputPanel)
        out.write(f"[dim]$ {text}[/]")
        try:
            result = await self._pipeline.run(text, session_id=self._session_id)
        except Exception as e:
            log.exception("pipeline crashed")
            out.write_error("draco.exec.crash", str(e))
            return

        if result.blocked:
            out.write_block(result.block_reason)
            self._refresh_status(exit_code=None, clear_exit=True)
            return
        if result.error is not None:
            out.write_error(result.error.code, result.error.message)
            self._refresh_status(clear_exit=True)
            return
        # 输出
        out.write_stdout(result.stdout)
        if result.stderr:
            out.write_stderr(result.stderr)
        out.write_exit_code(result.exit_code, result.alias_used)
        # 历史侧栏推最新
        self.query_one(HistorySidebar).push_latest(
            HistoryEntry(result.raw_input, result.exit_code, None)
        )
        self._refresh_status(exit_code=result.exit_code)
        # P3 纠错建议
        if self._ai_enabled and self._correction_hook is not None:
            await self._maybe_render_correction()
        # P3 感知建议
        if self._ai_enabled and self._perception is not None:
            self._maybe_render_suggestions()

    async def _maybe_render_correction(self) -> None:
        if self._correction_hook is None:
            return
        pending = self._correction_hook.pop_pending_correction()
        if pending is None:
            return
        raw, suggested = pending
        if not suggested:
            return
        out = self.query_one(OutputPanel)
        out.write_suggestion(suggested_command=suggested)
        ok = await self._confirmer.ask_yes("应用建议命令？", default=False)
        if ok:
            out.write(f"[dim]→ 执行: {suggested}[/]")
            await self._exec_shell(suggested)

    def _maybe_render_suggestions(self) -> None:
        if self._perception is None:
            return
        try:
            suggestions = self._perception.get_suggestions()
        except Exception:
            return
        if suggestions:
            self.query_one(OutputPanel).write_suggestion(suggestions=list(suggestions))

    async def _exec_agent(self, raw_input: str, reg_func: RegisteredFunction, args_str: str) -> None:
        """Python 通道 (P4)"""
        out = self.query_one(OutputPanel)
        out.write(f"[dim magenta]$ {raw_input}[/]")
        if self._py_executor is None:
            out.write_error("draco.agent", "Agent 通道未初始化")
            return
        try:
            bound_args, bound_kwargs = parse_call_args(
                args_str,
                reg_func.param_names_no_ctx,
                reg_func.param_types,
                reg_func.defaults,
            )
        except ArgParseError as e:
            out.write_error("draco.agent.args", e.message)
            return
        try:
            result: AgentExecResult = await self._py_executor.execute(
                reg_func, bound_args, bound_kwargs,
                cwd=os.getcwd(), session_id=self._session_id,
            )
        except Exception as e:
            log.exception("agent executor crashed")
            out.write_error("draco.agent.crash", str(e))
            return
        out.write_agent_result(result.func_name, result.success, result.error or "", result.stdout)
        if result.return_value is not None:
            import json
            try:
                s = json.dumps(result.return_value, ensure_ascii=False, default=str, indent=2)
                out.write(f"[dim]→ {s}[/]")
            except (TypeError, ValueError):
                out.write(f"[dim]→ {result.return_value!r}[/]")
        self._refresh_status(exit_code=result.exit_code)
        self.query_one(HistorySidebar).push_latest(
            HistoryEntry(raw_input, result.exit_code, None)
        )

    # === slash 命令 ===

    async def _handle_slash(self, text: str) -> None:
        out = self.query_one(OutputPanel)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            self._do_quit()
            return
        if cmd in ("/help", "/h", "/?"):
            out.write(self._help_text())
            return
        if cmd == "/clear":
            out.clear_output()
            return
        if cmd == "/alias":
            self._handle_alias_cmd(arg)
            return
        if cmd == "/aliases":
            self._list_aliases()
            return
        if cmd == "/history":
            self._show_history(arg)
            return
        if cmd == "/risk":
            self._handle_risk_cmd(arg)
            return
        if cmd == "/theme":
            self._handle_theme_cmd(arg)
            return
        if cmd == "/ai":
            await self._handle_ai_cmd(arg)
            return
        if cmd == "/agent":
            await self._handle_agent_cmd(arg)
            return
        out.write_error("draco.cli.unknown", f"未知命令: {cmd}（/help 查看可用命令）")

    def _help_text(self) -> str:
        return (
            "[bold]内置命令:[/]\n"
            "  /help                 显示帮助\n"
            "  /quit                 退出\n"
            "  /clear                清屏\n"
            "  /alias <n> <exp>      添加/更新别名\n"
            "  /aliases              列出别名\n"
            "  /history [N]          显示最近 N 条历史\n"
            "  /risk                 显示风险规则表\n"
            "  /risk test <cmd>      模拟评估命令风险\n"
            "  /theme                列出主题\n"
            "  /theme <name>         切换主题\n"
            "  /ai                   AI 状态\n"
            "  /ai on|off            启用/禁用 AI\n"
            "  /agent                Agent 状态\n"
            "  /agent on|off|list|run <name> [args]  Agent 控制\n"
            "  [dim]快捷键: Ctrl+T 主题 / Ctrl+H 历史栏 / Ctrl+L 清屏 / Ctrl+E AI / Ctrl+A Agent / Ctrl+D 退出[/]"
        )

    def _handle_alias_cmd(self, arg: str) -> None:
        out = self.query_one(OutputPanel)
        arg = arg.strip()
        if not arg:
            self._list_aliases()
            return
        if arg.startswith("-d "):
            name = arg[3:].strip()
            if self._aliases.remove(name):
                out.write_success(f"已删除别名: {name}")
            else:
                out.write_error("draco.alias", f"别名不存在: {name}")
            return
        parts = arg.split(maxsplit=1)
        if len(parts) == 1:
            exp = self._aliases.get(parts[0])
            if exp is not None:
                out.write_info(f"{parts[0]} = {exp}")
            else:
                out.write_error("draco.alias", f"别名不存在: {parts[0]}")
            return
        name, expansion = parts
        is_new = self._aliases.add(name, expansion)
        out.write_success(f"{'已添加' if is_new else '已更新'}别名: {name} = {expansion}")

    def _list_aliases(self) -> None:
        out = self.query_one(OutputPanel)
        aliases = self._aliases.list()
        if not aliases:
            out.write_info("（无别名）")
            return
        for name in sorted(aliases):
            out.write_info(f"  {name} = {aliases[name]}")

    def _show_history(self, arg: str) -> None:
        out = self.query_one(OutputPanel)
        try:
            n = int(arg) if arg.strip() else 20
        except ValueError:
            n = 20
        try:
            recs = self._history.recent(limit=n, session_id=None)
        except DracoError as e:
            out.write_error(e.code, e.message)
            return
        if not recs:
            out.write_info("（无历史记录）")
            return
        out.write_info(f"最近 {len(recs)} 条历史:")
        for r in reversed(recs):
            status = "OK" if r.exit_code == 0 else (f"×{r.exit_code}" if r.exit_code is not None else "?")
            out.write_info(f"  [{status}] {r.raw_input}")

    def _handle_risk_cmd(self, arg: str) -> None:
        out = self.query_one(OutputPanel)
        from ..security.risk_assessor import RiskLevel

        arg = arg.strip()
        if not arg:
            level_order = [RiskLevel.CRITICAL, RiskLevel.DANGER, RiskLevel.CAUTION]
            label = {RiskLevel.CRITICAL: "🚨", RiskLevel.DANGER: "🔴", RiskLevel.CAUTION: "⚠️"}
            out.write_info("风险规则表:")
            for level in level_order:
                for pat in self._assessor._patterns_by_level[level]:
                    out.write_info(
                        f"  [{label[level]} {level.value}] {pat.cmd} contain=[{','.join(pat.args_contain) or '-'}] {pat.desc}"
                    )
            return
        parts = arg.split(maxsplit=1)
        if parts[0].lower() == "test" and len(parts) == 2:
            self._preview_risk(parts[1])
            return
        out.write_error("draco.cli", "用法: /risk test <command>")

    def _preview_risk(self, command: str) -> None:
        out = self.query_one(OutputPanel)
        from ..shell.normalizer import normalize
        from ..shell.parser import parse

        try:
            ir = parse(command)
            normalize(ir)
            ir, _ = self._pipeline._expander.expand(ir)
            self._pipeline._mapper.map(ir)
        except DracoError as e:
            out.write_error(e.code, e.message)
            return
        assessment = self._assessor.assess(ir)
        out.write_info(f"风险预览: {assessment.level.value} — {assessment.reason}")
        for m in assessment.matched_rules:
            out.write_info(f"  命中: [{m.level.value}] {m.cmd}: {m.desc}")

    def _handle_theme_cmd(self, arg: str) -> None:
        out = self.query_one(OutputPanel)
        arg = arg.strip()
        if not arg:
            names = self._theme_provider.list_themes()
            current = self._theme_provider._current
            out.write_info("可用主题: " + ", ".join(names) + f" (当前: {current})")
            return
        try:
            theme = self._theme_provider.switch(arg)
            self.stylesheet.add_source(theme.css)
            self._refresh_status(theme=arg)
            out.write_success(f"已切换主题: {arg}")
        except Exception as e:
            out.write_error("draco.ui.theme", str(e))

    async def _handle_ai_cmd(self, arg: str) -> None:
        out = self.query_one(OutputPanel)
        arg = arg.strip()
        if not arg:
            self._show_ai_status()
            return
        sub = arg.split(maxsplit=1)[0].lower()
        if sub == "on":
            self._ai_on()
            return
        if sub == "off":
            self._ai_off()
            return
        out.write_info("textual 模式下 /ai 支持: on / off / (状态)。完整子命令请在 simple 模式使用 (--renderer simple)")

    def _show_ai_status(self) -> None:
        out = self.query_one(OutputPanel)
        enabled = self._ai_enabled
        available = self._llm_client.is_available() if (enabled and self._llm_client) else False
        out.write_info(
            f"AI 状态: 启用={'是' if enabled else '否'} 可用={'是' if available else '否'} "
            f"模型={self._config.ai_model}"
        )

    def _ai_on(self) -> None:
        out = self.query_one(OutputPanel)
        if self._ai_enabled:
            out.write_info("AI 已启用")
            return
        self._config.ai_enabled = True
        reset_global_client()
        from ..ai.character import reset_global_registry as reset_roles

        reset_roles()
        # 重建 hook 注册表保留 risk_hook
        new_reg = HookRegistry()
        new_reg.register_pre(self._risk_hook)
        from ..hooks.registry import set_global_registry

        set_global_registry(new_reg)
        self._pipeline._hooks = new_reg
        self._setup_ai()
        self._ai_enabled = True
        out.write_success(f"AI 已启用 (model={self._config.ai_model})")

    def _ai_off(self) -> None:
        out = self.query_one(OutputPanel)
        if not self._ai_enabled:
            out.write_info("AI 已关闭")
            return
        new_reg = HookRegistry()
        new_reg.register_pre(self._risk_hook)
        from ..hooks.registry import set_global_registry

        set_global_registry(new_reg)
        self._pipeline._hooks = new_reg
        self._ai_enabled = False
        self._correction_hook = None
        self._perception_hook = None
        self._ai_risk_hook = None
        out.write_success("AI 已禁用")

    async def _handle_agent_cmd(self, arg: str) -> None:
        out = self.query_one(OutputPanel)
        arg = arg.strip()
        if not arg:
            self._show_agent_status()
            return
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if sub == "on":
            self._agent_on()
            return
        if sub == "off":
            self._agent_off()
            return
        if sub == "list":
            self._agent_list()
            return
        if sub == "show" and rest:
            self._agent_show(rest.strip())
            return
        if sub == "reload":
            self._agent_reload()
            return
        if sub == "run" and rest:
            await self._agent_run(rest)
            return
        if sub == "templates":
            self._agent_templates()
            return
        out.write_info("textual 模式下 /agent 支持: on/off/list/show/reload/run/templates")

    def _show_agent_status(self) -> None:
        out = self.query_one(OutputPanel)
        n = len(self._func_registry.list()) if self._func_registry else 0
        out.write_info(
            f"Agent 状态: 启用={'是' if self._agent_enabled else '否'} 函数={n} 文件={self._config.functions_file}"
        )
        self._refresh_status()

    def _agent_on(self) -> None:
        out = self.query_one(OutputPanel)
        if self._agent_enabled:
            out.write_info("Agent 已启用")
            return
        self._agent_enabled = True
        self._config.agent_enabled = True
        self._setup_agent()
        n = len(self._func_registry.list()) if self._func_registry else 0
        out.write_success(f"Agent 已启用 ({n} 个函数)")
        self._refresh_status(func_count=n, agent=True)

    def _agent_off(self) -> None:
        out = self.query_one(OutputPanel)
        if not self._agent_enabled:
            out.write_info("Agent 已关闭")
            return
        reset_global_func_registry()
        self._func_registry = None
        self._py_executor = None
        self._code_generator = None
        self._code_runner = None
        self._agent_enabled = False
        out.write_success("Agent 已禁用")
        self._refresh_status(agent=False, func_count=0)

    def _agent_list(self) -> None:
        out = self.query_one(OutputPanel)
        if self._func_registry is None:
            out.write_error("draco.agent", "Agent 未启用，/agent on")
            return
        funcs = self._func_registry.list()
        if not funcs:
            out.write_info("（无函数）")
            return
        for name, rf in sorted(funcs.items()):
            params = ", ".join(rf.param_names_no_ctx) or "-"
            doc = rf.doc.split("\n")[0][:40] if rf.doc else "-"
            out.write_info(f"  {'async ' if rf.is_async else ''}{name}({params}) — {doc}")

    def _agent_show(self, name: str) -> None:
        out = self.query_one(OutputPanel)
        if self._func_registry is None:
            out.write_error("draco.agent", "Agent 未启用")
            return
        rf = self._func_registry.get(name)
        if rf is None:
            out.write_error("draco.agent", f"函数不存在: {name}")
            return
        out.write_code(rf.source, "python")

    def _agent_reload(self) -> None:
        out = self.query_one(OutputPanel)
        if self._func_registry is None:
            out.write_error("draco.agent", "Agent 未启用")
            return
        try:
            n = self._func_registry.reload()
            out.write_success(f"重新加载完成 ({n} 个函数)")
            self._refresh_status(func_count=n)
        except DracoError as e:
            out.write_error(e.code, e.message)

    async def _agent_run(self, rest: str) -> None:
        if self._func_registry is None:
            self.query_one(OutputPanel).write_error("draco.agent", "Agent 未启用")
            return
        parts = rest.split(maxsplit=1)
        name = parts[0]
        args_str = parts[1] if len(parts) > 1 else ""
        rf = self._func_registry.get(name)
        if rf is None:
            self.query_one(OutputPanel).write_error("draco.agent", f"函数不存在: {name}")
            return
        await self._exec_agent(rest, rf, args_str)

    def _agent_templates(self) -> None:
        out = self.query_one(OutputPanel)
        templates = list_templates()
        if not templates:
            out.write_info("（无模板）")
            return
        for t in templates:
            out.write_info(f"  {t.name} — {t.description}")

    # === 状态栏 ===

    def _refresh_status(
        self,
        *,
        exit_code: Optional[int] = None,
        clear_exit: bool = False,
        theme: Optional[str] = None,
        agent: Optional[bool] = None,
        func_count: Optional[int] = None,
    ) -> None:
        try:
            bar = self.query_one(StatusBar)
        except Exception:
            return
        bar.update_state(
            platform=self._config.current_platform,
            cwd=os.getcwd(),
            session=self._session_id,
            exit_code=exit_code,
            clear_exit=clear_exit,
            theme=theme or self._theme_provider._current,
            ai=self._ai_enabled,
            agent=self._agent_enabled if agent is None else agent,
            func_count=func_count if func_count is not None else (
                len(self._func_registry.list()) if self._func_registry else 0
            ),
        )

    # === 快捷键动作 ===

    def action_switch_theme(self) -> None:
        out = self.query_one(OutputPanel)
        names = self._theme_provider.list_themes()
        if not names:
            out.write_error("draco.ui.theme", "无可用主题")
            return
        current = self._theme_provider._current
        try:
            idx = names.index(current)
        except ValueError:
            idx = -1
        nxt = names[(idx + 1) % len(names)]
        try:
            theme = self._theme_provider.switch(nxt)
            self.stylesheet.add_source(theme.css)
            self._refresh_status(theme=nxt)
            out.write_success(f"主题: {nxt}")
        except Exception as e:
            out.write_error("draco.ui.theme", str(e))

    def action_toggle_history(self) -> None:
        self.query_one(HistorySidebar).toggle_hidden()

    def action_clear_output(self) -> None:
        self.query_one(OutputPanel).clear_output()

    def action_toggle_ai(self) -> None:
        if self._ai_enabled:
            self._ai_off()
        else:
            self._ai_on()

    def action_toggle_agent(self) -> None:
        if self._agent_enabled:
            self._agent_off()
        else:
            self._agent_on()

    def action_quit(self) -> None:
        self._do_quit()

    def _do_quit(self) -> None:
        self._running = False
        get_global_bus().publish(
            Event(type=EVT_SESSION_ENDED, payload={"session_id": self._session_id})
        )
        try:
            self._history.close()
        except Exception:
            pass
        self.exit()


def run_textual(config: Optional[DracoConfig] = None, *, skip_startup: bool = False) -> None:
    """启动 textual TUI"""
    app = DracoApp(config, skip_startup=skip_startup)
    app.run()
