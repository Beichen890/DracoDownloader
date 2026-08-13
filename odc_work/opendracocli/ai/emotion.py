"""运维情感引擎 — 跨命令延续的角色情绪状态

复用上游 emotion/engine.py 的 4 因素混合 + 惯性机制，
标签从二次元陪伴语义（傲娇/护短/慵懒）重映射为运维语义。

8 维标签: 冷静/警觉/果断/谨慎/中性/得意/疲惫/焦虑

与上游的关键差异:
  - 上游靠 LLM 自报 [EMOTION:...] 标签作为 catalyst
  - P3 从命令执行结果推断用户操作信号（成功/失败/危险命令）
    —— 终端场景没有 LLM 自报情绪，改用客观执行信号
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..logger import get_logger

log = get_logger("ai.emotion")

# 8 维运维情感标签（顺序即向量索引）
EMOTION_LABELS: List[str] = [
    "冷静",   # 0
    "警觉",   # 1
    "果断",   # 2
    "谨慎",   # 3
    "中性",   # 4
    "得意",   # 5
    "疲惫",   # 6
    "焦虑",   # 7
]

# 软扩散：单标签激活时，相关维度获得微弱加成（复用上游 _SOFT_SPREAD 机制）
# (相关标签, 扩散系数)
_SOFT_SPREAD: Dict[str, List[tuple[str, float]]] = {
    "警觉": [("谨慎", 0.20), ("焦虑", 0.10)],
    "得意": [("果断", 0.15)],
    "疲惫": [("中性", 0.20), ("焦虑", 0.10)],
    "焦虑": [("谨慎", 0.15)],
    "果断": [("冷静", 0.10)],
    "谨慎": [("警觉", 0.10)],
    "冷静": [("中性", 0.15)],
}

# 标签别名归一化（复用上游 validate_tag 思路）
_TAG_ALIASES: Dict[str, str] = {
    "平静": "冷静", "镇定": "冷静", "稳定": "冷静",
    "警惕": "警觉", "紧张": "警觉",
    "决断": "果断", "干脆": "果断",
    "小心": "谨慎", "慎重": "谨慎",
    "正常": "中性", "平淡": "中性",
    "满意": "得意", "成功": "得意",
    "累": "疲惫", "疲倦": "疲惫",
    "急躁": "焦虑", "烦躁": "焦虑", "受挫": "焦虑",
}

_DIM = len(EMOTION_LABELS)


def _label_to_vector(label: str, intensity: float = 0.7) -> List[float]:
    """标签转向量 + 软扩散"""
    vec = [0.0] * _DIM
    if label not in EMOTION_LABELS:
        label = "中性"
    idx = EMOTION_LABELS.index(label)
    vec[idx] = intensity
    for related, spread_val in _SOFT_SPREAD.get(label, []):
        if related in EMOTION_LABELS:
            vec[EMOTION_LABELS.index(related)] += spread_val * intensity
    return vec


def _normalize(vec: List[float]) -> List[float]:
    """L2 归一化到单位向量"""
    norm = sum(v * v for v in vec) ** 0.5
    if norm < 1e-9:
        return vec
    return [v / norm for v in vec]


def validate_tag(tag: str) -> str:
    """标签校验/归一化"""
    if not tag:
        return "中性"
    t = tag.strip()
    if t in EMOTION_LABELS:
        return t
    return _TAG_ALIASES.get(t, "中性")


@dataclass
class EmotionResult:
    """情感计算结果"""

    dominant: str = "中性"
    intensity: float = 0.5
    hint: str = ""           # 注入 prompt 的提示文本
    vector: List[float] = field(default_factory=lambda: [0.0] * _DIM)
    components: Dict[str, List[float]] = field(default_factory=dict)


class EmotionEngine:
    """运维情感引擎

    4 因素混合:
      blended = w_user * user_vec      # 用户操作信号（命令成功/失败/危险）
              + w_history * hist_vec   # 历史惯性（上一轮情感）
              + w_character * char_vec # 角色卡 emotion_profile 基线
              + w_random * rand_vec    # 随机波动
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        inertia: float = 0.3,
        character_profile: Optional[Dict[str, float]] = None,
    ) -> None:
        w = weights or {}
        self._w_user = w.get("user", 0.4)
        self._w_history = w.get("history", 0.3)
        self._w_character = w.get("character", 0.2)
        self._w_random = w.get("random", 0.1)
        # 归一化
        total = self._w_user + self._w_history + self._w_character + self._w_random
        if total > 0:
            self._w_user /= total
            self._w_history /= total
            self._w_character /= total
            self._w_random /= total
        self._inertia = inertia
        self._character_profile = character_profile or {}
        self._last_vector: Optional[List[float]] = None

    def set_character_profile(self, profile: Dict[str, float]) -> None:
        """设置角色卡 emotion_profile 基线"""
        self._character_profile = profile

    def _user_signal_to_vector(
        self,
        *,
        success: bool,
        failure_streak: int = 0,
        is_high_risk: bool = False,
        is_frequent: bool = False,
        is_first_turn: bool = False,
    ) -> List[float]:
        """从命令执行结果推断用户操作信号情感

        替代上游 MLL 自报情绪 —— 终端场景用客观执行信号。
        """
        if is_first_turn:
            return _label_to_vector("中性", 0.6)
        if is_high_risk:
            return _label_to_vector("警觉", 0.8)
        if failure_streak >= 3:
            return _label_to_vector("焦虑", 0.7)  # 软扩散到 谨慎/疲惫
        if not success:
            vec = _label_to_vector("谨慎", 0.5)
            vec[EMOTION_LABELS.index("焦虑")] += 0.3
            return vec
        if is_frequent:
            return _label_to_vector("得意", 0.6)
        return _label_to_vector("冷静", 0.7)

    def _profile_to_vector(self) -> List[float]:
        """角色卡 emotion_profile → 基线向量"""
        if not self._character_profile:
            return _label_to_vector("中性", 0.3)
        vec = [0.0] * _DIM
        for label, weight in self._character_profile.items():
            tag = validate_tag(label)
            idx = EMOTION_LABELS.index(tag)
            vec[idx] += float(weight)
        total = sum(vec)
        if total > 0:
            vec = [v / total for v in vec]
        return vec

    def _random_vector(self) -> List[float]:
        """随机波动（各维独立均匀采样后归一化到 0.3 总量）"""
        vec = [random.random() for _ in range(_DIM)]
        return [v * 0.3 / max(sum(vec), 1e-9) for v in vec]

    def compute(
        self,
        *,
        success: bool = True,
        failure_streak: int = 0,
        is_high_risk: bool = False,
        is_frequent: bool = False,
        is_first_turn: bool = False,
    ) -> EmotionResult:
        """计算本轮情感

        Args:
            success: 命令是否成功
            failure_streak: 连续失败数
            is_high_risk: 是否高危命令
            is_frequent: 是否高频命令
            is_first_turn: 是否会话首条命令
        """
        user_vec = self._user_signal_to_vector(
            success=success,
            failure_streak=failure_streak,
            is_high_risk=is_high_risk,
            is_frequent=is_frequent,
            is_first_turn=is_first_turn,
        )
        char_vec = self._profile_to_vector()
        rand_vec = self._random_vector()

        # 历史惯性
        if self._last_vector is not None:
            hist_vec = self._last_vector
        else:
            hist_vec = _label_to_vector("中性", 0.4)

        blended = [
            self._w_user * user_vec[i]
            + self._w_history * hist_vec[i]
            + self._w_character * char_vec[i]
            + self._w_random * rand_vec[i]
            for i in range(_DIM)
        ]

        # 惯性平滑：本轮 70% + 上一轮 30%
        if self._last_vector is not None:
            blended = [
                (1 - self._inertia) * blended[i]
                + self._inertia * self._last_vector[i]
                for i in range(_DIM)
            ]

        blended = _normalize(blended)
        self._last_vector = blended

        # 提取主导情感
        max_idx = max(range(_DIM), key=lambda i: blended[i])
        dominant = EMOTION_LABELS[max_idx]
        intensity = blended[max_idx]

        # top 提示
        hint = f"【角色情绪基调】{dominant}({intensity:.2f})"

        return EmotionResult(
            dominant=dominant,
            intensity=intensity,
            hint=hint,
            vector=blended,
            components={
                "user": user_vec,
                "history": hist_vec,
                "character": char_vec,
                "random": rand_vec,
            },
        )

    def reset(self) -> None:
        """重置情感状态（新会话）"""
        self._last_vector = None
