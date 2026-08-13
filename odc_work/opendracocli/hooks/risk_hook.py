"""RiskAssessmentHook — 风控 PreExec 钩子

priority=10，最先执行。编排:
  assess → confirm → auth → sandbox 标记

流程:
  safe:           CONTINUE
  caution:        confirmer.ask_yes() → CONTINUE / BLOCK
  danger:         confirmer.ask_yes() → CONTINUE (标记沙箱) / BLOCK
  critical:       authenticator.verify() + confirmer.ask_yes()
                  → CONTINUE (标记沙箱) / BLOCK
"""

from __future__ import annotations

from typing import Optional

from ..config import DracoConfig, get_global_config
from ..errors import ERR_AUTH_FAILED, ERR_AUTH_NOT_CONFIGURED, ERR_RISK_DENIED
from ..events import (
    EVT_AUTH_REQUIRED,
    EVT_RISK_BLOCKED,
    Event,
    get_global_bus,
)
from ..logger import get_logger
from ..security.authenticator import Authenticator
from ..security.confirmer import Confirmer
from ..security.risk_assessor import RiskAssessment, RiskAssessor, RiskLevel
from .base import BlockResult, ExecContext, ExecDecision, PreExecHook

log = get_logger("hooks.risk_hook")


class RiskAssessmentHook(PreExecHook):
    """风控评估钩子"""

    priority = 10  # 最先执行

    def __init__(
        self,
        assessor: RiskAssessor,
        confirmer: Confirmer,
        authenticator: Authenticator,
        config: Optional[DracoConfig] = None,
    ) -> None:
        self._assessor = assessor
        self._confirmer = confirmer
        self._authenticator = authenticator
        self._config = config or get_global_config()

    async def pre_exec(
        self, ctx: ExecContext
    ) -> tuple[ExecDecision, Optional[BlockResult]]:
        """风控评估主流程"""
        assessment = self._assessor.assess(ctx.ir)
        ctx.metadata["risk_level"] = assessment.level.value
        ctx.metadata["risk_reason"] = assessment.reason

        if assessment.level == RiskLevel.SAFE:
            return ExecDecision.CONTINUE, None

        log.info(
            "risk assessment: level=%s reason=%s raw=%r",
            assessment.level.value,
            assessment.reason,
            ctx.raw_input[:80],
        )

        # 构造确认提示
        prompt = self._build_prompt(assessment, ctx.raw_input)

        if assessment.level == RiskLevel.CAUTION:
            ok = await self._confirmer.ask_yes(prompt, default=False)
            if not ok:
                self._publish_block(ctx, assessment, "用户拒绝")
                return ExecDecision.BLOCK, BlockResult(
                    reason=f"用户拒绝: {assessment.reason}",
                    code=ERR_RISK_DENIED,
                )
            return ExecDecision.CONTINUE, None

        if assessment.level == RiskLevel.DANGER:
            ok = await self._confirmer.ask_yes(prompt, default=False)
            if not ok:
                self._publish_block(ctx, assessment, "用户拒绝")
                return ExecDecision.BLOCK, BlockResult(
                    reason=f"用户拒绝: {assessment.reason}",
                    code=ERR_RISK_DENIED,
                )
            ctx.metadata["use_sandbox"] = True
            return ExecDecision.CONTINUE, None

        # CRITICAL
        # 根据配置决定是否需要身份验证
        if self._config.require_auth_for == "critical":
            if not self._authenticator.is_configured():
                self._publish_block(ctx, assessment, "未配置密码")
                return ExecDecision.BLOCK, BlockResult(
                    reason="需先设置密码 (opendracocli --setup-auth)",
                    code=ERR_AUTH_NOT_CONFIGURED,
                )
            get_global_bus().publish(
                Event(
                    type=EVT_AUTH_REQUIRED,
                    payload={
                        "raw_input": ctx.raw_input,
                        "reason": assessment.reason,
                    },
                )
            )
            authed = await self._authenticator.verify()
            if not authed:
                self._publish_block(ctx, assessment, "身份验证失败")
                return ExecDecision.BLOCK, BlockResult(
                    reason="身份验证失败",
                    code=ERR_AUTH_FAILED,
                )

        ok = await self._confirmer.ask_yes(prompt, default=False)
        if not ok:
            self._publish_block(ctx, assessment, "用户拒绝")
            return ExecDecision.BLOCK, BlockResult(
                reason=f"用户拒绝: {assessment.reason}",
                code=ERR_RISK_DENIED,
            )
        ctx.metadata["use_sandbox"] = True
        return ExecDecision.CONTINUE, None

    @staticmethod
    def _build_prompt(assessment: RiskAssessment, raw_input: str) -> str:
        """构造确认提示文本"""
        level_str = {
            RiskLevel.CAUTION: "⚠️  注意",
            RiskLevel.DANGER: "🔴 危险",
            RiskLevel.CRITICAL: "🚨 极高危",
        }.get(assessment.level, assessment.level.value)
        return (
            f"{level_str}: 即将执行 [{raw_input}]\n"
            f"  原因: {assessment.reason}\n"
            f"  确认执行？"
        )

    @staticmethod
    def _publish_block(
        ctx: ExecContext,
        assessment: RiskAssessment,
        reason: str,
    ) -> None:
        """发布拦截事件"""
        get_global_bus().publish(
            Event(
                type=EVT_RISK_BLOCKED,
                payload={
                    "raw_input": ctx.raw_input,
                    "level": assessment.level.value,
                    "reason": reason,
                    "rules": [
                        {
                            "cmd": m.cmd,
                            "level": m.level.value,
                            "desc": m.desc,
                        }
                        for m in assessment.matched_rules
                    ],
                },
            )
        )
