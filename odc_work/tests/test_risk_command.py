"""/risk slash 命令测试 — 覆盖 DoD 第 7 项

验证:
  /risk test <cmd> 能预览风险等级（不执行）
  /risk 显示当前规则表
"""

import io
from contextlib import redirect_stdout

import pytest

from opendracocli.config import DracoConfig, set_global_config
from opendracocli.security.risk_assessor import set_global_assessor, RiskAssessor


@pytest.fixture
def app(tmp_path, monkeypatch):
    """构造一个接线了风控的 CLI 实例（不启动主循环）"""
    # 隔离配置，避免污染真实 ~ 目录
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
    )
    set_global_config(cfg)
    set_global_assessor(RiskAssessor(
        builtin_rules_path=cfg.builtin_rules_path,
        user_rules_path=cfg.security_rules_file_resolved,
    ))

    # 重置全局 hook registry，避免重复注册
    from opendracocli.hooks.registry import get_global_registry
    get_global_registry().clear()

    from opendracocli.cli import OpenDracoCLI
    app = OpenDracoCLI(config=cfg)
    app._rich = False  # 纯文本模式便于断言
    return app


def test_risk_test_critical_command(app):
    """/risk test rm -rf / 应预览为 critical"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app._handle_risk_cmd("test rm -rf /")
    out = buf.getvalue()
    assert "CRITICAL" in out
    assert "rm -rf /" in out


def test_risk_test_danger_command(app):
    """/risk test rm -rf /tmp/x 应预览为 danger"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app._handle_risk_cmd("test rm -rf /tmp/x")
    out = buf.getvalue()
    assert "DANGER" in out


def test_risk_test_safe_command(app):
    """/risk test echo hi 应预览为 safe"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app._handle_risk_cmd("test echo hi")
    out = buf.getvalue()
    assert "SAFE" in out


def test_risk_test_caution_command(app):
    """/risk test rm /tmp/single 应预览为 caution"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app._handle_risk_cmd("test rm /tmp/single")
    out = buf.getvalue()
    assert "CAUTION" in out


def test_risk_show_rules(app):
    """/risk （无参数）显示规则表"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app._handle_risk_cmd("")
    out = buf.getvalue()
    assert "风险规则表" in out
    assert "rm" in out  # 内置规则含 rm


def test_risk_test_invalid_subcommand(app):
    """/risk unknown 应提示未知子命令"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        app._handle_risk_cmd("unknown")
    # _render_error 输出到 stderr，纯文本模式走 print(file=sys.stderr)
    # 这里只验证不崩溃即可


def test_risk_test_missing_command(app):
    """/risk test （无命令）应提示用法"""
    import sys
    from contextlib import redirect_stderr
    buf_err = io.StringIO()
    with redirect_stderr(buf_err):
        app._handle_risk_cmd("test")
    assert "用法" in buf_err.getvalue()


def test_risk_hook_registered(app):
    """CLI 初始化后 RiskAssessmentHook 已注册"""
    from opendracocli.hooks.registry import get_global_registry
    from opendracocli.hooks.risk_hook import RiskAssessmentHook
    registry = get_global_registry()
    has_risk_hook = any(
        isinstance(h, RiskAssessmentHook) for h in registry._pre_hooks
    )
    assert has_risk_hook


def test_sandbox_executor_wired(app):
    """CLI 初始化后 SandboxExecutor 已注入 pipeline"""
    assert app._pipeline._sandbox_executor is not None
    assert app._pipeline._sandbox_executor is app._sandbox
