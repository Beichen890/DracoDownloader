"""Pipeline — 编排整条执行管线

TaskStep 风格：parse → normalize → alias_expand → draco_func_route → risk(pre) → exec → post → history

简单命令直接交给原生 shell（bash/cmd），不做命令名映射。
复杂/常用任务由 Draco 函数用 Python 库直接实现，走 Python 通道。

每步可观测，单步失败抛 DracoError，由上层捕获并发布 CommandFailed 事件。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..aliases.manager import AliasManager
from ..config import DracoConfig, get_global_config
from ..errors import DracoError
from ..events import EVT_COMMAND_EXECUTED, EVT_COMMAND_FAILED, Event, get_global_bus
from ..hooks.base import ExecContext, ExecDecision
from ..hooks.registry import HookRegistry, get_global_registry
from ..history.store import HistoryStore, get_global_store
from ..logger import get_logger
from .alias_expander import AliasExpander
from .compose import ComposeEngine
from .dispatcher import NativeDispatcher
from .executor import ExecResult, SubprocessExecutor, serialize_ir
from .normalizer import normalize
from .parser import parse

log = get_logger("shell.pipeline")


@dataclass
class PipelineResult:
    """管线执行结果（聚合 ExecResult + 元数据）"""

    success: bool
    raw_input: str
    mapped_command: str = ""
    canonical_ir: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    alias_used: Optional[str] = None
    is_high_risk: bool = False
    error: Optional[DracoError] = None
    blocked: bool = False
    block_reason: Optional[str] = None
    new_cwd: Optional[str] = None


class ShellPipeline:
    """编排整条执行管线"""

    def __init__(
        self,
        config: DracoConfig | None = None,
        alias_manager: Optional[AliasManager] = None,
        history_store: Optional[HistoryStore] = None,
        hook_registry: Optional[HookRegistry] = None,
        executor: Optional[SubprocessExecutor] = None,
        function_registry: Optional[Any] = None,
        python_executor: Optional[Any] = None,
        sandbox_executor: Optional[Any] = None,
    ) -> None:
        self._config = config or get_global_config()
        self._aliases = alias_manager
        self._history = history_store
        self._hooks = hook_registry or get_global_registry()
        self._executor = executor or SubprocessExecutor(config=self._config)
        self._expander = AliasExpander(
            manager=self._aliases, config=self._config
        )
        # Draco 函数路由：命中注册函数走 Python 通道，否则原生 shell
        self._function_registry = function_registry
        self._python_executor = python_executor
        # P2: 沙箱执行器（可选，由 CLI 在启用风控时注入）
        self._sandbox_executor = sandbox_executor
        # 原生内核 + 组合引擎（替代外部 shell）
        # 三层路由：Rust 内核 → Draco 函数 → exec 兜底
        # 组合语义（| > && || ;）由 ComposeEngine 自行编排，不依赖 shell
        self._dispatcher = NativeDispatcher(
            config=self._config,
            function_registry=function_registry,
            python_executor=python_executor,
        )
        self._compose = ComposeEngine(self._dispatcher)

    def set_sandbox_executor(self, sx: Any) -> None:
        """注入沙箱执行器（P2 风控启用时调用）"""
        self._sandbox_executor = sx

    def set_alias_manager(self, m: AliasManager) -> None:
        self._aliases = m
        self._expander.set_manager(m)

    def set_function_registry(self, registry: Any) -> None:
        """注入 Draco 函数注册表"""
        self._function_registry = registry
        self._dispatcher.set_function_registry(registry)

    def set_python_executor(self, executor: Any) -> None:
        """注入 Python 通道执行器"""
        self._python_executor = executor
        self._dispatcher.set_python_executor(executor)

    async def run(
        self,
        raw_input: str,
        *,
        cwd: Optional[str] = None,
        session_id: str = "",
        timeout: Optional[float] = None,
    ) -> PipelineResult:
        """执行一次完整管线

        流程:
          1. parse         — 解析为 IR
          2. normalize     — 归一化
          3. alias_expand  — 别名展开
          4. draco_func_route — 命中 Draco 函数则走 Python 通道，否则原生 shell
          5. pre_exec      — PreExec 钩子链（可阻断/改写）
          6. execute       — subprocess / Python 通道执行
          7. post_exec     — PostExec 钩子链（只读）
          8. history       — 写 SQLite
          9. event         — 发布事件
        """
        import os

        started_at = time.time()
        start_perf = time.perf_counter()
        effective_cwd = cwd or os.getcwd()
        result = PipelineResult(success=False, raw_input=raw_input)

        # --- 1. parse ---
        try:
            ir = parse(raw_input)
        except DracoError as e:
            return self._fail(result, e, started_at, session_id, effective_cwd)

        # --- 2. normalize ---
        normalize(ir)

        # --- 3. alias expand ---
        try:
            ir, alias_used = self._expander.expand(ir)
        except DracoError as e:
            return self._fail(result, e, started_at, session_id, effective_cwd)
        result.alias_used = alias_used

        # --- 4. canonical IR 序列化（存历史用）---
        # 路由决策下沉到 NativeDispatcher（三层：Rust 内核 → Draco 函数 → exec 兜底）
        result.canonical_ir = ir.to_json()

        # --- 5. pre_exec hooks ---
        ctx = ExecContext(
            ir=ir,
            raw_input=raw_input,
            alias_used=alias_used,
            cwd=effective_cwd,
            env=dict(os.environ),
        )
        try:
            decision, block = await self._hooks.run_pre(ctx)
        except Exception as e:
            log.exception("pre-exec hooks crashed")
            return self._fail(
                result,
                DracoError(code="draco.hook.crash", message=str(e)),
                started_at,
                session_id,
                effective_cwd,
            )

        if decision == ExecDecision.BLOCK:
            result.blocked = True
            result.block_reason = block.reason if block else "blocked"
            result.success = False
            result.duration_ms = int((time.perf_counter() - start_perf) * 1000)
            # 阻断也记录历史（exit_code=None）
            self._record(result, started_at, session_id, effective_cwd, stdout="", stderr="")
            get_global_bus().publish(
                Event(
                    type=EVT_COMMAND_FAILED,
                    payload={
                        "raw_input": raw_input,
                        "reason": result.block_reason,
                        "blocked": True,
                    },
                )
            )
            return result

        # 钩子可能改写了 ctx.ir，重新序列化
        result.canonical_ir = ctx.ir.to_json()

        # P2: 根据风控钩子标记选择 executor
        use_sandbox = ctx.metadata.get("use_sandbox", False)
        risk_level = ctx.metadata.get("risk_level", "safe")
        result.is_high_risk = risk_level in ("danger", "critical")

        executor = self._executor
        if use_sandbox and self._sandbox_executor is not None:
            executor = self._sandbox_executor
            log.info("using sandbox executor for risk_level=%s", risk_level)

        # --- 6. execute ---
        # 沙箱模式：执行前检查写路径是否在白名单内（违规直接拒绝，不执行）
        if use_sandbox and self._sandbox_executor is not None:
            violation = self._sandbox_executor.check(ctx.ir, effective_cwd)
            if violation is not None:
                log.warning(violation)
                # 沙箱拦截：管线正常处理（success=True），命令未执行（exit_code=-1）
                # blocked 仅表示风控钩子阻断，沙箱拦截不算 blocked
                result.success = True
                result.exit_code = -1
                result.stderr = violation
                result.mapped_command = serialize_ir(ctx.ir)
                result.duration_ms = int((time.perf_counter() - start_perf) * 1000)
                self._record(
                    result, started_at, session_id, effective_cwd, stdout="", stderr=violation
                )
                get_global_bus().publish(
                    Event(
                        type=EVT_COMMAND_EXECUTED,
                        payload={
                            "raw_input": raw_input,
                            "mapped_command": result.mapped_command,
                            "exit_code": -1,
                            "session_id": session_id,
                            "sandbox_blocked": True,
                        },
                    )
                )
                return result

        # 统一走 ComposeEngine：组合语义（| > && || ;）+ 三层路由（Rust→Python→exec）
        # 危险命令已被 pre_exec 钩子阻断；cd 等命令的 new_cwd 透传给上层
        try:
            exec_result: ExecResult = await self._compose.execute(
                ctx.ir, cwd=effective_cwd, timeout=timeout, session_id=session_id,
            )
        except DracoError as e:
            # timeout / cancelled
            result.mapped_command = ""
            return self._fail(result, e, started_at, session_id, effective_cwd)

        result.success = exec_result.exit_code is not None
        result.mapped_command = exec_result.mapped_command
        result.exit_code = exec_result.exit_code
        result.stdout = exec_result.stdout
        result.stderr = exec_result.stderr
        result.duration_ms = exec_result.duration_ms
        # cd 等命令返回新 cwd，透传给 CLI 应用到 session
        if exec_result.new_cwd:
            result.new_cwd = exec_result.new_cwd

        # --- 7. post_exec hooks (只读) ---
        try:
            await self._hooks.run_post(ctx, exec_result)
        except Exception:
            log.exception("post-exec hooks crashed")

        # --- 8. history ---
        self._record(result, started_at, session_id, effective_cwd)

        # --- 9. event ---
        get_global_bus().publish(
            Event(
                type=EVT_COMMAND_EXECUTED,
                payload={
                    "raw_input": raw_input,
                    "mapped_command": result.mapped_command,
                    "exit_code": result.exit_code,
                    "alias_used": result.alias_used,
                    "duration_ms": result.duration_ms,
                    "session_id": session_id,
                },
            )
        )

        return result

    def _fail(
        self,
        result: PipelineResult,
        err: DracoError,
        started_at: float,
        session_id: str,
        cwd: str,
    ) -> PipelineResult:
        """管线失败统一处理：记录历史 + 发事件"""
        result.success = False
        result.error = err
        result.exit_code = None
        result.duration_ms = int((time.perf_counter() - started_at) * 1000)
        self._record(result, started_at, session_id, cwd, stdout="", stderr=err.message)
        get_global_bus().publish(
            Event(
                type=EVT_COMMAND_FAILED,
                payload={
                    "raw_input": result.raw_input,
                    "error_code": err.code,
                    "message": err.message,
                },
            )
        )
        return result

    def _record(
        self,
        result: PipelineResult,
        started_at: float,
        session_id: str,
        cwd: str,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """写入历史（失败时不抛异常，避免历史故障拖垮主流程）"""
        if self._history is None:
            return
        try:
            self._history.record(
                raw_input=result.raw_input,
                canonical_ir=result.canonical_ir,
                mapped_command=result.mapped_command,
                platform=self._config.current_platform,
                cwd=cwd,
                exit_code=result.exit_code,
                started_at=started_at,
                duration_ms=result.duration_ms,
                stdout=stdout if stdout else result.stdout,
                stderr=stderr if stderr else result.stderr,
                is_high_risk=result.is_high_risk,
                alias_used=result.alias_used,
                session_id=session_id,
            )
        except DracoError as e:
            log.error("failed to record history: %s", e)
