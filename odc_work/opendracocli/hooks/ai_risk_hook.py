"""AIRiskHook — AI 增强风控 PreExec 钩子

priority=20，在 P2 RiskAssessmentHook (priority=10) 之后执行。
职责:
  - P2 规则评估为 safe 且 AI 启用且 ai_risk_enabled=True 时，调 RiskAdvisor
  - 把 AI 评估结果写入 ctx.metadata["ai_risk"]
  - AI 评估为 DANGER 时升级 risk_level，触发后续 Confirmer 流程

只升级不降级：
  - P2 已判 critical/danger → 跳过 AI（已高危）
  - P2 已判 caution → 跳过 AI（已需确认）
  - P2 判 safe 但 AI 判 DANGER → 升级为 danger
"""

from __future__ import annotations

from typing import Optional

from ..ai.risk_advisor import AIRiskAssessment, AIRiskLevel, RiskAdvisor
from ..config import DracoConfig, get_global_config
from ..events import EVT_AI_RISK_ASSESS, Event, get_global_bus
from ..logger import get_logger
from ..shell.ir import CommandIR
from ..shell.executor import serialize_ir
from .base import BlockResult, ExecContext, ExecDecision, PreExecHook

log = get_logger("hooks.ai_risk_hook")


class AIRiskHook(PreExecHook):
    """AI 增强风控钩子

    在 P2 RiskAssessmentHook 之后执行（priority=20）。
    只对 P2 判 safe 的命令做 AI 评估，只升级不降级。
    """

    priority = 20

    def __init__(
        self,
        advisor: RiskAdvisor,
        *,
        config: Optional[DracoConfig] = None,
    ) -> None:
        self._advisor = advisor
        self._config = config or get_global_config()

    async def pre_exec(
        self, ctx: ExecContext
    ) -> tuple[ExecDecision, Optional[BlockResult]]:
        """AI 增强风控主流程"""
        # 1. P2 已判高危，跳过 AI（不降级）
        risk_level = ctx.metadata.get("risk_level", "safe")
        if risk_level in ("caution", "danger", "critical"):
            return ExecDecision.CONTINUE, None

        # 2. AI 风控未启用
        if not getattr(self._config, "ai_risk_enabled", False):
            return ExecDecision.CONTINUE, None

        # 3. 序列化 IR 为命令字符串（含参数）
        command_str = _serialize_for_risk(ctx.ir)
        if not command_str:
            return ExecDecision.CONTINUE, None

        # 4. 调 RiskAdvisor
        try:
            assessment: AIRiskAssessment = await self._advisor.assess(command_str)
        except Exception:
            log.exception("AI 风险评估异常")
            return ExecDecision.CONTINUE, None

        # 5. 写入元数据（即使 SAFE 也记录，便于 /ai context 查看）
        ctx.metadata["ai_risk"] = {
            "level": assessment.level.value,
            "reason": assessment.reason,
            "suspicious_pattern": assessment.suspicious_pattern,
            "skipped": assessment.skipped,
            "raw_llm_output": assessment.raw_llm_output,
        }

        # 6. 发布事件
        get_global_bus().publish(
            Event(
                type=EVT_AI_RISK_ASSESS,
                payload={
                    "raw_input": ctx.raw_input,
                    "level": assessment.level.value,
                    "reason": assessment.reason,
                    "suspicious_pattern": assessment.suspicious_pattern,
                    "skipped": assessment.skipped,
                },
            )
        )

        # 7. AI 评估为 DANGER → 升级 risk_level，触发后续沙箱/确认流程
        #    注意：本钩子不直接 BLOCK，而是让 ctx.metadata 标记，
        #    由 CLI 层在 exec 后感知；或后续 RiskAssessmentHook 复评。
        #    此处采用「升级 risk_level + 标记 use_sandbox」的轻量方案。
        if assessment.level == AIRiskLevel.DANGER:
            ctx.metadata["risk_level"] = "danger"
            ctx.metadata["use_sandbox"] = True
            ctx.metadata["risk_reason"] = (
                f"AI 增强风控升级: {assessment.reason}"
            )
            log.info(
                "AI 风控升级 risk → danger: %s (pattern=%s)",
                ctx.raw_input[:80],
                assessment.suspicious_pattern,
            )
        elif assessment.level == AIRiskLevel.CAUTION:
            # CAUTION 不升级 risk_level（保持 safe），但记录原因
            log.info(
                "AI 风控提示 caution: %s (pattern=%s)",
                ctx.raw_input[:80],
                assessment.suspicious_pattern,
            )

        return ExecDecision.CONTINUE, None


def _serialize_for_risk(ir: CommandIR) -> str:
    """把 IR 序列化为命令字符串供 AI 评估

    复用 SubprocessExecutor.serialize_ir，确保 LLM 看到与实际执行一致的命令。
    """
    try:
        return serialize_ir(ir)
    except Exception:
        return ir.raw_input
