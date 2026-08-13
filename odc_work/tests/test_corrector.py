"""corrector.py + prompt.py 测试 — AI 纠错"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from opendracocli.ai.character import CommandRole, DEFAULT_ROLE
from opendracocli.ai.client import LLMResponse
from opendracocli.ai.corrector import CorrectionResult, Corrector
from opendracocli.ai.prompt import (
    build_ask_prompt,
    build_correction_prompt,
    build_correction_user_message,
    build_risk_assess_prompt,
    extract_suggested_command,
    strip_analysis,
)


@pytest.fixture
def rm_role():
    return CommandRole(
        cmd="rm",
        name="清道夫",
        skeleton="你是清道夫，冷静利落的运维专家。",
        personality_core=["冷静", "利落"],
        personality_desc="清扫不需要的文件",
        speech_style="短促、确认型",
        speech_examples=[{"context": "成功", "line": "已清扫。"}],
        emotion_profile={"冷静": 0.5},
        trigger_map={"危险": "警觉"},
    )


def test_build_correction_prompt_includes_skeleton(rm_role):
    """prompt 包含骨架"""
    p = build_correction_prompt(rm_role, "")
    assert "清道夫" in p
    assert "冷静利落" in p


def test_build_correction_prompt_includes_personality(rm_role):
    """prompt 包含性格详情"""
    p = build_correction_prompt(rm_role, "")
    assert "清扫不需要的文件" in p


def test_build_correction_prompt_includes_speech_examples(rm_role):
    """prompt 包含说话示例"""
    p = build_correction_prompt(rm_role, "")
    assert "已清扫" in p
    assert "对话示例" in p


def test_build_correction_prompt_includes_emotion(rm_role):
    """prompt 包含情感提示"""
    p = build_correction_prompt(rm_role, "【角色情绪基调】警觉(0.65)")
    assert "警觉" in p
    assert "0.65" in p


def test_build_correction_prompt_includes_platform(rm_role):
    """prompt 包含平台"""
    p = build_correction_prompt(rm_role, "", platform="linux", native_shell="bash")
    assert "linux" in p
    assert "bash" in p


def test_build_correction_prompt_includes_constraints(rm_role):
    """prompt 包含约束"""
    p = build_correction_prompt(rm_role, "")
    assert "约束" in p
    assert "不要主动执行" in p
    assert "分析" in p
    assert "建议命令" in p


def test_build_ask_prompt_basic():
    """ask prompt"""
    p = build_ask_prompt(DEFAULT_ROLE, "")
    assert DEFAULT_ROLE.skeleton in p
    assert "markdown" in p


def test_build_ask_prompt_with_context():
    """ask prompt 含上下文"""
    p = build_ask_prompt(DEFAULT_ROLE, "警觉", "最近命令：ls")
    assert "警觉" in p
    assert "最近命令" in p


def test_build_risk_assess_prompt_basic():
    """risk assess prompt"""
    p = build_risk_assess_prompt()
    assert "安全审查员" in p
    assert "SAFE" in p
    assert "DANGER" in p
    assert "不可信输入" in p  # 抗注入警告


def test_build_risk_assess_prompt_with_platform():
    """risk assess prompt 含平台"""
    p = build_risk_assess_prompt(platform="win")
    assert "win" in p


def test_build_correction_user_message_xml_blocks():
    """user message 用 XML 块隔离"""
    msg = build_correction_user_message("rm -rf /", 1, "permission denied")
    assert "<command>rm -rf /</command>" in msg
    assert "<exit_code>1</exit_code>" in msg
    assert "<stderr>permission denied</stderr>" in msg


def test_build_correction_user_message_truncates_stderr():
    """stderr 过长截断"""
    long_err = "x" * 2000
    msg = build_correction_user_message("cmd", 1, long_err)
    # stderr 截到 1000（exit_code 标签内的 x 不算）
    # 提取 <stderr>...</stderr> 块
    import re
    m = re.search(r"<stderr>(.*?)</stderr>", msg, re.DOTALL)
    assert m is not None
    stderr_content = m.group(1)
    assert len(stderr_content) <= 1000
    assert len(stderr_content) == 1000


def test_build_correction_user_message_empty_stderr():
    """空 stderr 显示提示"""
    msg = build_correction_user_message("cmd", 1, "")
    assert "(无 stderr)" in msg


def test_extract_suggested_command_basic():
    """提取 <code> 块"""
    text = "分析：路径错误\n建议命令：<code>ls /tmp</code>"
    cmd = extract_suggested_command(text)
    assert cmd == "ls /tmp"


def test_extract_suggested_command_strips_backticks():
    """去掉 markdown 反引号"""
    text = "建议：<code>`ls /tmp`</code>"
    cmd = extract_suggested_command(text)
    assert cmd == "ls /tmp"


def test_extract_suggested_command_none():
    """无 code 块返回 None"""
    text = "分析：路径错误，无建议"
    assert extract_suggested_command(text) is None


def test_extract_suggested_command_empty():
    """空 code 块返回 None"""
    assert extract_suggested_command("<code></code>") is None


def test_strip_analysis_removes_code_block():
    """strip_analysis 去掉 code 块"""
    text = "分析：错误\n建议：<code>ls</code>"
    result = strip_analysis(text)
    assert "ls" not in result
    assert "分析" in result


@pytest.mark.asyncio
async def test_corrector_unavailable_client():
    """AI 不可用时返回 ok=False"""
    client = MagicMock()
    client.is_available.return_value = False
    corr = Corrector(client)
    result = await corr.correct("rm x", 1, "err", DEFAULT_ROLE)
    assert result.ok is False
    assert "不可用" in result.error


@pytest.mark.asyncio
async def test_corrector_success(rm_role):
    """成功纠错"""
    client = MagicMock()
    client.is_available.return_value = True
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content="分析：路径错误\n建议命令：<code>ls /tmp</code>",
            usage={},
        )
    )
    corr = Corrector(client, platform="linux", native_shell="bash")
    result = await corr.correct("rm x", 1, "no such file", rm_role)
    assert result.ok is True
    assert "路径错误" in result.analysis
    assert result.suggested_command == "ls /tmp"


@pytest.mark.asyncio
async def test_corrector_llm_failure(rm_role):
    """LLM 失败时 ok=False"""
    client = MagicMock()
    client.is_available.return_value = True
    client.chat = AsyncMock(
        return_value=LLMResponse(ok=False, error="network error")
    )
    corr = Corrector(client)
    result = await corr.correct("rm x", 1, "err", rm_role)
    assert result.ok is False
    assert "network error" in result.error


@pytest.mark.asyncio
async def test_corrector_no_suggested_command(rm_role):
    """无建议命令时 suggested_command 为空"""
    client = MagicMock()
    client.is_available.return_value = True
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content="分析：未知错误，无建议",
            usage={},
        )
    )
    corr = Corrector(client)
    result = await corr.correct("rm x", 1, "err", rm_role)
    assert result.ok is True
    assert result.suggested_command == ""
