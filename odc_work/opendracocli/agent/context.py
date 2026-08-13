"""AgentContext — 用户函数的执行上下文

参考 DracoHub CodingAgent 的 ctx 模式：用户函数签名约定 def name(ctx, ...) -> Any。
ctx 提供：
  - ctx.shell(cmd): 调 shell 通道（复用 P1 管线 + P2 风控 + P3 AI），返回 dict
  - ctx.log(msg):   记录日志（显示给用户）
  - ctx.print(msg): 打印到 stdout（函数内可见输出）
  - ctx.env:        环境变量快照
  - ctx.cwd:        工作目录
  - ctx.config:     DracoConfig 引用（只读访问）

设计：ctx.shell 内部调用 ShellPipeline.run()，因此 P2 RiskAssessmentHook + P3
AIRiskHook 全部生效，不绕过安全。函数内调 `rm -rf` 仍会触发风控确认。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from ..config import DracoConfig


class AgentContext:
    """用户函数的执行上下文

    Attributes:
        env: 环境变量快照（函数启动时拷贝，修改不影响外部进程）
        cwd: 工作目录
        config: DracoConfig 引用（只读访问配置）
    """

    def __init__(
        self,
        *,
        shell_runner: Callable[[str], Any],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        config: Optional[DracoConfig] = None,
        log_sink: Optional[Callable[[str], None]] = None,
        session_id: str = "",
        stdin: Optional[str] = None,
    ) -> None:
        # shell_runner: async callable (cmd: str) -> PipelineResult
        self._shell_runner = shell_runner
        self._cwd = cwd or os.getcwd()
        self._env: Dict[str, str] = dict(env if env is not None else os.environ)
        self._config = config
        self._log_sink = log_sink
        self._session_id = session_id
        self._stdin = stdin
        # 函数可显式设置的退出码（None=默认 0）
        # 用于 fgrep 等"命令式"函数对齐 GNU 退出码约定（0=有匹配，1=无匹配）
        self._exit_code: Optional[int] = None

    @property
    def env(self) -> Dict[str, str]:
        """环境变量快照（可读可写，但修改不影响外部进程）"""
        return self._env

    @property
    def cwd(self) -> str:
        """工作目录"""
        return self._cwd

    @property
    def config(self) -> Optional[DracoConfig]:
        """DracoConfig 引用（只读访问）"""
        return self._config

    @property
    def session_id(self) -> str:
        """当前会话 ID"""
        return self._session_id

    @property
    def stdin(self) -> Optional[str]:
        """上游管道传入的 stdin（无上游则为 None）"""
        return self._stdin

    @property
    def exit_code(self) -> Optional[int]:
        """函数显式设置的退出码（None=未设置，默认 0）"""
        return self._exit_code

    @exit_code.setter
    def exit_code(self, value: Optional[int]) -> None:
        """设置退出码（用于 fgrep 等"命令式"函数对齐 GNU 退出码）"""
        self._exit_code = value

    async def shell(self, cmd: str) -> Dict[str, Any]:
        """调 shell 通道（复用 P1 管线 + P2 风控 + P3 AI）

        Args:
            cmd: shell 命令字符串

        Returns:
            dict: {
                "success": bool,
                "exit_code": Optional[int],
                "stdout": str,
                "stderr": str,
                "mapped_command": str,
                "blocked": bool,
                "block_reason": str,
            }
        """
        result = await self._shell_runner(cmd)
        # PipelineResult → dict（用户函数消费友好）
        return {
            "success": getattr(result, "success", False),
            "exit_code": getattr(result, "exit_code", None),
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
            "mapped_command": getattr(result, "mapped_command", ""),
            "blocked": getattr(result, "blocked", False),
            "block_reason": getattr(result, "block_reason", "") or "",
        }

    def log(self, msg: str) -> None:
        """记录日志（显示给用户，区别于 print 的 stdout）

        用于函数内部的进度提示，通常渲染为 dim 样式。
        """
        if self._log_sink is not None:
            try:
                self._log_sink(str(msg))
            except Exception:
                pass

    def print(self, msg: str) -> None:
        """打印到 stdout（函数内可见输出）

        与 log 的区别：print 走 stdout，是函数的"正式输出"；
        log 走日志通道，是进度提示。
        """
        import sys

        s = str(msg)
        sys.stdout.write(s)
        if not s.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
