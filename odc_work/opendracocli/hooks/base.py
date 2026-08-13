"""钩子基类 — PreExecHook / PostExecHook

P1 只定义接口，不注册业务钩子。
P2 注册 RiskAssessmentHook(PreExecHook) 做风控拦截。
P3 注册 AIRewriteHook(PreExecHook) 改写命令、ErrorCorrectHook(PostExecHook) 纠错。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from ..shell.ir import CommandIR


class ExecDecision(Enum):
    """PreExec 钩子的决策"""

    CONTINUE = "continue"  # 放行
    BLOCK = "block"        # 阻断执行


@dataclass
class ExecContext:
    """执行上下文 — 钩子可读取/修改

    Attributes:
        ir: 当前 IR（PreExec 钩子可改写，用于 P3 AI 角色化）
        raw_input: 用户原始输入
        alias_used: 命中的别名名（None 表示无别名）
        cwd: 工作目录
        env: 环境变量快照
        metadata: 钩子间传递的元数据
    """

    ir: CommandIR
    raw_input: str
    alias_used: Optional[str] = None
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BlockResult:
    """阻断结果"""

    reason: str
    code: str = "draco.hook.blocked"
    detail: Dict[str, Any] = field(default_factory=dict)


class PreExecHook(ABC):
    """执行前钩子 — 可放行或阻断

    priority 数字越小越先执行。任一钩子返回 BLOCK 则停止后续钩子并阻断执行。
    """

    priority: int = 100

    @abstractmethod
    async def pre_exec(self, ctx: ExecContext) -> tuple[ExecDecision, Optional[BlockResult]]:
        """返回 (CONTINUE, None) 或 (BLOCK, BlockResult)"""
        ...


class PostExecHook(ABC):
    """执行后钩子 — 只读，不能改结果

    用于纠错建议、统计等。priority 数字越小越先执行。
    """

    priority: int = 100

    @abstractmethod
    async def post_exec(self, ctx: ExecContext, result: Any) -> None:
        """result 是 ExecResult"""
        ...
