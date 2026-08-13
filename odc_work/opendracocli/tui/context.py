"""渲染层数据模型 (P5)

RenderContext 是「内核 → 渲染层」的唯一数据载体。内核层 (pipeline/hooks/events)
产出事件与 PipelineResult，渲染层据此构造 RenderContext，再交给 Renderer 实现
(textual 或 SimpleRenderer) 渲染。

设计原则：
- 纯数据，不依赖 textual / rich，任何 Renderer 实现都能消费。
- 字段覆盖 P1-P4 全部输出场景：shell 输出、P2 风控阻断、P3 AI 纠错建议、P4 Agent 进度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# === RenderContext.kind 取值 ===
KIND_OUTPUT = "output"             # 正常命令输出 (stdout/stderr)
KIND_ERROR = "error"               # 内核 DracoError (解析/映射/别名失败)
KIND_BLOCK = "block"               # P2 风控阻断
KIND_HISTORY = "history"           # 历史列表渲染
KIND_STATUS = "status"             # 状态栏更新
KIND_AGENT_PROGRESS = "agent_progress"  # P4 Agent 任务进度
KIND_SUGGESTION = "suggestion"     # P3 感知主动建议 / AI 纠错建议
KIND_INFO = "info"                 # 普通信息 (如启动横幅、提示)


@dataclass
class AgentTaskInfo:
    """P4 Agent 通道执行进度信息 (RenderContext.agent_task)"""

    func_name: str = ""
    success: bool = False
    return_value: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    is_async: bool = False


@dataclass
class RenderContext:
    """一次渲染所需的全部数据

    内核 → 渲染层的唯一数据载体。不同 kind 用到的字段不同，未用字段保持默认值。
    """

    kind: str = KIND_OUTPUT
    raw_input: str = ""                 # 用户原始输入
    mapped_command: Optional[str] = None  # 映射后命令 (供 status 显示)
    exit_code: Optional[int] = None
    stdout: str = ""                    # stdout (已截断或全文, 由调用方决定)
    stderr: str = ""                    # stderr
    duration_ms: int = 0
    is_high_risk: bool = False          # 来自 P2 风控标记
    blocked: bool = False               # 是否被钩子阻断
    block_reason: str = ""              # 阻断原因
    error_code: str = ""                # DracoError.code (KIND_ERROR 时)
    error_message: str = ""             # DracoError.message
    suggestions: list[str] = field(default_factory=list)  # P3 AI 建议/纠错
    suggested_command: str = ""         # P3 AI 纠错建议命令
    agent_task: Optional[AgentTaskInfo] = None  # P4 Agent 进度
    timestamp: float = 0.0
    session_id: str = ""
    cwd: str = ""                       # 执行时工作目录 (status 显示)
    platform: str = ""                  # 当前平台
    theme_name: str = ""                # 当前主题名 (KIND_STATUS)
    ai_enabled: bool = False            # AI 层状态 (status)
    agent_enabled: bool = False         # Agent 层状态 (status)


def context_from_pipeline_result(result, *, session_id: str = "", cwd: str = "") -> RenderContext:
    """从 ShellPipeline.PipelineResult 构造 RenderContext

    这是渲染层消费 shell 通道结果的标准入口。
    """
    if result.blocked:
        return RenderContext(
            kind=KIND_BLOCK,
            raw_input=result.raw_input,
            mapped_command=result.mapped_command or None,
            duration_ms=result.duration_ms,
            blocked=True,
            block_reason=result.block_reason or "被钩子阻断",
            is_high_risk=result.is_high_risk,
            session_id=session_id,
            cwd=cwd,
        )
    if result.error is not None:
        return RenderContext(
            kind=KIND_ERROR,
            raw_input=result.raw_input,
            mapped_command=result.mapped_command or None,
            duration_ms=result.duration_ms,
            error_code=result.error.code,
            error_message=result.error.message,
            session_id=session_id,
            cwd=cwd,
        )
    return RenderContext(
        kind=KIND_OUTPUT,
        raw_input=result.raw_input,
        mapped_command=result.mapped_command or None,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        is_high_risk=result.is_high_risk,
        session_id=session_id,
        cwd=cwd,
    )


def context_from_event(event) -> RenderContext:
    """从 EventBus.Event 构造 RenderContext (用于订阅事件直接渲染的场景)

    支持 CommandExecuted / CommandFailed / RiskBlocked / AICorrection /
    AgentExecuted 等事件类型。
    """
    p = event.payload or {}
    et = event.type
    if et == "CommandExecuted":
        return RenderContext(
            kind=KIND_OUTPUT,
            raw_input=p.get("raw_input", ""),
            mapped_command=p.get("mapped_command"),
            exit_code=p.get("exit_code"),
            duration_ms=p.get("duration_ms", 0),
            session_id=p.get("session_id", ""),
        )
    if et == "CommandFailed":
        return RenderContext(
            kind=KIND_ERROR,
            raw_input=p.get("raw_input", ""),
            error_code=p.get("error_code", ""),
            error_message=p.get("message", ""),
        )
    if et == "RiskBlocked":
        return RenderContext(
            kind=KIND_BLOCK,
            raw_input=p.get("raw_input", ""),
            block_reason=p.get("reason", "被风控阻断"),
            blocked=True,
        )
    if et == "AICorrection":
        return RenderContext(
            kind=KIND_SUGGESTION,
            raw_input=p.get("raw_input", ""),
            suggested_command=p.get("suggested_command", ""),
            suggestions=[p.get("analysis", "")] if p.get("analysis") else [],
        )
    if et == "AgentExecuted":
        return RenderContext(
            kind=KIND_AGENT_PROGRESS,
            raw_input=p.get("func_name", ""),
            agent_task=AgentTaskInfo(
                func_name=p.get("func_name", ""),
                success=p.get("success", False),
                error=p.get("error", ""),
            ),
        )
    # 兜底：当作普通信息
    return RenderContext(kind=KIND_INFO, raw_input=et)
