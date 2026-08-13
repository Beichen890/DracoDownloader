"""SubprocessExecutor — 调原生 shell 执行命令

策略：
  - 把映射+展开后的 IR 重新序列化为命令字符串
  - 调原生 shell（Win: cmd /c, Unix: bash -c）执行整条命令
  - 组合语义（&& | > 等）透传 shell
  - 捕获 stdout/stderr/exit_code

设计：定义 Executor 协议，SubprocessExecutor 是其实现。
P4 的 PythonChannelExecutor 将实现同一协议走独立通道。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..config import DracoConfig, get_global_config
from ..errors import ERR_EXEC_CANCELLED, ERR_EXEC_TIMEOUT, make_error
from ..logger import get_logger
from .ir import OP_AND, OP_NONE, OP_OR, OP_PIPE, OP_SEQ, CommandIR

log = get_logger("shell.executor")


@dataclass
class ExecResult:
    """执行结果

    Attributes:
        exit_code: 进程退出码（None 表示未执行/被取消）
        stdout: 完整 stdout
        stderr: 完整 stderr
        mapped_command: 最终交给 shell 的命令字符串
        duration_ms: 耗时（毫秒）
        cancelled: 是否被取消
        new_cwd: cd 等命令返回的新工作目录（绝对路径）；None 表示不变
    """

    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    mapped_command: str = ""
    duration_ms: int = 0
    cancelled: bool = False
    new_cwd: Optional[str] = None


class Executor(Protocol):
    """执行器协议 — P4 PythonChannelExecutor 也将实现此协议"""

    async def execute(
        self,
        ir: CommandIR,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ExecResult:
        ...


def serialize_ir(ir: CommandIR) -> str:
    """把 IR 重新序列化为命令字符串

    节点间用操作符连接，重定向附加在末尾。
    """
    if not ir.nodes:
        return ""

    parts = []
    for i, node in enumerate(ir.nodes):
        # 节点：name + args
        node_str = _quote_token(node.name)
        for arg in node.args:
            node_str += " " + _quote_token(arg)
        parts.append(node_str)
        # 操作符
        if i < len(ir.operators):
            parts.append(ir.operators[i])

    # 重定向（透传 shell）
    for r in ir.redirects:
        if r.kind == "stdout":
            parts.append(f"> {r.target}")
        elif r.kind == "append":
            parts.append(f">> {r.target}")
        elif r.kind == "stdin":
            parts.append(f"< {r.target}")
        elif r.kind == "stderr":
            parts.append(f"2> {r.target}")

    return " ".join(parts)


def _quote_token(tok: str) -> str:
    """对含空格/特殊字符的 token 加引号"""
    if not tok:
        return "''"
    if any(c in tok for c in " \t") and not (tok.startswith("'") or tok.startswith('"')):
        # 简单处理：含空格且未加引号则用双引号包裹
        # 转义内部双引号
        escaped = tok.replace('"', '\\"')
        return f'"{escaped}"'
    return tok


class SubprocessExecutor:
    """subprocess 执行器 — 调原生 shell"""

    def __init__(self, config: DracoConfig | None = None) -> None:
        self._config = config or get_global_config()

    async def execute(
        self,
        ir: CommandIR,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ExecResult:
        """执行 IR

        Args:
            ir: 映射+展开后的 IR
            cwd: 工作目录（None 用当前进程 cwd）
            timeout: 超时秒数（None 用 config.exec_timeout）

        Returns:
            ExecResult

        Raises:
            DracoError(ERR_EXEC_TIMEOUT): 超时
            DracoError(ERR_EXEC_CANCELLED): 用户取消
        """
        command_str = serialize_ir(ir)
        if not command_str:
            return ExecResult(
                exit_code=0,
                mapped_command="",
                duration_ms=0,
            )

        shell_exe, shell_args = self._config.native_shell
        full_args = shell_args + [command_str]
        to = timeout if timeout is not None else self._config.exec_timeout

        log.debug("exec: %s %s", shell_exe, command_str)
        start = asyncio.get_event_loop().time()

        try:
            proc = await asyncio.create_subprocess_exec(
                shell_exe,
                *full_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as e:
            # shell 不存在
            return ExecResult(
                exit_code=-1,
                stderr=f"shell not found: {shell_exe}: {e}",
                mapped_command=command_str,
                duration_ms=int((asyncio.get_event_loop().time() - start) * 1000),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=to)
        except asyncio.TimeoutError:
            await _safe_terminate(proc)
            raise make_error(
                ERR_EXEC_TIMEOUT,
                detail={"command": command_str, "timeout": to},
                command=command_str,
                timeout=to,
            )
        except asyncio.CancelledError:
            # 用户 Ctrl+C：先 ensure 不对已退出进程重复发信号，避免
            # "child process pid … exit status already read" 警告噪音
            await _safe_terminate(proc)
            raise make_error(
                ERR_EXEC_CANCELLED,
                detail={"command": command_str},
                command=command_str,
            )

        duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)

        return ExecResult(
            exit_code=proc.returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            mapped_command=command_str,
            duration_ms=duration_ms,
        )


async def _safe_terminate(proc: asyncio.subprocess.Process) -> None:
    """安全终止子进程，吞掉重复读 exit status 的告警。

    Ctrl+C / 超时时调用。若进程已退出（returncode 已知），不再发 kill 信号，
    避免触发 "child process pid … exit status already read" 警告噪音。
    """
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            # 进程刚好在我们检查后退出
            pass
    try:
        await proc.wait()
    except (ProcessLookupError, asyncio.CancelledError):
        pass
