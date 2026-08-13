"""AI 纠错器 — 命令失败时调用 LLM 给出纠正建议

复用 DracoHub 设计:
  - agent/core.py:_call_api 三元组降级
  - prompt.py XML 分隔抗注入
  - character.py 角色化语气

流程:
  命令失败 → 构造上下文 → LLM 分析 → 提取建议命令 → 用户确认（不自动执行）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..logger import get_logger
from .character import CommandRole
from .client import LLMClient, LLMResponse
from .prompt import (
    build_correction_prompt,
    build_correction_user_message,
    extract_suggested_command,
    strip_analysis,
)

log = get_logger("ai.corrector")


@dataclass
class CorrectionResult:
    """纠错结果"""

    ok: bool = False
    analysis: str = ""           # AI 分析的失败原因
    suggested_command: str = ""  # 建议的纠正命令（可能为空）
    raw: str = ""                # AI 原始输出
    error: str = ""              # 失败原因（ok=False 时）


class Corrector:
    """AI 纠错器

    命令失败时调用 LLM 分析 stderr + exit_code，给出纠正建议。
    AI 不可用时静默返回（不抛异常，复用 _call_api 降级策略）。
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        platform: str = "",
        native_shell: str = "",
    ) -> None:
        self._client = client
        self._platform = platform
        self._native_shell = native_shell

    async def correct(
        self,
        raw_input: str,
        exit_code: int,
        stderr: str,
        role: CommandRole,
        emotion_hint: str = "",
    ) -> CorrectionResult:
        """分析失败命令并给出纠正建议

        Args:
            raw_input: 用户原始命令
            exit_code: 退出码（非 0）
            stderr: 错误输出
            role: 命令角色卡
            emotion_hint: 情感提示

        Returns:
            CorrectionResult（AI 不可用时 ok=False, error=...）
        """
        if not self._client.is_available():
            return CorrectionResult(ok=False, error="AI 不可用")

        sys_prompt = build_correction_prompt(
            role,
            emotion_hint,
            platform=self._platform,
            native_shell=self._native_shell,
        )
        user_msg = build_correction_user_message(raw_input, exit_code, stderr)

        resp: LLMResponse = await self._client.chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ]
        )

        if not resp.ok:
            log.warning("纠错 LLM 调用失败: %s", resp.error)
            return CorrectionResult(ok=False, error=resp.error)

        suggested = extract_suggested_command(resp.content)
        analysis = strip_analysis(resp.content)

        return CorrectionResult(
            ok=True,
            analysis=analysis,
            suggested_command=suggested or "",
            raw=resp.content,
        )
