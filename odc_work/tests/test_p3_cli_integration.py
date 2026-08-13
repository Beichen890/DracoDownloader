"""P3 CLI 集成测试 — 验证 ai_enabled=False 零开销 + 启用后组件就位"""
import pytest

from opendracocli.config import DracoConfig, set_global_config
from opendracocli.hooks.registry import HookRegistry, set_global_registry


@pytest.fixture
def isolated_cfg(tmp_path):
    """隔离配置"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())
    return cfg


def test_cli_ai_disabled_by_default(isolated_cfg):
    """ai_enabled 默认 False"""
    assert isolated_cfg.ai_enabled is False


def test_cli_ai_no_hooks_when_disabled(isolated_cfg):
    """ai_enabled=False 时不注册 AI 钩子"""
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._ai_enabled is False
    assert app._llm_client is None
    assert app._correction_hook is None
    assert app._perception_hook is None
    assert app._ai_risk_hook is None
    # 只有 P2 风控钩子
    reg = app._pipeline._hooks
    assert len(reg._pre_hooks) == 1  # RiskAssessmentHook
    assert len(reg._post_hooks) == 0


def test_cli_ai_enabled_registers_hooks(tmp_path):
    """ai_enabled=True 时注册 AI 钩子"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        ai_enabled=True,
        ai_api_key="sk-test-key-12345",
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._ai_enabled is True
    assert app._llm_client is not None
    assert app._correction_hook is not None
    assert app._perception_hook is not None
    assert app._ai_risk_hook is not None
    # AI hooks 已注册
    reg = app._pipeline._hooks
    assert len(reg._pre_hooks) == 2  # RiskAssessmentHook + AIRiskHook
    assert len(reg._post_hooks) == 2  # CorrectionHook + PerceptionHook


def test_cli_ai_on_enables(tmp_path):
    """/ai on 启用 AI"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        ai_enabled=False,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._ai_enabled is False
    app._ai_on()
    assert app._ai_enabled is True
    assert app._llm_client is not None


def test_cli_ai_off_disables(tmp_path):
    """/ai off 禁用 AI"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        ai_enabled=True,
        ai_api_key="sk-test-key-12345",
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._ai_enabled is True
    app._ai_off()
    assert app._ai_enabled is False
    assert app._llm_client is None
    # P2 风控钩子仍在
    reg = app._pipeline._hooks
    assert len(reg._pre_hooks) == 1
    assert len(reg._post_hooks) == 0


def test_cli_ai_status_shows_state(tmp_path):
    """/ai 显示状态"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        ai_enabled=False,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    # 不抛异常即可
    app._show_ai_status()


def test_cli_key_hint_protects_privacy():
    """API key 只显示前后缀"""
    from opendracocli.cli import OpenDracoCLI

    assert OpenDracoCLI._key_hint("") == "(未配置)"
    assert OpenDracoCLI._key_hint("short") == "*****"  # 5 字符全遮
    assert OpenDracoCLI._key_hint("sk-1234567890abcdef") == "sk-1...cdef"
    # 不暴露完整 key
    hint = OpenDracoCLI._key_hint("sk-secret-key-1234567890")
    assert "secret-key" not in hint


async def test_cli_ai_handle_slash_routes(tmp_path):
    """/ai 子命令路由"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        ai_enabled=False,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()

    # 未启用 AI 时调用各子命令不应抛异常
    await app._handle_ai_cmd("")  # status
    await app._handle_ai_cmd("off")  # already off
    await app._handle_ai_cmd("context")  # not enabled
    await app._handle_ai_cmd("emotion")  # not enabled
    await app._handle_ai_cmd("role")  # not enabled
    await app._handle_ai_cmd("unknown_sub")  # unknown


async def test_p3_no_regression_on_p1_pipeline(tmp_path):
    """ai_enabled=False 时 P1 管线正常工作"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        ai_context_file=str(tmp_path / "context.json"),
        ai_enabled=False,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    # 简单 echo 命令应成功
    result = await app._pipeline.run("echo hello_p3", session_id="test")
    assert result.success
    assert "hello_p3" in result.stdout
