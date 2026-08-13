"""PythonChannelExecutor — 独立的 Python 函数执行通道

实现 Executor 协议变体：不像 SubprocessExecutor 那样吃 IR，而是直接调用 Python 函数。
共享 HistoryStore 与事件总线，保证可观测性与 P1-P3 一致。

执行流程:
  1. 构造 AgentContext（注入 shell_runner = ShellPipeline.run）
  2. 调用 func(ctx, **bound_kwargs)
  3. 同步函数直接调，异步函数 await
  4. 捕获返回值 + stdout + 异常
  5. 序列化为 AgentExecResult
  6. 写历史 + 发事件
"""

from __future__ import annotations

import asyncio
import inspect
import io
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..config import DracoConfig
from ..errors import ERR_AGENT_EXEC_FAILED, make_error
from ..events import EVT_AGENT_EXECUTED, EVT_FUNCTION_CALLED, Event, get_global_bus
from ..history.store import HistoryStore
from ..logger import get_logger
from .context import AgentContext
from .function_registry import RegisteredFunction

log = get_logger("agent.python_executor")


@dataclass
class AgentExecResult:
    """Python 通道执行结果"""

    success: bool
    exit_code: Optional[int]       # 0 成功 / 1 异常 / None 被取消
    stdout: str = ""
    stderr: str = ""
    return_value: Any = None
    duration_ms: int = 0
    func_name: str = ""
    mapped_command: str = ""       # "func_name(arg=1)" 供历史记录
    cancelled: bool = False
    error: Optional[str] = None


