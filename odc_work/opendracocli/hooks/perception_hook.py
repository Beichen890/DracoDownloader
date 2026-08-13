"""PerceptionHook — 感知与自互动 PostExec 钩子

priority=20，在 CorrectionHook 之后执行。
职责:
  - 每条命令后调用 PerceptionEngine.record 更新上下文 + 计算情感
  - 检测主动建议（连续失败/高频命令/重复长命令）→ 注入 ctx.metadata["suggestions"]
  - 发布 EVT_AI_PERCEPTION 和 EVT_AI_EMOTION_UPDATE 事件
"""

from __future__ import annotations

from typing import Optional

from ..ai.emotion import EmotionResult
from ..ai.perception import PerceptionEngine
from ..config import DracoConfig, get_global_config
from ..events import (
    EVT_AI_EMOTION_UPDATE,
    EVT_AI_PERCEPTION,
    Event,
    get_global_bus,
)
from ..logger import get_logger
from .base import ExecContext, PostExecHook

log = get_logger("hooks.perception_hook")


class PerceptionHook(PostExecHook):
    """感知与自互动钩子

    PostExecHook：只读，不能改 ExecResult。
    通过 ctx.metadata["suggestions"] 把主动建议交给 CLI 渲染。
    """

    priority = 20  # 在 CorrectionHook 之后

    def __init__(
        self,
        perception: PerceptionEngine,
        *,
        config: Optional[DracoConfig] = None,
    ) -> None:
        self._perception = perception
        self._config = config or get_global_config()

    async def post_exec(self, ctx: ExecContext, result) -> None:
        """更新感知上下文 + 计算情感 + 主动建议"""
        # 解析失败 / 阻断的命令 result 可能为 None 或无 exit_code；按失败处理
        exit_code = getattr(result, "exit_code", None)
        stderr = getattr(result, "stderr", "") or ""
        success = exit_code == 0

        # 高危标记（由 RiskAssessmentHook 写入）
        risk_level = ctx.metadata.get("risk_level", "safe")
        is_high_risk = risk_level in ("danger", "critical")

        first_token = ctx.ir.nodes[0].name if ctx.ir.nodes else ""

        try:
            emotion: EmotionResult = self._perception.record(
                raw_input=ctx.raw_input,
                success=success,
                exit_code=exit_code,
                is_high_risk=is_high_risk,
                first_token=first_token,
                stderr=stderr,
            )
        except Exception:
            log.exception("感知记录失败")
            return

        # 持久化（失败不抛异常）
        try:
            self._perception.save()
        except Exception:
            log.exception("感知状态保存失败")

        # 发布情感更新事件
        get_global_bus().publish(
            Event(
                type=EVT_AI_EMOTION_UPDATE,
                payload={
                    "dominant": emotion.dominant,
                    "intensity": emotion.intensity,
                    "hint": emotion.hint,
                    "vector": emotion.vector,
                    "raw_input": ctx.raw_input,
                },
            )
        )

        # 主动建议
        try:
            suggestions = self._perception.get_suggestions()
        except Exception:
            suggestions = []

        if suggestions:
            ctx.metadata["suggestions"] = suggestions
            get_global_bus().publish(
                Event(
                    type=EVT_AI_PERCEPTION,
                    payload={
                        "raw_input": ctx.raw_input,
                        "suggestions": suggestions,
                        "failure_streak": self._perception.state.failure_streak,
                        "emotion": emotion.dominant,
                    },
                )
            )
