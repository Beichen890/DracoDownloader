"""SandboxExecutor — 路径白名单沙箱

实现 Executor 协议，内部包装 SubprocessExecutor。
执行前检查 IR 中所有重定向目标 + 显式路径参数是否在可写白名单内。
违规 → 直接返回失败 ExecResult，不调 subprocess。

P2 简化版：只做路径白名单拦截（不真正 unshare/chroot）。
真正系统级沙箱留 P2.1。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from ..logger import get_logger
from ..shell.executor import ExecResult, SubprocessExecutor, serialize_ir
from ..shell.ir import CommandIR

log = get_logger("security.sandbox")


class SandboxExecutor:
    """路径白名单沙箱执行器"""

    def __init__(
        self,
        inner: SubprocessExecutor,
        writable_paths: List[Path],
    ) -> None:
        self._inner = inner
        self._writable = [p.expanduser().resolve() for p in writable_paths]
        log.debug("sandbox writable paths: %s", self._writable)

    async def execute(
        self,
        ir: CommandIR,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ExecResult:
        """执行前检查路径，通过则交给 inner executor"""
        # 提取所有可能被写入的路径
        write_paths = self._extract_write_paths(ir, cwd)

        for p in write_paths:
            if not self._is_writable(p):
                msg = (
                    f"draco.sandbox.violation: 写入路径 {p} 不在白名单内 "
                    f"(白名单: {self._writable})"
                )
                log.warning(msg)
                return ExecResult(
                    exit_code=-1,
                    stderr=msg,
                    mapped_command=serialize_ir(ir),
                    duration_ms=0,
                )

        # 路径检查通过，交给 inner
        return await self._inner.execute(ir, cwd=cwd, timeout=timeout)

    def _extract_write_paths(
        self, ir: CommandIR, cwd: Optional[str]
    ) -> List[Path]:
        """从 IR 提取所有可能被写入的路径

        来源:
          1. 重定向目标 (>, >>, 2>)
          2. 部分命令的显式路径参数（rm/rmdir/del 的路径参数）
        """
        paths: List[Path] = []
        base = Path(cwd) if cwd else Path.cwd()

        # 1. 重定向目标
        for r in ir.redirects:
            if r.kind in ("stdout", "append", "stderr"):
                paths.append(self._resolve_path(r.target, base))

        # 2. 危险命令的路径参数
        write_cmds = {"rm", "rmdir", "del", "rd", "mv", "move", "cp", "copy", "truncate", "chmod", "chown", "dd"}
        for node in ir.nodes:
            if node.name.lower() in write_cmds:
                for arg in node.args:
                    # 跳过选项参数（- 开头，如 -rf / -f）
                    if arg.startswith("-"):
                        continue
                    # 跳过 Windows 选项（/ 开头且短，如 /s /y /f）
                    # 注意: Unix 绝对路径也以 / 开头，但路径通常较长或含多级
                    if arg.startswith("/") and len(arg) <= 3:
                        continue
                    # 跳过明显非路径的纯数字（如 PID 1234）
                    if arg.isdigit():
                        continue
                    # 其余视为路径候选，检查
                    paths.append(self._resolve_path(arg, base))

        return paths

    @staticmethod
    def _resolve_path(p: str, base: Path) -> Path:
        """解析路径为绝对路径"""
        expanded = Path(os.path.expanduser(os.path.expandvars(p)))
        if expanded.is_absolute():
            return expanded.resolve()
        return (base / expanded).resolve()

    def _is_writable(self, path: Path) -> bool:
        """检查路径是否在白名单内

        路径在任一白名单目录下（含自身）即视为可写。
        """
        try:
            resolved = path.resolve()
        except OSError:
            return False

        for allowed in self._writable:
            try:
                # allowed 是否是 resolved 的祖先（或相等）
                resolved.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False
