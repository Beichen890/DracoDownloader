"""OpenDracoCLI TUI 主循环 — prompt_toolkit 输入 + rich 渲染

特性:
  - prompt_toolkit 提供输入行：历史回溯（上下方向键）、Tab 补全、多行
  - rich 渲染输出（彩色 stdout/stderr、错误面板）
  - 内置 slash 命令：/alias /aliases /history /risk /ai /agent /help /quit
  - P2 风控：RiskAssessmentHook + SandboxExecutor 自动接线
  - P3 AI：CorrectionHook + PerceptionHook + AIRiskHook（ai_enabled 时接线）
  - P4 Agent：PythonChannelExecutor + /agent 命令族（agent_enabled 时接线）
  - 会话 ID（启动生成）
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

from .agent.code_generator import CodeGenerator, GeneratedFunction
from .agent.code_runner import CodeRunner
from .agent.context import AgentContext
from .agent.function_registry import FunctionRegistry, get_global_registry as get_global_func_registry
from .agent.function_registry import reset_global_registry as reset_global_func_registry
from .agent.python_executor import AgentExecResult, PythonChannelExecutor
from .agent.arg_parser import ArgParseError, parse_call_args
from .agent.templates import get_template, list_templates
from .ai.character import RoleRegistry, get_global_registry as get_global_role_registry
from .ai.client import LLMClient, get_global_client, reset_global_client
from .ai.corrector import Corrector
from .ai.emotion import EmotionEngine
from .ai.perception import PerceptionEngine
from .ai.risk_advisor import RiskAdvisor
from .aliases.manager import AliasManager, get_global_manager
from .config import DracoConfig, get_global_config
from .errors import DracoError, make_error
from .events import EVT_SESSION_ENDED, EVT_SESSION_STARTED, Event, get_global_bus
from .history.store import HistoryStore, get_global_store
from .hooks.ai_risk_hook import AIRiskHook
from .hooks.correction_hook import CorrectionHook
from .hooks.perception_hook import PerceptionHook
from .hooks.risk_hook import RiskAssessmentHook
from .hooks.registry import get_global_registry
from .logger import get_logger
from .security.authenticator import Authenticator
from .security.confirmer import Confirmer
from .security.risk_assessor import RiskAssessor, get_global_assessor
from .security.sandbox import SandboxExecutor
from .shell.pipeline import ShellPipeline

log = get_logger("cli")


def _get_prompt_text(cwd: Optional[str] = None) -> str:
    """生成提示符（含 cwd）"""
    cwd = cwd or os.getcwd()
    # 缩短 home 路径
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    return f"draco {cwd}> "


class OpenDracoCLI:
    """TUI 主应用"""

    def __init__(
        self,
        config: Optional[DracoConfig] = None,
        alias_manager: Optional[AliasManager] = None,
        history_store: Optional[HistoryStore] = None,
    ) -> None:
        self._config = config or get_global_config()
        self._aliases = alias_manager or get_global_manager()
        self._history = history_store or get_global_store()

        # P2: 接线风控组件
        self._assessor = get_global_assessor(self._config)
        self._confirmer = Confirmer()
        self._authenticator = Authenticator(
            hash_path=self._config.auth_hash_file_resolved,
            iterations=self._config.pbkdf2_iterations,
        )
        self._sandbox = SandboxExecutor(
            inner=None,  # 占位，pipeline 创建后再绑定
            writable_paths=self._config.sandbox_writable_paths_resolved,
        )

        self._pipeline = ShellPipeline(
            config=self._config,
            alias_manager=self._aliases,
            history_store=self._history,
        )
        # 绑定沙箱的 inner executor 为 pipeline 的原生 executor
        self._sandbox._inner = self._pipeline._executor
        self._pipeline.set_sandbox_executor(self._sandbox)

        # P4 Agent 标志（需在 _setup_draco_funcs 之前，决定是否加载用户函数）
        self._agent_enabled = bool(self._config.agent_enabled)

        # Draco 内置函数：默认注册（不依赖 agent_enabled）
        # 命中 Draco 函数走 Python 通道，否则透传原生 shell
        self._func_registry: Optional[FunctionRegistry] = None
        self._py_executor: Optional[PythonChannelExecutor] = None
        self._code_generator: Optional[CodeGenerator] = None
        self._code_runner: Optional[CodeRunner] = None
        self._setup_draco_funcs()

        # 注册 RiskAssessmentHook（priority=10，最先执行）
        self._risk_hook = RiskAssessmentHook(
            assessor=self._assessor,
            confirmer=self._confirmer,
            authenticator=self._authenticator,
            config=self._config,
        )
        get_global_registry().register_pre(self._risk_hook)

        # P3 AI 智能层（条件接线）
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

        # P4 Agent 自动化（条件接线，仅初始化代码生成器）
        # _agent_enabled 已在 _setup_draco_funcs 之前设置
        if self._agent_enabled:
            self._setup_agent()

        self._session_id = str(uuid.uuid4())
        self._running = False
        # session cwd：cd 命令返回 new_cwd，更新此值并传给 pipeline（持久化）
        self._session_cwd = os.getcwd()

    def _setup_draco_funcs(self) -> None:
        """注册 Draco 内置函数并接入 pipeline

        内置函数始终注册（用 Python 库实现跨平台任务）。
        用户函数文件仅在 agent_enabled 时叠加注册。
        """
        from .draco_funcs.registry import register_builtins

        self._func_registry = get_global_func_registry()
        n_builtin = register_builtins(self._func_registry)
        log.info("注册 %d 个内置 Draco 函数", n_builtin)

        # 用户函数文件仅在 agent_enabled 时加载
        if self._agent_enabled:
            try:
                self._func_registry.set_user_file(self._config.functions_file_resolved)
                n_user = self._func_registry.load_user_file(self._config.functions_file_resolved)
                if n_user > 0:
                    log.info("加载 %d 个用户函数", n_user)
            except DracoError as e:
                log.warning("加载用户函数文件失败: %s", e)

        # Python 通道执行器（共享 history + 事件）
        self._py_executor = PythonChannelExecutor(
            config=self._config,
            history_store=self._history,
            shell_runner=self._shell_runner_for_agent,
        )

        # 接入 pipeline
        self._pipeline.set_function_registry(self._func_registry)
        self._pipeline.set_python_executor(self._py_executor)

    def _setup_ai(self) -> None:
        """初始化 P3 AI 组件并注册钩子"""
        # LLM 客户端（全局单例，便于 /ai 命令复用）
        self._llm_client = get_global_client(self._config)

        # 角色注册表
        self._role_registry = get_global_role_registry()

        # 情感引擎
        self._emotion_engine = EmotionEngine(
            weights=self._config.emotion_weights,
            inertia=self._config.emotion_inertia,
        )

        # 感知引擎
        self._perception = PerceptionEngine(
            context_file=self._config.ai_context_file_resolved,
            emotion_engine=self._emotion_engine,
        )

        # AI 纠错器
        self._corrector = Corrector(
            self._llm_client,
            platform=self._config.current_platform,
            native_shell=self._config.native_shell[0],
        )

        # AI 增强风控
        self._risk_advisor = RiskAdvisor(
            self._llm_client,
            platform=self._config.current_platform,
            native_shell=self._config.native_shell[0],
        )

        # 注册 PostExecHook（纠错 + 感知）
        self._correction_hook = CorrectionHook(
            client=self._llm_client,
            corrector=self._corrector,
            role_registry=self._role_registry,
            perception=self._perception,
            confirmer=self._confirmer,
            config=self._config,
        )
        get_global_registry().register_post(self._correction_hook)

        if self._config.ai_perception_enabled:
            self._perception_hook = PerceptionHook(
                perception=self._perception,
                config=self._config,
            )
            get_global_registry().register_post(self._perception_hook)

        # 注册 PreExecHook（AI 风控，priority=20，在 P2 risk_hook 之后）
        if self._config.ai_risk_enabled:
            self._ai_risk_hook = AIRiskHook(
                advisor=self._risk_advisor,
                config=self._config,
            )
            get_global_registry().register_pre(self._ai_risk_hook)

        log.info("P3 AI 智能层已启用 (model=%s)", self._config.ai_model)

    def _teardown_ai(self) -> None:
        """卸载 P3 AI 组件（/ai off 时调用）"""
        # 注：钩子注册表不提供 unregister，这里通过重建注册表实现
        from .hooks.registry import HookRegistry, set_global_registry

        new_reg = HookRegistry()
        # 重新注册 P2 风控钩子
        new_reg.register_pre(self._risk_hook)
        set_global_registry(new_reg)
        self._ai_enabled = False
        self._llm_client = None
        self._role_registry = None
        self._emotion_engine = None
        self._perception = None
        self._corrector = None
        self._risk_advisor = None
        self._correction_hook = None
        self._perception_hook = None
        self._ai_risk_hook = None
        # 更新 pipeline 的 hook 引用
        self._pipeline._hooks = new_reg
        log.info("P3 AI 智能层已禁用")

    def _setup_agent(self) -> None:
        """初始化 P4 Agent 组件（代码生成器）

        Draco 函数注册和 Python 执行器已在 _setup_draco_funcs 完成，
        这里只负责代码生成器（需要 LLMClient）。
        """
        self._code_runner = CodeRunner()

        # 代码生成器（复用 P3 LLMClient，若 AI 未启用则不可用）
        client = self._llm_client or get_global_client(self._config)
        self._code_generator = CodeGenerator(
            client,
            platform=self._config.current_platform,
            max_tokens=self._config.agent_max_gen_tokens,
            runner=self._code_runner,
        )

        log.info("P4 Agent 自动化已启用 (functions=%s)", self._config.functions_file)

    def _teardown_agent(self) -> None:
        """卸载 P4 Agent 组件（代码生成器 + 用户函数）

        注意：内置 Draco 函数和 Python 执行器是默认基础设施，不在此清理。
        """
        self._code_generator = None
        self._code_runner = None
        self._agent_enabled = False
        # 卸载用户函数（保留内置 Draco 函数）
        if self._func_registry is not None:
            self._func_registry.clear_user_functions()
        log.info("P4 Agent 自动化已禁用（内置 Draco 函数保留）")

    async def _shell_runner_for_agent(self, cmd: str):
        """AgentContext.shell 的 runner — 调 ShellPipeline（含 P2 风控 + P3 AI）"""
        return await self._pipeline.run(cmd, session_id=self._session_id, cwd=self._session_cwd)

    async def run(self) -> None:
        """启动 TUI 主循环"""
        self._running = True
        get_global_bus().publish(
            Event(
                type=EVT_SESSION_STARTED,
                payload={"session_id": self._session_id, "platform": self._config.current_platform},
            )
        )

        # rich 可用则用它打印启动横幅
        try:
            from rich.console import Console
            from rich.panel import Panel

            console = Console()
            ai_status = (
                f"[green]on[/] ({self._config.ai_model})"
                if self._ai_enabled
                else "[dim]off[/]"
            )
            agent_status = (
                f"[green]on[/] ({len(self._func_registry.list()) if self._func_registry else 0} funcs)"
                if self._agent_enabled
                else "[dim]off[/]"
            )
            console.print(
                Panel.fit(
                    "[bold cyan]OpenDracoCLI[/] — AI 时代的智能终端 (P4)\n"
                    f"平台: {self._config.current_platform}  会话: {self._session_id[:8]}  AI: {ai_status}  Agent: {agent_status}\n"
                    "输入 [green]/help[/] 查看内置命令，[green]/ai[/] AI 状态，[green]/agent[/] Agent 状态，[green]/quit[/] 退出",
                    border_style="cyan",
                )
            )
            self._rich = True
        except ImportError:
            self._rich = False
            print("OpenDracoCLI (P4) — /help for commands, /ai for AI, /agent for Agent, /quit to exit")

        # 历史 tail 提示
        try:
            recent = self._history.recent(limit=3, session_id=None)
            if recent:
                print(f"(最近 {len(recent)} 条历史可用方向键回溯)")
        except Exception:
            pass

        # prompt_toolkit 历史
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import InMemoryHistory

            # 从 SQLite 加载历史到内存（prompt_toolkit 的方向键回溯）
            pt_history = InMemoryHistory()
            try:
                for rec in reversed(self._history.recent(limit=500, session_id=None)):
                    pt_history.append_string(rec.raw_input)
            except Exception:
                pass

            session = PromptSession(history=pt_history)
            self._pt_session = session
            self._pt_available = True
        except ImportError:
            self._pt_available = False
            self._pt_session = None

        while self._running:
            try:
                if self._pt_available:
                    text = await session.prompt_async(_get_prompt_text(self._session_cwd))
                else:
                    # 回退到 input()
                    text = input(_get_prompt_text(self._session_cwd))
            except (EOFError, KeyboardInterrupt):
                # Ctrl+D / Ctrl+C 退出
                print()
                break

            text = text.strip()
            if not text:
                continue

            # slash 命令
            if text.startswith("/"):
                handled = await self._handle_slash(text)
                if handled == "quit":
                    break
                continue

            # 执行命令
            await self._exec_command(text)

        self._running = False
        get_global_bus().publish(
            Event(type=EVT_SESSION_ENDED, payload={"session_id": self._session_id})
        )
        try:
            self._history.close()
        except Exception:
            pass

    async def _exec_command(self, text: str) -> None:
        """执行一条命令并渲染输出

        P4: 先判断是否命中已注册函数（Python 通道），否则走 shell 通道
        """
        # P4: 通道路由 — 首 token 命中函数注册表则走 Python 通道
        if self._agent_enabled and self._func_registry is not None:
            tokens = text.split()
            first_token = tokens[0] if tokens else ""
            reg = self._func_registry.match(first_token)
            if reg is not None:
                # 剩余部分作为参数
                rest = text[len(first_token):].strip()
                await self._exec_agent(text, reg, rest)
                return

        await self._exec_shell(text)

    async def _exec_shell(self, text: str) -> None:
        """走 shell 通道（P1-P3 管线）"""
        try:
            result = await self._pipeline.run(
                text, session_id=self._session_id, cwd=self._session_cwd
            )
        except Exception as e:
            log.exception("pipeline crashed")
            self._render_error(f"管线异常: {e}")
            return

        if result.blocked:
            self._render_block(result)
            return

        if not result.success and result.error is not None:
            self._render_draco_error(result.error)
            return

        # cd 等命令更新 session cwd（持久化，后续命令用新目录）
        if result.new_cwd:
            self._session_cwd = result.new_cwd

        # 渲染 stdout/stderr
        if result.stdout:
            sys.stdout.write(result.stdout)
            if not result.stdout.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        if result.stderr:
            sys.stderr.write(result.stderr)
            if not result.stderr.endswith("\n"):
                sys.stderr.write("\n")
            sys.stderr.flush()

        # 非零退出码提示
        if result.exit_code not in (0, None):
            self._render_error(
                f"退出码 {result.exit_code}" + (f"（别名 {result.alias_used}）" if result.alias_used else "")
            )

        # P3: 渲染 AI 纠错建议（如有）
        if self._ai_enabled and self._correction_hook is not None:
            await self._maybe_render_correction()

        # P3: 渲染感知主动建议（如有）
        if self._ai_enabled and self._perception is not None:
            self._maybe_render_suggestions()

    async def _exec_agent(self, raw_input: str, reg_func, args_str: str) -> None:
        """走 Python 通道执行用户函数"""
        if self._py_executor is None:
            self._render_error("Agent 通道未初始化")
            return

        # 参数解析与绑定
        try:
            bound_args, bound_kwargs = parse_call_args(
                args_str,
                reg_func.param_names_no_ctx,
                reg_func.param_types,
                reg_func.defaults,
            )
        except ArgParseError as e:
            self._render_draco_error(make_error("draco.agent.args", reason=e.message))
            return

        # 执行
        try:
            result: AgentExecResult = await self._py_executor.execute(
                reg_func,
                bound_args,
                bound_kwargs,
                cwd=os.getcwd(),
                session_id=self._session_id,
            )
        except Exception as e:
            log.exception("agent executor crashed")
            self._render_error(f"Agent 通道异常: {e}")
            return

        # 渲染输出
        if result.stdout:
            sys.stdout.write(result.stdout)
            if not result.stdout.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

        if not result.success:
            if result.stderr:
                sys.stderr.write(result.stderr)
                if not result.stderr.endswith("\n"):
                    sys.stderr.write("\n")
                sys.stderr.flush()
            self._render_error(
                f"函数 {result.func_name} 执行失败"
                + (f": {result.error}" if result.error else "")
            )
            return

        # 渲染返回值（若非 None 且 stdout 未含 print）
        if result.return_value is not None:
            rv = result.return_value
            if isinstance(rv, str):
                # 字符串返回值直接打印（若 stdout 已有输出则不重复）
                if not result.stdout:
                    print(rv)
            else:
                import json
                try:
                    # 优先 JSON 序列化
                    s = json.dumps(rv, ensure_ascii=False, default=str, indent=2)
                    if getattr(self, "_rich", False):
                        from rich.console import Console
                        Console().print(f"[dim]→ {s}[/]")
                    else:
                        print(f"→ {s}")
                except (TypeError, ValueError):
                    print(f"→ {rv!r}")

    async def _maybe_render_correction(self) -> None:
        """渲染并应用 AI 纠错建议"""
        if self._correction_hook is None:
            return
        pending = self._correction_hook.pop_pending_correction()
        if pending is None:
            return
        raw_input, suggested = pending
        if not suggested:
            return

        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.panel import Panel

            Console().print(
                Panel(
                    f"[cyan]建议命令:[/]\n[green]{suggested}[/]",
                    title="AI 纠错建议",
                    border_style="cyan",
                )
            )
        else:
            print(f"--- AI 纠错建议 ---")
            print(f"  {suggested}")
            print(f"-------------------")

        # 询问是否应用
        ok = await self._confirmer.ask_yes("应用建议命令？", default=False)
        if ok:
            # 走 shell 通道（含 P2 风控，不绕过）
            print(f"→ 执行: {suggested}")
            await self._exec_shell(suggested)

    def _maybe_render_suggestions(self) -> None:
        """渲染感知主动建议"""
        if self._perception is None:
            return
        try:
            suggestions = self._perception.get_suggestions()
        except Exception:
            return
        if not suggestions:
            return
        if getattr(self, "_rich", False):
            from rich.console import Console

            console = Console()
            for s in suggestions:
                console.print(f"[dim cyan]💡 {s}[/]")
        else:
            for s in suggestions:
                print(f"💡 {s}")

    async def _handle_slash(self, text: str) -> Optional[str]:
        """处理 / 开头的内置命令，返回 'quit' 或 None"""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            return "quit"
        if cmd in ("/help", "/h", "/?"):
            self._print_help()
            return None
        if cmd == "/alias":
            self._handle_alias_cmd(arg)
            return None
        if cmd == "/aliases":
            self._list_aliases()
            return None
        if cmd == "/history":
            self._show_history(arg)
            return None
        if cmd == "/risk":
            self._handle_risk_cmd(arg)
            return None
        if cmd == "/ai":
            await self._handle_ai_cmd(arg)
            return None
        if cmd == "/agent":
            await self._handle_agent_cmd(arg)
            return None
        self._render_error(f"未知命令: {cmd}（/help 查看可用命令）")
        return None

    def _print_help(self) -> None:
        print("内置命令:")
        print("  /alias <name> <expansion>   添加/更新别名")
        print("  /alias <name>               查看别名")
        print("  /alias -d <name>            删除别名")
        print("  /aliases                    列出所有别名")
        print("  /history [N]                显示最近 N 条历史（默认 20）")
        print("  /risk                       显示当前风险规则表")
        print("  /risk test <command>        模拟评估命令风险（不执行）")
        print("  /ai                         显示 AI 状态")
        print("  /ai on                      启用 AI 智能层")
        print("  /ai off                     禁用 AI 智能层")
        print("  /ai ask <question>          向 AI 提问")
        print("  /ai correct <command>       手动让 AI 纠错某命令（不执行）")
        print("  /ai role [cmd]              查看/列出命令角色卡")
        print("  /ai context                 查看感知上下文")
        print("  /ai emotion                 查看当前情感状态")
        print("  /agent                      显示 Agent 状态")
        print("  /agent on                   启用 Agent 通道")
        print("  /agent off                  禁用 Agent 通道")
        print("  /agent list                 列出已注册函数")
        print("  /agent show <name>          查看函数源码")
        print("  /agent reload               重新加载 functions.py")
        print("  /agent gen <intent>         AI 生成函数（代码即行动）")
        print("  /agent run <name> [args]    显式调用函数")
        print("  /agent templates            列出可用模板")
        print("  /agent apply <template>     应用模板到 functions.py")
        print("  /help                       显示此帮助")
        print("  /quit                       退出")

    async def _handle_ai_cmd(self, arg: str) -> None:
        """处理 /ai 子命令"""
        arg = arg.strip()
        if not arg:
            self._show_ai_status()
            return
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "on":
            self._ai_on()
            return
        if sub == "off":
            self._ai_off()
            return
        if sub == "ask":
            if not rest:
                self._render_error("用法: /ai ask <question>")
                return
            await self._ai_ask(rest)
            return
        if sub == "correct":
            if not rest:
                self._render_error("用法: /ai correct <command>")
                return
            await self._ai_correct_manual(rest)
            return
        if sub == "role":
            self._ai_role(rest)
            return
        if sub == "context":
            self._ai_context()
            return
        if sub == "emotion":
            self._ai_emotion()
            return
        self._render_error(
            f"未知子命令: {sub}（可用: on/off/ask/correct/role/context/emotion）"
        )

    def _show_ai_status(self) -> None:
        """显示 AI 智能层状态"""
        enabled = self._ai_enabled
        client = self._llm_client
        if enabled and client is not None:
            available = client.is_available()
            model = self._config.ai_model
            key_hint = self._key_hint(self._config.ai_api_key_effective)
            api_base = self._config.ai_api_base
        else:
            available = False
            model = self._config.ai_model
            key_hint = "(未配置)"
            api_base = self._config.ai_api_base

        role_count = len(self._role_registry.list()) if self._role_registry else 0
        ctx_count = (
            self._perception.state.command_count if self._perception else 0
        )

        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(title="AI 智能层状态", border_style="cyan")
            table.add_column("项", style="bold")
            table.add_column("值")
            table.add_row("启用", "[green]是[/]" if enabled else "[dim]否[/]")
            table.add_row("可用", "[green]是[/]" if available else "[red]否[/]")
            table.add_row("模型", model)
            table.add_row("API Base", api_base)
            table.add_row("API Key", key_hint)
            table.add_row("角色卡数", str(role_count))
            table.add_row("上下文命令数", str(ctx_count))
            table.add_row("自动纠错", "是" if self._config.ai_auto_correct else "否")
            table.add_row("AI 风控", "是" if self._config.ai_risk_enabled else "否")
            table.add_row(
                "感知自互动",
                "是" if self._config.ai_perception_enabled else "否",
            )
            console.print(table)
        else:
            print(f"AI 智能层状态:")
            print(f"  启用: {'是' if enabled else '否'}")
            print(f"  可用: {'是' if available else '否'}")
            print(f"  模型: {model}")
            print(f"  API Base: {api_base}")
            print(f"  API Key: {key_hint}")
            print(f"  角色卡数: {role_count}")
            print(f"  上下文命令数: {ctx_count}")

    @staticmethod
    def _key_hint(key: str) -> str:
        """API key 只显示前后缀（隐私保护）"""
        if not key:
            return "(未配置)"
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}...{key[-4:]}"

    def _ai_on(self) -> None:
        """启用 AI 智能层"""
        if self._ai_enabled:
            # 已启用，刷新 key（用户可能刚设了环境变量）
            if self._llm_client is not None:
                self._llm_client.update_key(self._config.ai_api_key_effective)
            print("AI 已启用（已刷新配置）")
            return
        # 重新读取配置（用户可能刚设了 DRACO_AI_API_KEY）
        from .config import DracoConfig

        new_cfg = DracoConfig()
        # 继承旧配置的非 AI 字段
        for f_name in (
            "history_db_path", "aliases_file",
            "stdout_summary_lines", "stderr_summary_lines", "exec_timeout",
            "max_alias_depth", "log_level", "windows_shell", "unix_shell",
            "security_rules_file", "auth_hash_file", "sandbox_writable_paths",
            "require_auth_for", "pbkdf2_iterations",
        ):
            try:
                setattr(new_cfg, f_name, getattr(self._config, f_name))
            except Exception:
                pass
        self._config = new_cfg
        self._config.ai_enabled = True
        reset_global_client()
        from .ai.character import reset_global_registry as reset_roles
        reset_roles()
        self._setup_ai()
        self._ai_enabled = True
        print(f"✅ AI 已启用 (model={self._config.ai_model})")
        if not self._llm_client or not self._llm_client.is_available():
            print("⚠️  AI 不可用：请设置 DRACO_AI_API_KEY 环境变量或检查 api_base")

    def _ai_off(self) -> None:
        """禁用 AI 智能层"""
        if not self._ai_enabled:
            print("AI 已是关闭状态")
            return
        self._teardown_ai()
        print("✅ AI 已禁用")

    async def _ai_ask(self, question: str) -> None:
        """/ai ask — 向 AI 提问"""
        if not self._ai_enabled or self._llm_client is None:
            self._render_error("AI 未启用，先用 /ai on")
            return
        if not self._llm_client.is_available():
            self._render_error(
                "AI 不可用（未配置 API key 或 api_base 不可达）"
            )
            return

        # 角色匹配（无具体命令时用 DEFAULT_ROLE）
        from .ai.character import DEFAULT_ROLE
        from .ai.prompt import build_ask_prompt

        role = DEFAULT_ROLE
        emotion_hint = ""
        context_summary = ""
        if self._perception is not None:
            emotion_hint = self._perception.get_emotion_hint()
            context_summary = self._perception.get_context_summary()

        sys_prompt = build_ask_prompt(role, emotion_hint, context_summary)

        if getattr(self, "_rich", False):
            from rich.console import Console

            console = Console()
            console.print(f"[cyan]问:[/] {question}")
            console.print("[dim]思考中...[/]")
        else:
            print(f"问: {question}")
            print("思考中...")

        chunks: list[str] = []

        def on_chunk(ch: str) -> None:
            chunks.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()

        resp = await self._llm_client.chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": question},
            ],
            stream=True,
            on_chunk=on_chunk,
        )
        if not resp.ok:
            print()
            self._render_error(f"AI 调用失败: {resp.error}")
            return
        # 流式已打印 content，补一个换行
        if not chunks or not chunks[-1].endswith("\n"):
            print()

    async def _ai_correct_manual(self, command: str) -> None:
        """/ai correct — 手动让 AI 纠错某命令（不执行）"""
        if not self._ai_enabled or self._corrector is None or self._role_registry is None:
            self._render_error("AI 未启用，先用 /ai on")
            return
        if not self._llm_client or not self._llm_client.is_available():
            self._render_error("AI 不可用")
            return

        from .shell.parser import parse
        from .shell.normalizer import normalize

        try:
            ir = parse(command)
            normalize(ir)
        except DracoError as e:
            self._render_draco_error(e)
            return

        first_token = ir.nodes[0].name if ir.nodes else ""
        role = self._role_registry.match(first_token)
        emotion_hint = ""
        if self._perception is not None:
            emotion_hint = self._perception.get_emotion_hint()

        # 假装这条命令失败了（无 stderr/exit_code）让 AI 给出通用建议
        result = await self._corrector.correct(
            raw_input=command,
            exit_code=1,
            stderr="(用户主动请求纠错建议)",
            role=role,
            emotion_hint=emotion_hint,
        )
        if not result.ok:
            self._render_error(f"AI 纠错失败: {result.error}")
            return

        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.panel import Panel

            body = f"[cyan]分析:[/]\n{result.analysis}"
            if result.suggested_command:
                body += f"\n\n[cyan]建议命令:[/]\n[green]{result.suggested_command}[/]"
            Console().print(
                Panel(body, title=f"AI 纠错 — {role.name}", border_style="cyan")
            )
        else:
            print(f"--- AI 纠错（{role.name}）---")
            print(f"分析: {result.analysis}")
            if result.suggested_command:
                print(f"建议命令: {result.suggested_command}")
            print("------------------------")

    def _ai_role(self, arg: str) -> None:
        """/ai role [cmd] — 查看/列出命令角色卡"""
        if self._role_registry is None:
            self._render_error("AI 未启用，先用 /ai on")
            return
        arg = arg.strip()
        if arg:
            role = self._role_registry.get(arg)
            if role is None:
                # 尝试 match
                role = self._role_registry.match(arg)
                if role.cmd.lower() != arg.lower():
                    self._render_error(f"未找到角色卡: {arg}")
                    return
            self._print_role_detail(role)
            return
        # 列出全部
        roles = self._role_registry.list()
        if not roles:
            print("（无角色卡）")
            return
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            table = Table(title="命令角色卡", border_style="cyan")
            table.add_column("cmd", style="bold")
            table.add_column("名称")
            table.add_column("性格")
            table.add_column("说话风格")
            for cmd, role in sorted(roles.items()):
                table.add_row(
                    cmd,
                    role.name,
                    "/".join(role.personality_core) or "-",
                    role.speech_style or "-",
                )
            Console().print(table)
        else:
            print("命令角色卡:")
            for cmd, role in sorted(roles.items()):
                print(
                    f"  {cmd}  →  {role.name}  ({'/'.join(role.personality_core) or '-'})"
                )

    @staticmethod
    def _print_role_detail(role) -> None:
        """打印单个角色卡详情"""
        print(f"cmd: {role.cmd}")
        print(f"名称: {role.name}")
        print(f"骨架: {role.skeleton}")
        if role.personality_desc:
            print(f"性格: {role.personality_desc}")
        if role.speech_style:
            print(f"说话风格: {role.speech_style}")
        if role.speech_examples:
            print("对话示例:")
            for ex in role.speech_examples:
                print(f"  [{ex.get('context', '')}] {ex.get('line', '')}")
        if role.emotion_profile:
            print(f"情感基线: {role.emotion_profile}")
        if role.trigger_map:
            print(f"触发映射: {role.trigger_map}")

    def _ai_context(self) -> None:
        """/ai context — 查看感知上下文"""
        if self._perception is None:
            self._render_error("AI 未启用，先用 /ai on")
            return
        state = self._perception.state
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(title="感知上下文", border_style="cyan")
            table.add_column("项", style="bold")
            table.add_column("值")
            table.add_row("命令计数", str(state.command_count))
            table.add_row("连续失败", str(state.failure_streak))
            table.add_row(
                "常用命令",
                ", ".join(
                    f"{k}({v})"
                    for k, v in sorted(
                        state.frequent_commands.items(),
                        key=lambda x: x[1],
                        reverse=True,
                    )[:5]
                )
                or "(无)",
            )
            table.add_row(
                "最近命令",
                " | ".join(f"「{c}」" for c in state.last_commands[-3:]) or "(无)",
            )
            table.add_row(
                "常见错误",
                ", ".join(
                    f"{k[:40]}({v})"
                    for k, v in sorted(
                        state.common_errors.items(),
                        key=lambda x: x[1],
                        reverse=True,
                    )[:3]
                )
                or "(无)",
            )
            console.print(table)
        else:
            print(f"感知上下文:")
            print(f"  命令计数: {state.command_count}")
            print(f"  连续失败: {state.failure_streak}")
            print(f"  常用命令: {state.frequent_commands}")
            print(f"  最近命令: {state.last_commands}")
            print(f"  常见错误: {state.common_errors}")

    def _ai_emotion(self) -> None:
        """/ai emotion — 查看当前情感状态"""
        if self._perception is None:
            self._render_error("AI 未启用，先用 /ai on")
            return
        hint = self._perception.get_emotion_hint()
        vec = self._perception.state.last_emotion_vector
        if not hint:
            print("当前情感: 中性（无历史数据）")
            return
        print(f"当前情感: {hint}")
        if vec:
            from .ai.emotion import EMOTION_LABELS

            print("情感向量:")
            for label, val in zip(EMOTION_LABELS, vec):
                bar = "█" * int(val * 20)
                print(f"  {label}: {val:.3f} {bar}")

    async def _handle_agent_cmd(self, arg: str) -> None:
        """处理 /agent 子命令"""
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
        if sub == "show":
            if not rest:
                self._render_error("用法: /agent show <name>")
                return
            self._agent_show(rest.strip())
            return
        if sub == "reload":
            self._agent_reload()
            return
        if sub == "gen":
            if not rest:
                self._render_error("用法: /agent gen <intent>")
                return
            await self._agent_gen(rest)
            return
        if sub == "run":
            if not rest:
                self._render_error("用法: /agent run <name> [args]")
                return
            await self._agent_run(rest)
            return
        if sub == "templates":
            self._agent_templates()
            return
        if sub == "apply":
            if not rest:
                self._render_error("用法: /agent apply <template>")
                return
            self._agent_apply(rest.strip())
            return
        self._render_error(
            f"未知子命令: {sub}（可用: on/off/list/show/reload/gen/run/templates/apply）"
        )

    def _show_agent_status(self) -> None:
        """显示 Agent 通道状态"""
        enabled = self._agent_enabled
        func_count = len(self._func_registry.list()) if self._func_registry else 0
        user_funcs = self._func_registry.list_user() if self._func_registry else {}
        gen_available = (
            self._code_generator is not None
            and self._llm_client is not None
            and self._llm_client.is_available()
        )

        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(title="Agent 自动化状态", border_style="magenta")
            table.add_column("项", style="bold")
            table.add_column("值")
            table.add_row("启用", "[green]是[/]" if enabled else "[dim]否[/]")
            table.add_row("函数文件", self._config.functions_file)
            table.add_row("已注册函数", str(func_count))
            table.add_row("用户函数", str(len(user_funcs)))
            table.add_row(
                "代码生成",
                "[green]可用[/]" if gen_available else "[dim]不可用（需 AI）[/]",
            )
            table.add_row("沙箱检查", "是" if self._config.agent_code_sandbox else "否")
            console.print(table)
        else:
            print(f"Agent 自动化状态:")
            print(f"  启用: {'是' if enabled else '否'}")
            print(f"  函数文件: {self._config.functions_file}")
            print(f"  已注册函数: {func_count}")
            print(f"  代码生成: {'可用' if gen_available else '不可用（需 AI）'}")

    def _agent_on(self) -> None:
        """启用 Agent 通道"""
        if self._agent_enabled:
            print("Agent 已启用")
            return
        self._agent_enabled = True
        self._config.agent_enabled = True
        self._setup_agent()
        # 加载用户函数文件（内置 Draco 函数已在初始化时注册）
        if self._func_registry is not None:
            try:
                n_user = self._func_registry.load_user_file(self._config.functions_file_resolved)
                if n_user > 0:
                    log.info("加载 %d 个用户函数", n_user)
            except DracoError as e:
                log.warning("加载用户函数文件失败: %s", e)
        n = len(self._func_registry.list()) if self._func_registry else 0
        print(f"✅ Agent 已启用 ({n} 个函数)")
        if n == 0:
            print(f"  提示: 在 {self._config.functions_file_resolved} 定义函数，或 /agent templates 查看模板")

    def _agent_off(self) -> None:
        """禁用 Agent 通道"""
        if not self._agent_enabled:
            print("Agent 已是关闭状态")
            return
        self._teardown_agent()
        print("✅ Agent 已禁用")

    def _agent_list(self) -> None:
        """列出已注册函数"""
        if self._func_registry is None:
            self._render_error("Agent 未启用，先用 /agent on")
            return
        funcs = self._func_registry.list()
        if not funcs:
            print("（无函数，用 /agent templates 查看模板，或 /agent gen 生成）")
            return
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            table = Table(title="已注册函数", border_style="magenta")
            table.add_column("函数名", style="bold")
            table.add_column("async")
            table.add_column("参数")
            table.add_column("说明")
            for name, rf in sorted(funcs.items()):
                params = ", ".join(rf.param_names_no_ctx) or "-"
                doc = rf.doc.split("\n")[0][:40] if rf.doc else "-"
                table.add_row(name, "是" if rf.is_async else "否", params, doc)
            Console().print(table)
        else:
            print("已注册函数:")
            for name, rf in sorted(funcs.items()):
                params = ", ".join(rf.param_names_no_ctx) or "-"
                async_mark = "async " if rf.is_async else ""
                print(f"  {async_mark}{name}({params})  — {rf.doc.split(chr(10))[0][:40] if rf.doc else '-'}")

    def _agent_show(self, name: str) -> None:
        """查看函数源码"""
        if self._func_registry is None:
            self._render_error("Agent 未启用，先用 /agent on")
            return
        rf = self._func_registry.get(name)
        if rf is None:
            self._render_error(f"函数不存在: {name}")
            return
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.panel import Panel
            from rich.syntax import Syntax

            Console().print(
                Panel(
                    Syntax(rf.source, "python", theme="monokai"),
                    title=f"{name} (async={rf.is_async})",
                    border_style="magenta",
                )
            )
        else:
            print(f"--- {name} (async={rf.is_async}) ---")
            print(rf.source)

    def _agent_reload(self) -> None:
        """重新加载 functions.py"""
        if self._func_registry is None:
            self._render_error("Agent 未启用，先用 /agent on")
            return
        try:
            n = self._func_registry.reload()
            print(f"✅ 重新加载完成 ({n} 个函数)")
        except DracoError as e:
            self._render_draco_error(e)

    async def _agent_gen(self, intent: str) -> None:
        """AI 生成函数（代码即行动）"""
        if self._code_generator is None:
            self._render_error("Agent 未启用，先用 /agent on")
            return
        if not self._llm_client or not self._llm_client.is_available():
            self._render_error("AI 不可用，先用 /ai on 启用 AI")
            return

        if getattr(self, "_rich", False):
            from rich.console import Console
            Console().print(f"[magenta]意图:[/] {intent}")
            Console().print("[dim]生成函数中...[/]")
        else:
            print(f"意图: {intent}")
            print("生成函数中...")

        context_summary = ""
        if self._perception is not None:
            context_summary = self._perception.get_context_summary()

        gen: GeneratedFunction = await self._code_generator.generate(
            intent, context_summary=context_summary
        )

        if not gen.ok:
            self._render_error(f"代码生成失败: {gen.error}")
            return

        # 渲染生成的代码
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.panel import Panel
            from rich.syntax import Syntax

            Console().print(
                Panel(
                    Syntax(gen.code, "python", theme="monokai"),
                    title=f"AI 生成 — {gen.func_name}",
                    border_style="magenta",
                )
            )
        else:
            print(f"--- AI 生成 — {gen.func_name} ---")
            print(gen.code)
            print("---")

        # 询问是否执行
        ok = await self._confirmer.ask_yes("执行此函数？", default=False)
        if not ok:
            # 询问是否保存到 functions.py
            save = await self._confirmer.ask_yes("保存到 functions.py？", default=False)
            if save:
                self._save_function_to_file(gen.code, gen.func_name)
            return

        # 执行生成的函数
        await self._exec_generated(gen)

    async def _exec_generated(self, gen: GeneratedFunction) -> None:
        """执行 AI 生成的函数"""
        if self._code_runner is None or self._py_executor is None:
            self._render_error("Agent 通道未初始化")
            return
        try:
            func, func_name, _violations = self._code_runner.exec_function_code(
                gen.code, strict=self._config.agent_code_sandbox
            )
        except DracoError as e:
            self._render_draco_error(e)
            return

        if func is None or not callable(func):
            self._render_error("生成的代码未定义可调用函数")
            return

        # 构造临时 RegisteredFunction
        import inspect
        from .agent.function_registry import RegisteredFunction

        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            self._render_error("无法获取生成函数的签名")
            return

        rf = RegisteredFunction(
            name=func_name or gen.func_name or "generated",
            func=func,
            signature=sig,
            doc=inspect.getdoc(func) or "",
            source=gen.code,
            is_builtin=False,
            is_async=inspect.iscoroutinefunction(func),
        )

        # 执行（无参数）
        try:
            result: AgentExecResult = await self._py_executor.execute(
                rf, [], {}, cwd=os.getcwd(), session_id=self._session_id
            )
        except Exception as e:
            log.exception("generated function exec crashed")
            self._render_error(f"执行失败: {e}")
            return

        if result.stdout:
            sys.stdout.write(result.stdout)
            if not result.stdout.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

        if not result.success:
            if result.stderr:
                sys.stderr.write(result.stderr)
                sys.stderr.flush()
            self._render_error(f"函数执行失败: {result.error}")
            return

        if result.return_value is not None:
            import json
            try:
                s = json.dumps(result.return_value, ensure_ascii=False, default=str, indent=2)
                print(f"→ {s}")
            except (TypeError, ValueError):
                print(f"→ {result.return_value!r}")

        # 询问是否保存
        save = await self._confirmer.ask_yes("保存到 functions.py 以便复用？", default=False)
        if save:
            self._save_function_to_file(gen.code, gen.func_name)

    def _save_function_to_file(self, code: str, func_name: str) -> None:
        """把函数源码追加到 functions.py"""
        path = self._config.functions_file_resolved
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n\n# 由 /agent gen 生成\n")
                f.write(code)
                if not code.endswith("\n"):
                    f.write("\n")
            print(f"✅ 已保存到 {path}")
            # 重新加载
            if self._func_registry is not None:
                try:
                    self._func_registry.reload()
                    print(f"   已重新加载（{len(self._func_registry.list())} 个函数）")
                except DracoError as e:
                    self._render_error(f"重新加载失败: {e.message}")
        except OSError as e:
            self._render_error(f"保存失败: {e}")

    async def _agent_run(self, rest: str) -> None:
        """/agent run <name> [args] — 显式调用函数"""
        if self._func_registry is None:
            self._render_error("Agent 未启用，先用 /agent on")
            return
        parts = rest.split(maxsplit=1)
        name = parts[0]
        args_str = parts[1] if len(parts) > 1 else ""
        rf = self._func_registry.get(name)
        if rf is None:
            self._render_error(f"函数不存在: {name}")
            return
        await self._exec_agent(rest, rf, args_str)

    def _agent_templates(self) -> None:
        """列出可用模板"""
        templates = list_templates()
        if not templates:
            print("（无模板）")
            return
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            table = Table(title="函数模板", border_style="magenta")
            table.add_column("模板名", style="bold")
            table.add_column("说明")
            for t in templates:
                table.add_row(t.name, t.description)
            Console().print(table)
        else:
            print("函数模板:")
            for t in templates:
                print(f"  {t.name}  — {t.description}")

    def _agent_apply(self, name: str) -> None:
        """应用模板到 functions.py"""
        try:
            tpl = get_template(name)
        except KeyError:
            self._render_error(f"模板不存在: {name}（/agent templates 查看可用模板）")
            return
        self._save_function_to_file(tpl.code, tpl.name)

    def _handle_alias_cmd(self, arg: str) -> None:
        """处理 /alias 子命令"""
        arg = arg.strip()
        if not arg:
            self._list_aliases()
            return
        if arg.startswith("-d "):
            name = arg[3:].strip()
            if self._aliases.remove(name):
                print(f"已删除别名: {name}")
            else:
                self._render_error(f"别名不存在: {name}")
            return
        # /alias name expansion...
        parts = arg.split(maxsplit=1)
        if len(parts) == 1:
            # 查看
            exp = self._aliases.get(parts[0])
            if exp is not None:
                print(f"{parts[0]} = {exp}")
            else:
                self._render_error(f"别名不存在: {parts[0]}")
            return
        name, expansion = parts
        is_new = self._aliases.add(name, expansion)
        print(f"{'已添加' if is_new else '已更新'}别名: {name} = {expansion}")

    def _list_aliases(self) -> None:
        aliases = self._aliases.list()
        if not aliases:
            print("（无别名，用 /alias <name> <expansion> 添加）")
            return
        for name in sorted(aliases):
            print(f"  {name} = {aliases[name]}")

    def _show_history(self, arg: str) -> None:
        try:
            n = int(arg) if arg.strip() else 20
        except ValueError:
            n = 20
        try:
            recs = self._history.recent(limit=n, session_id=None)
        except DracoError as e:
            self._render_draco_error(e)
            return
        if not recs:
            print("（无历史记录）")
            return
        print(f"最近 {len(recs)} 条历史:")
        for r in reversed(recs):  # 时间正序显示
            status = "OK" if r.exit_code == 0 else (f"×{r.exit_code}" if r.exit_code is not None else "?")
            print(f"  [{status}] {r.raw_input}")

    def _handle_risk_cmd(self, arg: str) -> None:
        """处理 /risk 子命令

        /risk                  — 显示当前规则表
        /risk test <command>   — 模拟评估某命令的风险等级（不执行）
        """
        arg = arg.strip()
        if not arg:
            self._show_risk_rules()
            return
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        if sub == "test":
            if len(parts) < 2:
                self._render_error("用法: /risk test <command>")
                return
            self._preview_risk(parts[1])
            return
        self._render_error(f"未知子命令: {sub}（可用: test）")

    def _show_risk_rules(self) -> None:
        """显示当前生效的风险规则表"""
        from .security.risk_assessor import RiskLevel

        level_order = [RiskLevel.CRITICAL, RiskLevel.DANGER, RiskLevel.CAUTION]
        level_label = {
            RiskLevel.CRITICAL: "🚨 CRITICAL",
            RiskLevel.DANGER: "🔴 DANGER",
            RiskLevel.CAUTION: "⚠️  CAUTION",
        }
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(title="风险规则表", border_style="cyan")
            table.add_column("等级", style="bold")
            table.add_column("命令")
            table.add_column("args_contain")
            table.add_column("exact_args")
            table.add_column("说明")
            for level in level_order:
                for pat in self._assessor._patterns_by_level[level]:
                    table.add_row(
                        level_label[level],
                        pat.cmd,
                        ",".join(pat.args_contain) or "-",
                        ",".join(pat.exact_args) or "-",
                        pat.desc,
                    )
            console.print(table)
        else:
            print("风险规则表:")
            for level in level_order:
                pats = self._assessor._patterns_by_level[level]
                if not pats:
                    continue
                print(f"  [{level_label[level]}]")
                for pat in pats:
                    ac = ",".join(pat.args_contain) or "-"
                    ea = ",".join(pat.exact_args) or "-"
                    print(f"    {pat.cmd}  contain=[{ac}] exact=[{ea}]  {pat.desc}")

    def _preview_risk(self, command: str) -> None:
        """模拟评估某命令的风险等级（不执行）"""
        from .shell.parser import parse
        from .shell.normalizer import normalize

        try:
            ir = parse(command)
            normalize(ir)
            # 别名展开（与真实管线一致，确保评估准确）
            ir, _ = self._pipeline._expander.expand(ir)
        except DracoError as e:
            self._render_draco_error(e)
            return

        assessment = self._assessor.assess(ir)
        level = assessment.level
        label = {
            "safe": "✅ SAFE",
            "caution": "⚠️  CAUTION",
            "danger": "🔴 DANGER",
            "critical": "🚨 CRITICAL",
        }.get(level.value, level.value)

        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.panel import Panel

            console = Console()
            color = {
                "safe": "green",
                "caution": "yellow",
                "danger": "red",
                "critical": "bold red",
            }.get(level.value, "white")
            body = f"[{color}]{label}[/]\n命令: {command}\n"
            if assessment.reason:
                body += f"原因: {assessment.reason}\n"
            if assessment.matched_rules:
                body += "命中规则:\n"
                for m in assessment.matched_rules:
                    body += f"  - [{m.level.value}] {m.cmd}: {m.desc}\n"
            console.print(Panel(body.strip(), title="风险预览", border_style=color))
        else:
            print(f"风险预览: {label}")
            print(f"  命令: {command}")
            if assessment.reason:
                print(f"  原因: {assessment.reason}")
            for m in assessment.matched_rules:
                print(f"  命中: [{m.level.value}] {m.cmd}: {m.desc}")

    def _render_error(self, msg: str) -> None:
        if getattr(self, "_rich", False):
            from rich.console import Console
            Console().print(f"[bold red]错误:[/] {msg}")
        else:
            print(f"错误: {msg}", file=sys.stderr)

    def _render_draco_error(self, err: DracoError) -> None:
        if getattr(self, "_rich", False):
            from rich.console import Console
            from rich.panel import Panel

            Console().print(
                Panel(
                    f"[bold red]{err.code}[/]\n{err.message}",
                    title="命令失败",
                    border_style="red",
                )
            )
        else:
            print(f"[{err.code}] {err.message}", file=sys.stderr)

    def _render_block(self, result) -> None:
        msg = result.block_reason or "被钩子阻断"
        if getattr(self, "_rich", False):
            from rich.console import Console
            Console().print(f"[bold yellow]已阻断:[/] {msg}")
        else:
            print(f"已阻断: {msg}", file=sys.stderr)


def main() -> None:
    """入口函数（pyproject scripts 指向）

    支持参数:
      --setup-auth          设置/重置身份验证密码（用于 critical 级操作）
      --no-startup          跳过启动界面 (P5)
      --renderer <engine>   指定渲染引擎: textual | simple (P5)
      --theme <name>        指定主题 (P5)
      --template <name>     指定启动模板 (P5)

    P5: TTY 环境默认 textual，非 TTY 自动降级 simple (OpenDracoCLI prompt_toolkit 循环)。
    """
    args = sys.argv[1:]
    if "--setup-auth" in args:
        _setup_auth_interactive()
        return

    # P5: 解析 flags 并覆盖配置
    cfg = get_global_config()
    skip_startup = "--no-startup" in args
    for i, a in enumerate(args):
        if a == "--renderer" and i + 1 < len(args):
            cfg.tui_engine = args[i + 1]
        elif a == "--theme" and i + 1 < len(args):
            cfg.theme = args[i + 1]
        elif a == "--template" and i + 1 < len(args):
            cfg.startup_template = args[i + 1]

    # textual 引擎：TTY 且未强制 simple 时启用
    if cfg.effective_tui_engine == "textual":
        try:
            from .tui.app import run_textual

            run_textual(cfg, skip_startup=skip_startup)
            return
        except Exception:
            # textual 不可用或崩溃 → 降级到 simple (OpenDracoCLI prompt_toolkit 循环)
            log.exception("textual TUI 启动失败，降级到 simple 引擎")

    app = OpenDracoCLI()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


def _setup_auth_interactive() -> None:
    """交互式设置身份验证密码"""
    cfg = get_global_config()
    auth = Authenticator(
        hash_path=cfg.auth_hash_file_resolved,
        iterations=cfg.pbkdf2_iterations,
    )
    print("OpenDracoCLI — 设置身份验证密码")
    print(f"  哈希文件: {cfg.auth_hash_file_resolved}")
    print(f"  算法: PBKDF2-HMAC-SHA256 ({cfg.pbkdf2_iterations} 轮)")
    if auth.is_configured():
        print("  ⚠️  已存在密码，将覆盖。")
    print()

    try:
        pw1 = input("请输入新密码: ")
        if not pw1:
            print("密码不能为空，已取消。")
            return
        pw2 = input("请再次输入以确认: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return

    if pw1 != pw2:
        print("两次输入不一致，已取消。")
        return
    auth.set_password(pw1)
    print("✅ 密码已设置。critical 级操作将通过此密码验证身份。")


if __name__ == "__main__":
    main()