class PythonChannelExecutor:
    """Python 函数执行通道"""

    def __init__(
        self,
        config: DracoConfig,
        history_store: Optional[HistoryStore] = None,
        shell_runner: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """
        Args:
            config: DracoConfig
            history_store: 共享历史存储（None 则不写历史）
            shell_runner: async callable (cmd: str) -> PipelineResult
                          用于 ctx.shell()，通常绑定 ShellPipeline.run
        """
        self._config = config
        self._history = history_store
        self._shell_runner = shell_runner

    def set_shell_runner(self, runner: Callable[[str], Any]) -> None:
        """注入 shell runner（CLI 接线时调用）"""
        self._shell_runner = runner

    async def execute(
        self,
        reg_func: RegisteredFunction,
        args: List[Any],
        kwargs: Dict[str, Any],
        *,
        cwd: Optional[str] = None,
        session_id: str = "",
        timeout: Optional[float] = None,
        stdin: Optional[str] = None,
    ) -> AgentExecResult:
        """执行一个已注册的函数

        Args:
            reg_func: RegisteredFunction
            args: 位置参数（通常为空，参数已绑定到 kwargs）
            kwargs: 关键字参数
            cwd: 工作目录
            session_id: 会话 ID
            timeout: 超时秒数
            stdin: 上游管道传入的 stdin（无上游为 None）

        Returns:
            AgentExecResult
        """
        func = reg_func.func
        func_name = reg_func.name
        # 构造历史记录用的命令字符串
        arg_str = self._format_call(func_name, kwargs)
        result = AgentExecResult(
            success=False,
            exit_code=None,
            func_name=func_name,
            mapped_command=arg_str,
        )

        # 发布 FunctionCalled 事件
        get_global_bus().publish(
            Event(
                type=EVT_FUNCTION_CALLED,
                payload={
                    "func_name": func_name,
                    "args": list(args),
                    "kwargs": dict(kwargs),
                    "session_id": session_id,
                },
            )
        )

        # 构造 AgentContext
        ctx = AgentContext(
            shell_runner=self._shell_runner or self._noop_shell_runner,
            cwd=cwd,
            config=self._config,
            log_sink=lambda msg: log.info("[func:%s] %s", func_name, msg),
            session_id=session_id,
            stdin=stdin,
        )

        # 捕获 stdout
        old_stdout = sys.stdout
        captured = io.StringIO()

        class _Tee:
            """同时写 captured 和原 stdout，让函数内 print 用户可见"""

            def write(self, s):
                captured.write(s)
                old_stdout.write(s)

            def flush(self):
                captured.flush()
                old_stdout.flush()

        sys.stdout = _Tee()

        start_perf = time.perf_counter()
        started_at = time.time()
        to = timeout if timeout is not None else self._config.exec_timeout

        try:
            if reg_func.is_async:
                coro = func(ctx, *args, **kwargs)
                ret = await asyncio.wait_for(coro, timeout=to)
            else:
                # 同步函数在 executor 线程跑，避免阻塞事件循环
                loop = asyncio.get_event_loop()
                ret = await loop.run_in_executor(
                    None, lambda: func(ctx, *args, **kwargs)
                )

            result.success = True
            result.exit_code = 0
            result.return_value = ret
            # 函数可通过 ctx.exit_code 显式设置退出码（对齐 GNU grep 等约定）
            ctx_exit = getattr(ctx, "exit_code", None)
            if ctx_exit is not None:
                result.exit_code = ctx_exit

        except asyncio.TimeoutError:
            result.exit_code = None
            result.cancelled = True
            result.error = f"函数执行超时（{to}s）"
            result.stderr = result.error
            log.warning("function %s timeout", func_name)

        except asyncio.CancelledError:
            result.exit_code = None
            result.cancelled = True
            result.error = "函数执行被取消"
            result.stderr = result.error
            raise

        except Exception as e:
            result.exit_code = 1
            result.error = f"{type(e).__name__}: {e}"
            result.stderr = traceback.format_exc()
            log.exception("function %s failed", func_name)

        finally:
            sys.stdout = old_stdout
            result.stdout = captured.getvalue()
            result.duration_ms = int((time.perf_counter() - start_perf) * 1000)

        # 写历史
        self._record_history(result, started_at, session_id, cwd or "")

        # 发事件
        get_global_bus().publish(
            Event(
                type=EVT_AGENT_EXECUTED,
                payload={
                    "func_name": func_name,
                    "success": result.success,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "mapped_command": result.mapped_command,
                    "return_value": self._safe_repr(result.return_value),
                    "session_id": session_id,
                },
            )
        )

        return result

    @staticmethod
    def _format_call(func_name: str, kwargs: Dict[str, Any]) -> str:
        """格式化调用字符串供历史记录"""
        if not kwargs:
            return f"{func_name}()"
        parts = [f"{k}={v!r}" for k, v in kwargs.items()]
        return f"{func_name}({', '.join(parts)})"

    @staticmethod
    def _safe_repr(value: Any) -> str:
        """安全的返回值 repr（限制长度）"""
        try:
            s = repr(value)
            if len(s) > 500:
                return s[:500] + "...(truncated)"
            return s
        except Exception:
            return "<unreprable>"

    @staticmethod
    async def _noop_shell_runner(cmd: str) -> Any:
        """未注入 shell runner 时的占位（返回失败）"""
        from ..shell.pipeline import PipelineResult

        return PipelineResult(
            success=False,
            raw_input=cmd,
            stderr="Agent shell runner 未注入",
        )

    def _record_history(
        self,
        result: AgentExecResult,
        started_at: float,
        session_id: str,
        cwd: str,
    ) -> None:
        """写入共享历史（失败不抛异常）"""
        if self._history is None:
            return
        try:
            self._history.record(
                raw_input=result.mapped_command,
                canonical_ir="",  # Python 通道无 IR
                mapped_command=result.mapped_command,
                platform=self._config.current_platform,
                cwd=cwd,
                exit_code=result.exit_code,
                started_at=started_at,
                duration_ms=result.duration_ms,
                stdout=result.stdout,
                stderr=result.stderr,
                is_high_risk=False,
                alias_used=None,
                session_id=session_id,
            )
        except Exception as e:
            log.error("failed to record agent history: %s", e)
