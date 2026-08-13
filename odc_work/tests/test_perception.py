"""perception.py 测试 — 感知与自互动"""
import json
from pathlib import Path

import pytest

from opendracocli.ai.emotion import EMOTION_LABELS, EmotionEngine
from opendracocli.ai.perception import (
    PerceptionEngine,
    PerceptionState,
    _MAX_FREQ_ENTRIES,
    _MAX_LAST_COMMANDS,
)


@pytest.fixture
def engine(tmp_path):
    """感知引擎（tmp_path 隔离）"""
    emo = EmotionEngine()
    return PerceptionEngine(context_file=tmp_path / "context.json", emotion_engine=emo)


def test_state_fresh():
    """fresh state 字段默认"""
    s = PerceptionState.fresh()
    assert s.command_count == 0
    assert s.failure_streak == 0
    assert s.frequent_commands == {}
    assert s.last_commands == []
    assert s.session_started_at > 0


def test_state_to_dict_roundtrip():
    """状态序列化往返"""
    s = PerceptionState(
        command_count=5,
        failure_streak=2,
        frequent_commands={"ls": 3},
        last_commands=["ls", "pwd"],
        last_emotion_vector=[0.1] * 8,
    )
    d = s.to_dict()
    s2 = PerceptionState.from_dict(d)
    assert s2.command_count == 5
    assert s2.failure_streak == 2
    assert s2.frequent_commands == {"ls": 3}
    assert s2.last_commands == ["ls", "pwd"]


def test_load_nonexistent_file(tmp_path):
    """加载不存在的文件返回 fresh"""
    emo = EmotionEngine()
    eng = PerceptionEngine(context_file=tmp_path / "nofile.json", emotion_engine=emo)
    assert eng.state.command_count == 0


def test_load_invalid_json(tmp_path):
    """非法 JSON 返回 fresh"""
    f = tmp_path / "bad.json"
    f.write_text("not json{", encoding="utf-8")
    emo = EmotionEngine()
    eng = PerceptionEngine(context_file=f, emotion_engine=emo)
    assert eng.state.command_count == 0


def test_record_success_increments_count(engine):
    """成功记录 +1 计数"""
    engine.record(
        raw_input="ls",
        success=True,
        exit_code=0,
        is_high_risk=False,
        first_token="ls",
    )
    assert engine.state.command_count == 1
    assert engine.state.failure_streak == 0
    assert engine.state.frequent_commands["ls"] == 1


def test_record_failure_increments_streak(engine):
    """失败记录 +1 连击"""
    engine.record(
        raw_input="cat x",
        success=False,
        exit_code=1,
        is_high_risk=False,
        first_token="cat",
        stderr="no such file",
    )
    assert engine.state.failure_streak == 1
    assert "no such file" in engine.state.common_errors


def test_record_success_resets_streak(engine):
    """成功重置连击"""
    engine.record(
        raw_input="fail1", success=False, exit_code=1,
        is_high_risk=False, first_token="fail1", stderr="e1",
    )
    engine.record(
        raw_input="ok", success=True, exit_code=0,
        is_high_risk=False, first_token="ok",
    )
    assert engine.state.failure_streak == 0


def test_record_appends_last_commands(engine):
    """最近命令滑动窗口"""
    for i in range(_MAX_LAST_COMMANDS + 3):
        engine.record(
            raw_input=f"cmd{i}",
            success=True,
            exit_code=0,
            is_high_risk=False,
            first_token=f"cmd{i}",
        )
    # 不超过上限
    assert len(engine.state.last_commands) <= _MAX_LAST_COMMANDS
    # 保留最新的（最后的 cmd 应是 cmd{max+2}）
    assert engine.state.last_commands[-1] == f"cmd{_MAX_LAST_COMMANDS + 2}"


def test_record_freq_caps_max(engine):
    """命令频次表上限"""
    for i in range(_MAX_FREQ_ENTRIES + 5):
        engine.record(
            raw_input=f"unique{i}",
            success=True,
            exit_code=0,
            is_high_risk=False,
            first_token=f"unique{i}",
        )
    assert len(engine.state.frequent_commands) <= _MAX_FREQ_ENTRIES


