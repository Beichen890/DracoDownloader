"""NativeDispatcher — 单命令的三层路由分发器

放弃外部 shell 后，每个 CommandNode 由本分发器决定执行后端：
  1. Rust 内核 (opendracocli._native) — 高频/IO 密集型命令，进程内调用零开销
  2. Draco 函数 (Python + 第三方库) — 复杂命令，走 Python 通道
  3. exec 兜底 — 无替代的程序直接 exec（不经 shell，跨平台安全）

未命中前两层的命令（如 git/python/vim）走 exec 兜底。
glob 通配符展开在此层完成（相对 cwd），跨平台一致。
"""

from __future__ import annotations

import asyncio
import glob
import os
import time
from typing import Any, List, Optional

from ..errors import ERR_EXEC_CANCELLED, ERR_EXEC_TIMEOUT, make_error
from ..logger import get_logger
from .executor import ExecResult, _quote_token
from .ir import CommandNode

log = get_logger("shell.dispatcher")


def expand_globs(args: List[str], cwd: str) -> List[str]:
    """对含通配符的非 flag 参数做 glob 展开（相对 cwd）

    规则：
      - 以 - 开头的 flag 不展开
      - 含 * ? [ 的参数用 glob.glob 展开（recursive=True 支持 **）
      - 展开为空则保留原参数（避免命令报"无匹配"）
      - 展开结果按字典序排序，转为相对 cwd 的路径（若原参数是相对的）

    跨平台一致，不依赖 shell 的 glob 行为。
    """
    result: List[str] = []
    for a in args:
        if a.startswith("-") or not any(c in a for c in "*?["):
            result.append(a)
            continue
        is_abs = os.path.isabs(a)
        pattern = a if is_abs else os.path.join(cwd, a)
        matches = sorted(glob.glob(pattern, recursive=True))
        if not matches:
            result.append(a)
            continue
        for m in matches:
            result.append(m if is_abs else os.path.relpath(m, cwd))
    return result


def _resolve(cwd: str, p: str) -> str:
    if os.path.isabs(p):
        return p
    return os.path.join(cwd, p)


class NativeDispatcher:
    """单命令三层路由分发器"""

    def __init__(
        self,
        config: Any = None,
        function_registry: Any = None,
        python_executor: Any = None,
    ) -> None:
        self._config = config
        self._func_reg = function_registry
        self._py_exec = python_executor
        # Rust 内核（可选：未构建则降级到 exec 兜底）
        self._native = None
        self._native_cmds: set[str] = set()
        try:
            from .. import _native  # type: ignore

            self._native = _native
            self._native_cmds = set(_native.supported_commands())
            log.debug("Rust 内核就绪：%d 个命令", len(self._native_cmds))
        except ImportError:
            log.warning("Rust 内核未构建，基础命令将走 exec 兜底")

    def set_function_registry(self, reg: Any) -> None:
        self._func_reg = reg

    def set_python_executor(self, ex: Any) -> None:
        self._py_exec = ex

    async def execute_node(
        self,
        node: CommandNode,
        cwd: str,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> ExecResult:
        """执行单个命令节点，按三层优先级路由

        Args:
            node: 已归一化的命令节点
            cwd: 当前工作目录（绝对路径）
            stdin: 上游管道输入（无则为 None）
            timeout: 超时秒数
            session_id: 会话 ID（Draco 函数通道用）

        Returns:
            ExecResult（含 new_cwd，cd 命令会设置）
        """
        # glob 展开（跨平台一致）
        args = expand_globs(node.args, cwd)
        mapped = node.name + (" " + " ".join(_quote_token(a) for a in args) if args else "")
        start = time.monotonic()

        # --- 1. Rust 内核 ---
        if self._native is not None and node.name in self._native_cmds:
            try:
                r = self._native.execute(node.name, args, cwd, stdin)
            except Exception as e:  # Rust panic 等不应发生，但防御
                return ExecResult(
                    exit_code=1,
                    stderr=f"draco_native: {node.name}: 内部错误: {e}",
                    mapped_command=mapped,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            return ExecResult(
                exit_code=r.exit_code,
                stdout=r.stdout,
                stderr=r.stderr,
                mapped_command=mapped,
                duration_ms=int((time.monotonic() - start) * 1000),
                new_cwd=r.new_cwd,
            )

        # --- 2. Draco 函数（Python + 第三方库）---
        if self._func_reg is not None:
            func = self._func_reg.match(node.name)
            if func is not None and self._py_exec is not None:
                return await self._run_draco_func(
                    func, args, cwd, mapped, start, timeout, session_id, stdin
                )

        # --- 3. exec 兜底（不经 shell，直接 exec 程序）---
        return await self._exec_fallback(node.name, args, cwd, stdin, timeout, mapped, start)

    async def _run_draco_func(
        self, func, args, cwd, mapped, start, timeout, session_id, stdin=None
    ) -> ExecResult:
        """走 Python 通道执行 Draco 函数"""
        from ..agent.arg_parser import ArgParseError, parse_call_args

        raw_args = " ".join(_quote_token(a) for a in args)
        try:
            pos_args, kwargs = parse_call_args(
                raw_args,
                func.param_names_no_ctx,
                func.param_types,
                func.defaults,
            )
        except ArgParseError as e:
            return ExecResult(
                exit_code=2,
                stderr=f"{func.name}: 参数错误: {e.message}",
                mapped_command=mapped,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        try:
            agent_result = await self._py_exec.execute(
                func, pos_args, kwargs,
                cwd=cwd, session_id=session_id, timeout=timeout, stdin=stdin,
            )
            return ExecResult(
                exit_code=agent_result.exit_code,
                stdout=agent_result.stdout,
                stderr=agent_result.stderr,
                mapped_command=mapped,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as e:
            return ExecResult(
                exit_code=1,
                stderr=f"{func.name}: {e}",
                mapped_command=mapped,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    async def _exec_fallback(
        self, name, args, cwd, stdin, timeout, mapped, start
    ) -> ExecResult:
        """exec 兜底：直接 exec 程序，不经 shell

        用于未覆盖的命令（git/python/vim 等）及交互式程序。
        """
        to = timeout if timeout is not None else (
            self._config.exec_timeout if self._config else 30.0
        )
        stdin_bytes = stdin.encode("utf-8") if stdin else None
        try:
            proc = await asyncio.create_subprocess_exec(
                name,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                cwd=cwd,
            )
        except FileNotFoundError:
            return ExecResult(
                exit_code=127,
                stderr=f"{name}: 命令未找到",
                mapped_command=mapped,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes), timeout=to
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise make_error(
                ERR_EXEC_TIMEOUT,
                detail={"command": mapped, "timeout": to},
                command=mapped,
                timeout=to,
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise make_error(
                ERR_EXEC_CANCELLED,
                detail={"command": mapped},
                command=mapped,
            )

        return ExecResult(
            exit_code=proc.returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            mapped_command=mapped,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
