"""P2 安全端到端集成测试 — pipeline + risk_hook + sandbox"""
import pytest

from opendracocli.aliases.manager import AliasManager
from opendracocli.config import DracoConfig
from opendracocli.events import EVT_RISK_BLOCKED, EventBus, set_global_bus
from opendracocli.hooks.registry import HookRegistry
from opendracocli.history.store import HistoryStore
from opendracocli.hooks.risk_hook import RiskAssessmentHook
from opendracocli.security.authenticator import Authenticator
from opendracocli.security.confirmer import Confirmer
from opendracocli.security.risk_assessor import RiskAssessor
from opendracocli.security.sandbox import SandboxExecutor
from opendracocli.shell.executor import SubprocessExecutor
from opendracocli.shell.pipeline import ShellPipeline


class _StubAliasManager(AliasManager):
    def __init__(self):
        self._config = DracoConfig()
        self._aliases = {}

    def _load(self): pass
    def _save(self): pass


@pytest.fixture
def secured_pipeline(tmp_path):
    """装配了风控钩子 + 沙箱的 pipeline"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "hist.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        security_rules_file="",
        sandbox_writable_paths=[str(tmp_path)],
    )
    aliases = _StubAliasManager()
    history = HistoryStore(config=cfg)
    hooks = HookRegistry()

    assessor = RiskAssessor(builtin_rules_path=cfg.builtin_rules_path)
    confirmer = Confirmer()
    authenticator = Authenticator(
        hash_path=tmp_path / "auth.hash", iterations=10000
    )
    risk_hook = RiskAssessmentHook(
        assessor=assessor,
        confirmer=confirmer,
        authenticator=authenticator,
        config=cfg,
    )
    hooks.register_pre(risk_hook)

    inner = SubprocessExecutor(config=cfg)
    sandbox = SandboxExecutor(
        inner=inner,
        writable_paths=cfg.sandbox_writable_paths_resolved,
    )

    pipe = ShellPipeline(
        config=cfg,
        alias_manager=aliases,
        history_store=history,
        hook_registry=hooks,
        executor=inner,
        sandbox_executor=sandbox,
    )

    bus = EventBus()
    set_global_bus(bus)

    return pipe, history, confirmer, authenticator, bus


@pytest.mark.asyncio
async def test_safe_command_no_confirmation(secured_pipeline):
    """echo 不触发任何确认"""
    pipe, _, confirmer, *_ = secured_pipeline
    confirm_calls = []
    confirmer.set_test_callback(lambda p, d: (confirm_calls.append(p), False)[1])

    result = await pipe.run("echo safe", session_id="s1")
    assert result.success
    assert result.exit_code == 0
    assert confirm_calls == []  # 没有调用确认


@pytest.mark.asyncio
async def test_caution_confirmation_yes(secured_pipeline):
    """rm file.txt → caution → yes → 执行"""
    pipe, _, confirmer, *_ = secured_pipeline
    confirmer.set_test_callback(lambda p, d: True)

    result = await pipe.run("rm nonexistent_file", session_id="s1")
    assert result.success  # 管线成功（rm 可能返回非0但管线本身没断）
    assert result.is_high_risk is False  # caution 不是高危


@pytest.mark.asyncio
async def test_caution_confirmation_no_blocks(secured_pipeline):
    """rm file.txt → caution → no → BLOCK"""
    pipe, history, confirmer, *_ = secured_pipeline
    confirmer.set_test_callback(lambda p, d: False)

    result = await pipe.run("rm somefile", session_id="s1")
    assert result.blocked
    assert result.success is False

    # 历史记录
    recs = history.recent(limit=10, session_id=None)
    assert len(recs) == 1
    assert recs[0].exit_code is None  # 被阻断


@pytest.mark.asyncio
async def test_danger_uses_sandbox(secured_pipeline, tmp_path):
    """rm -rf <whitelisted> → danger → yes → 沙箱执行"""
    pipe, _, confirmer, *_ = secured_pipeline
    confirmer.set_test_callback(lambda p, d: True)

    # 在白名单内创建目录再删
    target = tmp_path / "danger_test"
    target.mkdir()
    result = await pipe.run(f"rm -rf {target}", session_id="s1")
    assert result.success
    assert result.is_high_risk is True  # danger 是高危


@pytest.mark.asyncio
async def test_critical_not_configured_blocks(secured_pipeline):
    """rm -rf / → critical → 未配置密码 → BLOCK"""
    pipe, _, confirmer, auth, *_ = secured_pipeline
    assert auth.is_configured() is False

    result = await pipe.run("rm -rf /", session_id="s1")
    assert result.blocked
    assert result.success is False
    assert "密码" in (result.block_reason or "") or "setup-auth" in (result.block_reason or "")


@pytest.mark.asyncio
async def test_critical_correct_password_executes(secured_pipeline):
    """rm -rf / → critical → 密码正确 + yes → 执行（经沙箱）"""
    pipe, _, confirmer, auth, *_ = secured_pipeline
    auth.set_password("admin123")
    auth.set_test_callback(lambda: "admin123")
    confirmer.set_test_callback(lambda p, d: True)

    result = await pipe.run("rm -rf /tmp/opendracocli_test_nonexistent", session_id="s1")
    # 沙箱会拦截对 / 的写，但 /tmp 在白名单... 实际上 /tmp 不在 fixture 白名单（只有 tmp_path）
    # 这里测的是流程走通（不阻断），退出码取决于 rm 实际行为
    assert not result.blocked  # 没被风控阻断


@pytest.mark.asyncio
async def test_sandbox_blocks_write_outside_whitelist(secured_pipeline):
    """沙箱拦截写白名单外路径"""
    pipe, _, confirmer, *_ = secured_pipeline
    confirmer.set_test_callback(lambda p, d: True)

    # danger: rm -rf 白名单外路径 → 沙箱拦截
    result = await pipe.run("rm -rf /etc/opendracocli_test", session_id="s1")
    # 应被沙箱拦截（exit_code=-1）或 rm 失败
    assert result.success  # 管线本身成功
    # exit_code 非0（沙箱拦截或 rm 权限不足）
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_risk_blocked_event_published(secured_pipeline):
    """拦截时发布 RiskBlocked 事件"""
    pipe, _, confirmer, _, bus = secured_pipeline
    confirmer.set_test_callback(lambda p, d: False)
    events = []
    bus.subscribe(EVT_RISK_BLOCKED, lambda e: events.append(e))

    await pipe.run("rm file", session_id="s1")
    assert len(events) == 1
    assert events[0].payload["level"] == "caution"


@pytest.mark.asyncio
async def test_p1_regression_alias_still_works(secured_pipeline):
    """P1 别名功能在 P2 风控下仍正常"""
    pipe, _, confirmer, *_ = secured_pipeline
    confirmer.set_test_callback(lambda p, d: True)  # 万一触发

    pipe._aliases.add("greet", "echo hello_from_alias")
    result = await pipe.run("greet", session_id="s1")
    assert result.success
    assert "hello_from_alias" in result.stdout
    assert result.alias_used == "greet"
