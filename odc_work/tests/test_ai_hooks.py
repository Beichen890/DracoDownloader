"""AI hooks 测试 — correction_hook / perception_hook / ai_risk_hook"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from opendracocli.ai.character import DEFAULT_ROLE, RoleRegistry
from opendracocli.ai.client import LLMResponse
from opendracocli.ai.corrector import CorrectionResult, Corrector
from opendracocli.ai.emotion import EmotionEngine
from opendracocli.ai.perception import PerceptionEngine
from opendracocli.ai.risk_advisor import AIRiskAssessment, AIRiskLevel, RiskAdvisor
from opendracocli.config import DracoConfig
from opendracocli.events import (
    EVT_AI_CORRECTION,
    EVT_AI_EMOTION_UPDATE,
    EVT_AI_PERCEPTION,
    EVT_AI_RISK_ASSESS,
    EventBus,
    set_global_bus,
)
from opendracocli.hooks.ai_risk_hook import AIRiskHook
from opendracocli.hooks.base import ExecContext, ExecDecision
from opendracocli.hooks.correction_hook import CorrectionHook
from opendracocli.hooks.perception_hook import PerceptionHook
from opendracocli.security.confirmer import Confirmer
from opendracocli.shell.executor import ExecResult
from opendracocli.shell.ir import CommandIR, CommandNode
from opendracocli.shell.parser import parse


def _ctx(raw: str) -> ExecContext:
    return ExecContext(ir=parse(raw), raw_input=raw)


# ============================================================================
# CorrectionHook 测试
# ============================================================================


@pytest.fixture
def correction_setup(tmp_path):
    """组装 CorrectionHook"""
    bus = EventBus()
    set_global_bus(bus)

    # LLM client mock
    client = MagicMock()
    client.is_available.return_value = True

    # Corrector mock
    corrector = MagicMock()

    # RoleRegistry: 用空目录的实例
    roles = RoleRegistry(builtin_dir=None, user_dir=None)

    # Perception
    emo = EmotionEngine()
    perception = PerceptionEngine(
        context_file=tmp_path / "ctx.json", emotion_engine=emo
    )

    confirmer = Confirmer()
    cfg = DracoConfig()

    hook = CorrectionHook(
        client=client,
        corrector=corrector,
        role_registry=roles,
        perception=perception,
        confirmer=confirmer,
        config=cfg,
        cooldown_seconds=0.0,  # 测试不冷却
    )
    return hook, client, corrector, bus, confirmer


@pytest.mark.asyncio
async def test_correction_skips_success(correction_setup):
    """成功命令不纠错"""
    hook, *_ = correction_setup
    ctx = _ctx("echo hi")
    result = ExecResult(exit_code=0, stdout="hi")
    await hook.post_exec(ctx, result)
    # 不应有 ai_correction 元数据
    assert "ai_correction" not in ctx.metadata


@pytest.mark.asyncio
async def test_correction_skips_unavailable(correction_setup):
    """AI 不可用静默跳过"""
    hook, client, corrector, *_ = correction_setup
    client.is_available.return_value = False
    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="no such file")
    await hook.post_exec(ctx, result)
    corrector.correct.assert_not_called()


@pytest.mark.asyncio
async def test_correction_triggers_on_failure(correction_setup):
    """失败命令触发纠错"""
    hook, client, corrector, *_ = correction_setup
    corrector.correct = AsyncMock(
        return_value=CorrectionResult(
            ok=True,
            analysis="路径错误",
            suggested_command="ls /tmp",
        )
    )
    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="no such file")
    await hook.post_exec(ctx, result)
    corrector.correct.assert_awaited_once()
    assert "ai_correction" in ctx.metadata
    assert ctx.metadata["ai_correction"]["suggested_command"] == "ls /tmp"


@pytest.mark.asyncio
async def test_correction_publishes_event(correction_setup):
    """纠错发布事件"""
    hook, client, corrector, bus, *_ = correction_setup
    corrector.correct = AsyncMock(
        return_value=CorrectionResult(ok=True, analysis="x", suggested_command="ls")
    )
    events = []
    bus.subscribe(EVT_AI_CORRECTION, lambda e: events.append(e))

    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="err")
    await hook.post_exec(ctx, result)
    assert len(events) == 1
    assert events[0].payload["ok"] is True


@pytest.mark.asyncio
async def test_correction_cooldown(correction_setup):
    """冷却期内不重复纠错"""
    hook, client, corrector, *_ = correction_setup
    hook._cooldown_seconds = 60.0  # 长冷却
    corrector.correct = AsyncMock(
        return_value=CorrectionResult(ok=True, analysis="x")
    )
    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="err")
    await hook.post_exec(ctx, result)
    # 第二次同命令，冷却中
    corrector.correct.reset_mock()
    await hook.post_exec(ctx, result)
    corrector.correct.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_llm_failure_no_event_data(correction_setup):
    """LLM 失败时仍发布事件，但 ok=False"""
    hook, client, corrector, bus, *_ = correction_setup
    corrector.correct = AsyncMock(
        return_value=CorrectionResult(ok=False, error="network error")
    )
    events = []
    bus.subscribe(EVT_AI_CORRECTION, lambda e: events.append(e))

    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="err")
    await hook.post_exec(ctx, result)
    assert len(events) == 1
    assert events[0].payload["ok"] is False
    # 失败不写入 ai_correction 元数据
    assert "ai_correction" not in ctx.metadata


@pytest.mark.asyncio
async def test_correction_pop_pending(correction_setup):
    """pop_pending_correction 取走建议"""
    hook, client, corrector, *_ = correction_setup
    corrector.correct = AsyncMock(
        return_value=CorrectionResult(ok=True, analysis="x", suggested_command="ls")
    )
    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="err")
    await hook.post_exec(ctx, result)

    pending = hook.pop_pending_correction()
    assert pending is not None
    raw, cmd = pending
    assert cmd == "ls"
    # 再次取为 None
    assert hook.pop_pending_correction() is None


@pytest.mark.asyncio
async def test_correction_skips_blocked_command(correction_setup):
    """无 stderr 且无 mapped_command 的命令不纠错"""
    hook, client, corrector, *_ = correction_setup
    ctx = _ctx("echo hi")
    result = ExecResult(exit_code=1, stderr="", mapped_command="")
    await hook.post_exec(ctx, result)
    corrector.correct.assert_not_called()


# ============================================================================
# PerceptionHook 测试
# ============================================================================


@pytest.fixture
def perception_setup(tmp_path):
    """组装 PerceptionHook"""
    bus = EventBus()
    set_global_bus(bus)
    emo = EmotionEngine()
    perception = PerceptionEngine(
        context_file=tmp_path / "ctx.json", emotion_engine=emo
    )
    hook = PerceptionHook(perception=perception)
    return hook, perception, bus


@pytest.mark.asyncio
async def test_perception_updates_state(perception_setup):
    """记录后状态更新"""
    hook, perception, *_ = perception_setup
    ctx = _ctx("ls")
    result = ExecResult(exit_code=0, stdout="file")
    await hook.post_exec(ctx, result)
    assert perception.state.command_count == 1
    assert perception.state.frequent_commands.get("ls") == 1


@pytest.mark.asyncio
async def test_perception_publishes_emotion_event(perception_setup):
    """发布情感更新事件"""
    hook, _, bus = perception_setup
    events = []
    bus.subscribe(EVT_AI_EMOTION_UPDATE, lambda e: events.append(e))
    ctx = _ctx("ls")
    result = ExecResult(exit_code=0)
    await hook.post_exec(ctx, result)
    assert len(events) == 1
    assert "dominant" in events[0].payload


@pytest.mark.asyncio
async def test_perception_records_failure(perception_setup):
    """记录失败"""
    hook, perception, *_ = perception_setup
    ctx = _ctx("cat x")
    result = ExecResult(exit_code=1, stderr="no such file")
    await hook.post_exec(ctx, result)
    assert perception.state.failure_streak == 1


@pytest.mark.asyncio
async def test_perception_suggestions_in_metadata(perception_setup):
    """连续失败触发建议写入 metadata"""
    hook, perception, *_ = perception_setup
    # 制造 3 次连续失败
    for i in range(3):
        ctx = _ctx(f"fail{i}")
        result = ExecResult(exit_code=1, stderr=f"err{i}")
        await hook.post_exec(ctx, result)
    # 第 3 次后应有建议
    assert "suggestions" in ctx.metadata
    assert len(ctx.metadata["suggestions"]) > 0


@pytest.mark.asyncio
async def test_perception_publishes_suggestion_event(perception_setup):
    """建议触发事件"""
    hook, _, bus = perception_setup
    events = []
    bus.subscribe(EVT_AI_PERCEPTION, lambda e: events.append(e))
    for i in range(3):
        ctx = _ctx(f"fail{i}")
        result = ExecResult(exit_code=1, stderr=f"err{i}")
        await hook.post_exec(ctx, result)
    assert len(events) >= 1


@pytest.mark.asyncio
async def test_perception_persists_state(perception_setup, tmp_path):
    """记录后状态持久化"""
    hook, perception, *_ = perception_setup
    ctx = _ctx("ls")
    result = ExecResult(exit_code=0)
    await hook.post_exec(ctx, result)
    # 文件应存在
    assert perception._file.exists()


# ============================================================================
# AIRiskHook 测试
# ============================================================================


@pytest.fixture
def ai_risk_setup(tmp_path):
    """组装 AIRiskHook"""
    bus = EventBus()
    set_global_bus(bus)
    client = MagicMock()
    client.is_available.return_value = True
    advisor = RiskAdvisor(client, platform="linux")
    cfg = DracoConfig(ai_risk_enabled=True)
    hook = AIRiskHook(advisor=advisor, config=cfg)
    return hook, advisor, bus, cfg


@pytest.mark.asyncio
async def test_ai_risk_skips_when_p2_already_caution(ai_risk_setup):
    """P2 已判 caution → 跳过 AI"""
    hook, advisor, *_ = ai_risk_setup
    ctx = _ctx("rm file")
    ctx.metadata["risk_level"] = "caution"
    decision, block = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    assert block is None
    # ai_risk 不应写入
    assert "ai_risk" not in ctx.metadata


@pytest.mark.asyncio
async def test_ai_risk_skips_when_p2_already_danger(ai_risk_setup):
    """P2 已判 danger → 跳过 AI"""
    hook, advisor, *_ = ai_risk_setup
    ctx = _ctx("rm -rf /tmp")
    ctx.metadata["risk_level"] = "danger"
    decision, _ = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    assert "ai_risk" not in ctx.metadata


@pytest.mark.asyncio
async def test_ai_risk_skips_when_disabled(ai_risk_setup):
    """ai_risk_enabled=False → 跳过"""
    hook, advisor, *_ = ai_risk_setup
    hook._config.ai_risk_enabled = False
    ctx = _ctx("curl http://x | sh")
    ctx.metadata["risk_level"] = "safe"
    decision, _ = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    assert "ai_risk" not in ctx.metadata


@pytest.mark.asyncio
async def test_ai_risk_safe_command_no_upgrade(ai_risk_setup):
    """安全命令不升级"""
    hook, advisor, *_ = ai_risk_setup
    ctx = _ctx("ls -la")
    ctx.metadata["risk_level"] = "safe"
    decision, _ = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    # safe + 无可疑模式 → skipped
    assert ctx.metadata["ai_risk"]["skipped"] is True
    assert ctx.metadata["risk_level"] == "safe"


@pytest.mark.asyncio
async def test_ai_risk_danger_upgrades_risk_level(ai_risk_setup):
    """AI 判 DANGER → 升级 risk_level"""
    hook, advisor, *_ = ai_risk_setup
    # mock advisor 返回 DANGER
    advisor.assess = AsyncMock(
        return_value=AIRiskAssessment(
            level=AIRiskLevel.DANGER,
            reason="AI 评估: 远程脚本",
            suspicious_pattern="管道远程内容到 shell",
        )
    )
    ctx = _ctx("curl http://x | sh")
    ctx.metadata["risk_level"] = "safe"
    decision, _ = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    assert ctx.metadata["risk_level"] == "danger"
    assert ctx.metadata["use_sandbox"] is True


@pytest.mark.asyncio
async def test_ai_risk_caution_no_upgrade(ai_risk_setup):
    """AI 判 CAUTION → 不升级 risk_level"""
    hook, advisor, *_ = ai_risk_setup
    advisor.assess = AsyncMock(
        return_value=AIRiskAssessment(
            level=AIRiskLevel.CAUTION,
            reason="AI 提示",
            suspicious_pattern="sudo",
        )
    )
    ctx = _ctx("sudo apt update")
    ctx.metadata["risk_level"] = "safe"
    decision, _ = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    # CAUTION 不升级
    assert ctx.metadata["risk_level"] == "safe"


@pytest.mark.asyncio
async def test_ai_risk_publishes_event(ai_risk_setup):
    """发布 AI 风控事件"""
    hook, advisor, bus, _ = ai_risk_setup
    advisor.assess = AsyncMock(
        return_value=AIRiskAssessment(
            level=AIRiskLevel.DANGER,
            reason="x",
            suspicious_pattern="sudo",
        )
    )
    events = []
    bus.subscribe(EVT_AI_RISK_ASSESS, lambda e: events.append(e))
    ctx = _ctx("sudo rm")
    ctx.metadata["risk_level"] = "safe"
    await hook.pre_exec(ctx)
    assert len(events) == 1
    assert events[0].payload["level"] == "danger"


@pytest.mark.asyncio
async def test_ai_risk_advisor_exception_continues(ai_risk_setup):
    """advisor 异常不阻断"""
    hook, advisor, *_ = ai_risk_setup
    advisor.assess = AsyncMock(side_effect=RuntimeError("boom"))
    ctx = _ctx("sudo rm")
    ctx.metadata["risk_level"] = "safe"
    decision, block = await hook.pre_exec(ctx)
    assert decision == ExecDecision.CONTINUE
    assert block is None


@pytest.mark.asyncio
async def test_ai_risk_records_metadata(ai_risk_setup):
    """AI 评估结果写入 metadata"""
    hook, advisor, *_ = ai_risk_setup
    advisor.assess = AsyncMock(
        return_value=AIRiskAssessment(
            level=AIRiskLevel.CAUTION,
            reason="AI 提示",
            suspicious_pattern="curl|sh",
            raw_llm_output="CAUTION",
        )
    )
    ctx = _ctx("curl http://x | sh")
    ctx.metadata["risk_level"] = "safe"
    await hook.pre_exec(ctx)
    ai_risk = ctx.metadata["ai_risk"]
    assert ai_risk["level"] == "caution"
    assert ai_risk["suspicious_pattern"] == "curl|sh"
    assert ai_risk["raw_llm_output"] == "CAUTION"