def test_save_and_reload(tmp_path):
    """保存后再加载"""
    f = tmp_path / "ctx.json"
    emo1 = EmotionEngine()
    eng1 = PerceptionEngine(context_file=f, emotion_engine=emo1)
    eng1.record(
        raw_input="ls", success=True, exit_code=0,
        is_high_risk=False, first_token="ls",
    )
    eng1.save()

    emo2 = EmotionEngine()
    eng2 = PerceptionEngine(context_file=f, emotion_engine=emo2)
    assert eng2.state.command_count == 1
    assert eng2.state.frequent_commands.get("ls") == 1


def test_get_emotion_hint_empty(engine):
    """无情感数据时 hint 为空"""
    assert engine.get_emotion_hint() == ""


def test_get_emotion_hint_after_record(engine):
    """记录后 hint 非空"""
    engine.record(
        raw_input="ls", success=True, exit_code=0,
        is_high_risk=False, first_token="ls",
    )
    hint = engine.get_emotion_hint()
    assert "【角色情绪基调】" in hint


def test_get_context_summary(engine):
    """上下文摘要"""
    engine.record(
        raw_input="ls -la", success=True, exit_code=0,
        is_high_risk=False, first_token="ls",
    )
    engine.record(
        raw_input="pwd", success=True, exit_code=0,
        is_high_risk=False, first_token="pwd",
    )
    summary = engine.get_context_summary()
    assert "ls" in summary
    assert "最近命令" in summary


def test_get_suggestions_no_data(engine):
    """无数据时无建议"""
    assert engine.get_suggestions() == []


def test_get_suggestions_failure_streak(engine):
    """连续失败 3 次触发建议"""
    for i in range(3):
        engine.record(
            raw_input=f"fail{i}",
            success=False,
            exit_code=1,
            is_high_risk=False,
            first_token=f"fail{i}",
            stderr=f"err{i}",
        )
    suggestions = engine.get_suggestions()
    assert any("失败" in s for s in suggestions)


def test_get_suggestions_high_freq(engine):
    """高频命令触发别名建议"""
    for _ in range(10):
        engine.record(
            raw_input="git_status_long_cmd",
            success=True,
            exit_code=0,
            is_high_risk=False,
            first_token="git_status_long_cmd",
        )
    suggestions = engine.get_suggestions()
    # 至少有一条建议提到别名
    assert any("别名" in s or "alias" in s for s in suggestions)


def test_reset_session_preserves_long_term(engine):
    """重置会话保留长期记忆"""
    for _ in range(5):
        engine.record(
            raw_input="ls",
            success=True,
            exit_code=0,
            is_high_risk=False,
            first_token="ls",
        )
    engine.reset_session()
    # 长期记忆保留
    assert engine.state.frequent_commands.get("ls") == 5
    # 会话计数重置
    assert engine.state.command_count == 0


def test_save_creates_parent_dir(tmp_path):
    """保存时创建父目录"""
    f = tmp_path / "subdir" / "ctx.json"
    emo = EmotionEngine()
    eng = PerceptionEngine(context_file=f, emotion_engine=emo)
    eng.save()
    assert f.exists()


def test_save_atomic(tmp_path):
    """原子写入：.tmp → replace"""
    f = tmp_path / "ctx.json"
    emo = EmotionEngine()
    eng = PerceptionEngine(context_file=f, emotion_engine=emo)
    eng.save()
    assert f.exists()
    # .tmp 应被清理（已 rename）
    assert not (tmp_path / "ctx.tmp").exists()


def test_emotion_vector_recorded(engine):
    """记录后情感向量被保存"""
    result = engine.record(
        raw_input="ls", success=True, exit_code=0,
        is_high_risk=False, first_token="ls",
    )
    assert len(result.vector) == len(EMOTION_LABELS)
    assert engine.state.last_emotion_vector == result.vector
