"""感知与自互动 — 用户操作上下文记忆 + 主动建议

复用 DracoHub 设计:
  - sub_agent/hermes/context_memory.py: JSON 持久化用户习惯
  - emotion/engine.py: 情感跨轮延续
  - 原子写入（写 .tmp → os.replace，参考 fact_memory._save）

记忆内容:
  - session: 命令计数、连续失败数、最近情感向量
  - long_term: 命令频次、常用路径、常见错误、最近5条命令
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..logger import get_logger
from .emotion import EmotionEngine, EmotionResult

log = get_logger("ai.perception")

_MAX_LAST_COMMANDS = 5
_MAX_FREQ_ENTRIES = 20


@dataclass
class PerceptionState:
    """感知状态（持久化到 JSON）"""

    session_started_at: float = 0.0
    command_count: int = 0
    failure_streak: int = 0
    last_emotion_vector: List[float] = field(default_factory=list)
    frequent_commands: Dict[str, int] = field(default_factory=dict)
    common_errors: Dict[str, int] = field(default_factory=dict)
    last_commands: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session": {
                "started_at": self.session_started_at,
                "command_count": self.command_count,
                "failure_streak": self.failure_streak,
                "last_emotion_vector": self.last_emotion_vector,
            },
            "long_term": {
                "frequent_commands": self.frequent_commands,
                "common_errors": self.common_errors,
                "last_commands": self.last_commands,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PerceptionState":
        sess = data.get("session", {}) or {}
        lt = data.get("long_term", {}) or {}
        return cls(
            session_started_at=sess.get("started_at", 0.0),
            command_count=sess.get("command_count", 0),
            failure_streak=sess.get("failure_streak", 0),
            last_emotion_vector=sess.get("last_emotion_vector", []),
            frequent_commands=lt.get("frequent_commands", {}),
            common_errors=lt.get("common_errors", {}),
            last_commands=lt.get("last_commands", []),
        )

    @classmethod
    def fresh(cls) -> "PerceptionState":
        return cls(session_started_at=time.time())


class PerceptionEngine:
    """感知引擎 — 记录上下文 + 推断情感 + 主动建议

    复用 context_memory 的 JSON 持久化 + EmotionEngine 的情感延续。
    """

    def __init__(
        self,
        context_file: Path,
        emotion_engine: EmotionEngine,
    ) -> None:
        self._file = context_file
        self._emotion = emotion_engine
        self._state = self._load()

    def _load(self) -> PerceptionState:
        """加载状态（原子读取，失败返回 fresh）"""
        if not self._file.exists():
            return PerceptionState.fresh()
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            return PerceptionState.from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.warning("加载感知状态失败: %s，重置", e)
            return PerceptionState.fresh()

    def save(self) -> None:
        """原子写入（复用 fact_memory._save 模式：.tmp → fsync → replace）"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            content = json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._file)
        except OSError as e:
            log.warning("保存感知状态失败: %s", e)

    def record(
        self,
        raw_input: str,
        *,
        success: bool,
        exit_code: Optional[int],
        is_high_risk: bool,
        first_token: str,
        stderr: str = "",
    ) -> EmotionResult:
        """记录一条命令执行结果，更新状态 + 计算情感

        Returns:
            本轮情感结果
        """
        self._state.command_count += 1

        # 失败连击
        if success:
            self._state.failure_streak = 0
        else:
            self._state.failure_streak += 1

        # 命令频次（top _MAX_FREQ_ENTRIES）
        token = first_token.lower()
        if token:
            self._state.frequent_commands[token] = (
                self._state.frequent_commands.get(token, 0) + 1
            )
            if len(self._state.frequent_commands) > _MAX_FREQ_ENTRIES:
                # 保留 top N
                sorted_items = sorted(
                    self._state.frequent_commands.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:_MAX_FREQ_ENTRIES]
                self._state.frequent_commands = dict(sorted_items)

        # 常见错误
        if not success and stderr:
            err_key = stderr.strip().split("\n")[0][:80] if stderr.strip() else f"exit {exit_code}"
            self._state.common_errors[err_key] = (
                self._state.common_errors.get(err_key, 0) + 1
            )

        # 最近命令（滑动窗口）
        self._state.last_commands.append(raw_input)
        if len(self._state.last_commands) > _MAX_LAST_COMMANDS:
            self._state.last_commands = self._state.last_commands[-_MAX_LAST_COMMANDS:]

        # 情感计算
        is_frequent = (
            self._state.frequent_commands.get(token, 0) >= 10
        )
        is_first_turn = self._state.command_count == 1
        result = self._emotion.compute(
            success=success,
            failure_streak=self._state.failure_streak,
            is_high_risk=is_high_risk,
            is_frequent=is_frequent,
            is_first_turn=is_first_turn,
        )
        self._state.last_emotion_vector = result.vector
        return result

    def get_emotion_hint(self) -> str:
        """获取当前情感提示（注入 prompt）"""
        # 用上一轮向量重算一次 hint（不改变状态）
        if not self._state.last_emotion_vector:
            return ""
        from .emotion import EMOTION_LABELS, _normalize

        vec = _normalize(self._state.last_emotion_vector)
        if not vec or max(vec) < 1e-9:
            return ""
        max_idx = max(range(len(vec)), key=lambda i: vec[i])
        return f"【角色情绪基调】{EMOTION_LABELS[max_idx]}({vec[max_idx]:.2f})"

    def get_context_summary(self) -> str:
        """获取上下文摘要（注入 /ai ask）"""
        parts: List[str] = []
        if self._state.last_commands:
            parts.append("最近命令：")
            for c in self._state.last_commands[-3:]:
                parts.append(f"  - {c}")
        if self._state.frequent_commands:
            top3 = sorted(
                self._state.frequent_commands.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:3]
            parts.append("常用命令：" + ", ".join(f"{k}({v})" for k, v in top3))
        if self._state.failure_streak >= 2:
            parts.append(f"连续失败 {self._state.failure_streak} 次")
        hint = self.get_emotion_hint()
        if hint:
            parts.append(hint)
        return "\n".join(parts)

    def get_suggestions(self) -> List[str]:
        """主动建议（自互动）

        基于当前状态给出提示，由 CLI 渲染。
        """
        suggestions: List[str] = []
        if self._state.failure_streak >= 3:
            suggestions.append(
                f"已连续失败 {self._state.failure_streak} 次，要不要 `/ai ask` 让我帮忙分析？"
            )
        # 高频命令建议别名
        for cmd, cnt in self._state.frequent_commands.items():
            if cnt >= 10 and len(cmd) > 4:
                suggestions.append(
                    f"「{cmd}」已执行 {cnt} 次，可以用 `/alias` 包装成短别名"
                )
                break
        # 重复长命令
        if len(self._state.last_commands) >= 3:
            from collections import Counter

            counter = Counter(self._state.last_commands)
            cmd, cnt = counter.most_common(1)[0]
            if cnt >= 3 and len(cmd) > 10:
                suggestions.append(f"命令「{cmd[:40]}...」重复了 {cnt} 次，建议定义别名")
        return suggestions

    @property
    def state(self) -> PerceptionState:
        return self._state

    def reset_session(self) -> None:
        """重置会话状态（保留长期记忆）"""
        long_term_frequent = dict(self._state.frequent_commands)
        long_term_errors = dict(self._state.common_errors)
        long_term_last = list(self._state.last_commands)
        self._state = PerceptionState.fresh()
        self._state.frequent_commands = long_term_frequent
        self._state.common_errors = long_term_errors
        self._state.last_commands = long_term_last
        self._emotion.reset()
