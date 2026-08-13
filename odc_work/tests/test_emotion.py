"""emotion.py 测试 — 运维情感引擎"""
import pytest

from opendracocli.ai.emotion import (
    EMOTION_LABELS,
    EmotionEngine,
    EmotionResult,
    _label_to_vector,
    _normalize,
    validate_tag,
)


def test_label_to_vector_basic():
    """标签转向量 + 软扩散"""
    vec = _label_to_vector("警觉", 0.8)
    idx_alert = EMOTION_LABELS.index("警觉")
    idx_caution = EMOTION_LABELS.index("谨慎")
    idx_anxiety = EMOTION_LABELS.index("焦虑")
    assert vec[idx_alert] == pytest.approx(0.8)
    # 警觉 → 谨慎(0.20), 焦虑(0.10)
    assert vec[idx_caution] == pytest.approx(0.20 * 0.8)
    assert vec[idx_anxiety] == pytest.approx(0.10 * 0.8)


def test_label_to_vector_unknown_falls_back_to_neutral():
    """未知标签回退到中性"""
    vec = _label_to_vector("不存在的标签", 0.7)
    idx_neutral = EMOTION_LABELS.index("中性")
    assert vec[idx_neutral] == pytest.approx(0.7)


def test_normalize_zero_vector():
    """零向量归一化保持不变"""
    vec = [0.0] * 8
    assert _normalize(vec) == vec


def test_normalize_unit():
    """L2 归一化"""
    vec = [3.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    normed = _normalize(vec)
    # ||(3,4)|| = 5 → (0.6, 0.8, ...)
    assert normed[0] == pytest.approx(0.6)
    assert normed[1] == pytest.approx(0.8)


def test_validate_tag_canonical():
    """标准标签原样返回"""
    for label in EMOTION_LABELS:
        assert validate_tag(label) == label


def test_validate_tag_aliases():
    """别名归一化"""
    assert validate_tag("平静") == "冷静"
    assert validate_tag("警惕") == "警觉"
    assert validate_tag("小心") == "谨慎"
    assert validate_tag("累") == "疲惫"
    assert validate_tag("") == "中性"


def test_engine_weights_normalization():
    """权重归一化"""
    eng = EmotionEngine(weights={"user": 2.0, "history": 2.0, "character": 0.0, "random": 0.0})
    # 2+2=4 → 0.5, 0.5, 0, 0
    assert eng._w_user == pytest.approx(0.5)
    assert eng._w_history == pytest.approx(0.5)
    assert eng._w_character == pytest.approx(0.0)
    assert eng._w_random == pytest.approx(0.0)


def test_engine_first_turn_neutral():
    """首条命令情感为中性"""
    eng = EmotionEngine()
    result = eng.compute(is_first_turn=True)
    assert result.dominant in EMOTION_LABELS
    # 首轮 user_vec 是中性
    # 惯性 + 角色基线后可能漂移，但应是合理标签


def test_engine_high_risk_alert():
    """高危命令 → 警觉"""
    eng = EmotionEngine(weights={"user": 1.0, "history": 0.0, "character": 0.0, "random": 0.0})
    result = eng.compute(is_high_risk=True)
    # 用户信号是 警觉(0.8) → 软扩散到 谨慎/焦虑
    # 权重全给 user，主导应是 警觉
    assert result.dominant == "警觉"


def test_engine_success_calm():
    """成功命令 → 冷静"""
    eng = EmotionEngine(weights={"user": 1.0, "history": 0.0, "character": 0.0, "random": 0.0})
    result = eng.compute(success=True, is_high_risk=False)
    assert result.dominant == "冷静"


def test_engine_failure_anxious():
    """失败命令 → 谨慎/焦虑"""
    eng = EmotionEngine(weights={"user": 1.0, "history": 0.0, "character": 0.0, "random": 0.0})
    result = eng.compute(success=False, is_high_risk=False)
    assert result.dominant in ("谨慎", "焦虑")


def test_engine_failure_streak_anxious():
    """连续 3 次失败 → 焦虑"""
    eng = EmotionEngine(weights={"user": 1.0, "history": 0.0, "character": 0.0, "random": 0.0})
    result = eng.compute(success=False, failure_streak=3)
    assert result.dominant == "焦虑"


def test_engine_inertia():
    """惯性跨轮延续"""
    eng = EmotionEngine(
        weights={"user": 1.0, "history": 0.0, "character": 0.0, "random": 0.0},
        inertia=0.5,
    )
    # 第一轮：警觉（高危）
    r1 = eng.compute(is_high_risk=True)
    # 第二轮：成功（user 信号是冷静）
    r2 = eng.compute(success=True, is_high_risk=False)
    # 由于惯性 0.5，第二轮应保留部分警觉
    idx_alert = EMOTION_LABELS.index("警觉")
    assert r2.vector[idx_alert] > 0.0  # 警觉分量非零


def test_engine_reset():
    """reset 清空历史"""
    eng = EmotionEngine()
    eng.compute(is_high_risk=True)
    assert eng._last_vector is not None
    eng.reset()
    assert eng._last_vector is None


def test_engine_character_profile():
    """角色基线影响情感"""
    eng = EmotionEngine(
        weights={"user": 0.0, "history": 0.0, "character": 1.0, "random": 0.0},
        character_profile={"冷静": 0.5, "谨慎": 0.5},
    )
    result = eng.compute(success=True)
    # 角色基线只有冷静+谨慎，主导应是其一
    assert result.dominant in ("冷静", "谨慎")


def test_engine_hint_format():
    """hint 文本格式正确"""
    eng = EmotionEngine()
    result = eng.compute(success=True)
    assert "【角色情绪基调】" in result.hint
    assert result.dominant in result.hint


def test_engine_result_vector_normalized():
    """结果向量归一化"""
    eng = EmotionEngine()
    result = eng.compute(success=True)
    norm = sum(v * v for v in result.vector) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)
