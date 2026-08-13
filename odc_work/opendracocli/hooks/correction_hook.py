"""CorrectionHook — AI 纠错 PostExec 钩子

priority=10，命令失败时触发 LLM 纠错。
流程（复用 DracoHub agent/core.py 的降级策略）:
  1. 命令成功 → 跳过
  2. AI 不可用 → 静默跳过
  3. 同命令冷却期内 → 跳过
  4. 调 Corrector.correct → 渲染建议 → 用户确认
  5. 用户确认应用建议命令 → 注入 ctx.metadata["pending_input"] 由 CLI 处理
  6. 发布 EVT_AI_CORRECTION 事件

纠错建议的命令**不自动执行**，必须用户确认；应用时仍走完整 P1 管线 + P2 风控。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..ai.character import CommandRole, RoleRegistry
from ..ai.client import LLMClient
from ..ai.corrector import CorrectionResult, Corrector
from ..ai.perception import PerceptionEngine
from ..config import DracoConfig, get_global_config
from ..events import EVT_AI_CORRECTION, Event, get_global_bus
from ..logger import get_logger
from ..security.confirmer import Confirmer
from .base import ExecContext, PostExecHook

log = get_logger("hooks.correction_hook")


@dataclass
class _CooldownEntry:
    """冷却表条目"""

    last_ts: float = 0.0
    count: int = 0


class CorrectionHook(PostExecHook):
    """AI 纠错钩子

    只读 PostExecHook：不能改 ExecResult，但可通过 ctx.metadata["pending_input"]
    把建议命令交给 CLI 下一轮处理（不绕过风控）。
    """

    priority = 10  # 最先执行（在 PerceptionHook 之前）

    def __init__(
        self,
        client: LLMClient,
        corrector: Corrector,
        role_registry: RoleRegistry,
        perception: PerceptionEngine,
        confirmer: Confirmer,
        *,
        config: Optional[DracoConfig] = None,
        cooldown_seconds: float = 5.0,
        max_stderr_chars: int = 1000,
    ) -> None:
        self._client = client
        self._corrector = corrector
        self._roles = role_registry
        self._perception = perception
        self._confirmer = confirmer
        self._config = config or get_global_config()
        self._cooldown_seconds = cooldown_seconds
        self._max_stderr = max_stderr_chars
        # 同命令冷却表：raw_input → _CooldownEntry
        self._cooldowns: Dict[str, _CooldownEntry] = {}
        # 上次纠错结果（供 CLI 读取并应用建议命令）
        self._last_result: Optional[CorrectionResult] = None
        self._last_raw_input: str = ""

    async def post_exec(self, ctx: ExecContext, result) -> None:
        """纠错主流程"""
        # 1. 成功不纠错
        if result.exit_code == 0 or result.exit_code is None:
            return

        # 2. AI 不可用静默跳过
        if not self._client.is_available():
            return

        # 3. 阻断/解析失败的命令不纠错（无 stderr 可分析）
        if not result.stderr and not result.mapped_command:
            return

        # 4. 冷却期检查（防抖）
        if self._in_cooldown(ctx.raw_input):
            log.debug("纠错冷却中，跳过: %s", ctx.raw_input[:60])
            return

        # 5. 角色匹配 + 情感提示
        first_token = ctx.ir.nodes[0].name if ctx.ir.nodes else ""
        role: CommandRole = self._roles.match(first_token)
        emotion_hint = self._perception.get_emotion_hint()

        # 6. 调用 Corrector
        stderr_trim = (result.stderr or "")[: self._max_stderr]
        try:
            corr: CorrectionResult = await self._corrector.correct(
                raw_input=ctx.raw_input,
                exit_code=result.exit_code,
                stderr=stderr_trim,
                role=role,
                emotion_hint=emotion_hint,
            )
        except Exception:
            log.exception("纠错调用异常")
            return

        self._last_result = corr
        self._last_raw_input = ctx.raw_input
        self._mark_cooldown(ctx.raw_input)

        # 7. 发布事件
        get_global_bus().publish(
            Event(
                type=EVT_AI_CORRECTION,
                payload={
                    "raw_input": ctx.raw_input,
                    "ok": corr.ok,
                    "analysis": corr.analysis,
                    "suggested_command": corr.suggested_command,
                    "error": corr.error,
                    "role": role.name,
                },
            )
        )

        if not corr.ok:
            log.info("AI 纠错失败: %s", corr.error)
            return

        # 8. 渲染建议 + 用户确认（由 CLI 注入的回调处理）
        #    这里只把结果存入 ctx.metadata，由 CLI 的 _render_ai_correction 决定如何呈现
        ctx.metadata["ai_correction"] = {
            "role_name": role.name,
            "analysis": corr.analysis,
            "suggested_command": corr.suggested_command,
            "raw_input": ctx.raw_input,
        }

    def _in_cooldown(self, raw_input: str) -> bool:
        """检查同命令是否在冷却期内"""
        entry = self._cooldowns.get(raw_input)
        if entry is None:
            return False
        if time.time() - entry.last_ts >= self._cooldown_seconds:
            return False
        return True

    def _mark_cooldown(self, raw_input: str) -> None:
        """标记命令进入冷却"""
        entry = self._cooldowns.get(raw_input)
        if entry is None:
            entry = _CooldownEntry()
            self._cooldowns[raw_input] = entry
        entry.last_ts = time.time()
        entry.count += 1
        # 冷却表防膨胀：超过 256 条清理最旧的
        if len(self._cooldowns) > 256:
            oldest_key = min(self._cooldowns.keys(), key=lambda k: self._cooldowns[k].last_ts)
            self._cooldowns.pop(oldest_key, None)

    def pop_pending_correction(self) -> Optional[Tuple[str, str]]:
        """供 CLI 调用：取走上次的纠错建议

        Returns:
            (raw_input, suggested_command) 或 None
        """
        if self._last_result is None or not self._last_result.suggested_command:
            return None
        cmd = self._last_result.suggested_command
        raw = self._last_raw_input
        self._last_result = None
        return raw, cmd
