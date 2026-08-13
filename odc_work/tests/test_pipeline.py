"""pipeline 端到端测试"""
import pytest

from opendracocli.aliases.manager import AliasManager
from opendracocli.config import DracoConfig
from opendracocli.events import EVT_COMMAND_EXECUTED, EventBus
from opendracocli.hooks.base import (
    BlockResult,
    ExecContext,
    ExecDecision,
    PostExecHook,
    PreExecHook,
)
from opendracocli.hooks.registry import HookRegistry
from opendracocli.history.store import HistoryStore
from opendracocli.shell.pipeline import ShellPipeline


class _StubAliasManager(AliasManager):
    """绕过文件 IO"""

    def __init__(self, aliases: dict[str, str]):
        self._config = DracoConfig()
        self._aliases = dict(aliases)

    def _load(self) -> None:
        pass

    def _save(self) -> None:
        pass


@pytest.fixture
def pipeline(tmp_path):
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "hist.db"),
        aliases_file=str(tmp_path / "aliases.json"),
    )
    aliases = _StubAliasManager({})
    history = HistoryStore(config=cfg)
    hooks = HookRegistry()
    pipe = ShellPipeline(
        config=cfg,
        alias_manager=aliases,
        history_store=history,
        hook_registry=hooks,
    )
    return pipe, history, aliases, hooks, cfg


@pytest.mark.asyncio
async def test_simple_command_executes(pipeline):
    pipe, history, *_ = pipeline
    result = await pipe.run("echo hello", session_id="s1")
    assert result.success
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_command_records_history(pipeline):
    pipe, history, *_ = pipeline
    await pipe.run("echo hi", session_id="s1")
    recs = history.recent(limit=10, session_id=None)
    assert len(recs) == 1
    assert recs[0].raw_input == "echo hi"
    assert recs[0].exit_code == 0
    assert recs[0].session_id == "s1"


@pytest.mark.asyncio
async def test_alias_then_native_shell(pipeline):
    """别名 ll=ls -la，展开后透传原生 shell 执行"""
    pipe, history, aliases, *_ = pipeline
    aliases.add("ll", "ls -la")

    result = await pipe.run("ll", session_id="s1")
    assert result.success
    assert result.alias_used == "ll"
    # mapped_command 应含 ls（透传原生 shell，不映射）
    assert "ls" in result.mapped_command


@pytest.mark.asyncio
async def test_failed_parse_records_history(pipeline):
    """解析失败也记录历史（exit_code=None）"""
    pipe, history, *_ = pipeline
    result = await pipe.run("&&&", session_id="s1")
    assert not result.success
    assert result.error is not None
    recs = history.recent(limit=10, session_id=None)
    assert len(recs) == 1
    assert recs[0].exit_code is None


@pytest.mark.asyncio
async def test_nonzero_exit_not_error(pipeline):
    """非零退出码是正常结果，不是 DracoError"""
    pipe, *_ = pipeline
    result = await pipe.run("this_cmd_does_not_exist_xyz", session_id="s1")
    # 非零退出码：success=True（管线本身成功跑完），exit_code 非 0
    assert result.success
    assert result.exit_code != 0
    assert result.error is None


@pytest.mark.asyncio
async def test_event_published(pipeline):
    pipe, history, aliases, hooks, cfg = pipeline
    bus = EventBus()
    from opendracocli.events import set_global_bus

    set_global_bus(bus)

    received = []
    bus.subscribe(EVT_COMMAND_EXECUTED, lambda e: received.append(e))
    await pipe.run("echo test", session_id="s1")
    assert len(received) == 1
    assert received[0].payload["exit_code"] == 0


@pytest.mark.asyncio
async def test_pre_exec_hook_blocks(pipeline):
    """PreExec 钩子可以阻断执行"""
    pipe, history, aliases, hooks, cfg = pipeline

    class _BlockHook(PreExecHook):
        priority = 10

        async def pre_exec(self, ctx: ExecContext):
            if "danger" in ctx.raw_input:
                return ExecDecision.BLOCK, BlockResult(reason="危险命令", code="test.block")
            return ExecDecision.CONTINUE, None

    hooks.register_pre(_BlockHook())

    result = await pipe.run("echo danger", session_id="s1")
    assert result.blocked
    assert result.success is False
    assert result.block_reason == "危险命令"
    # 阻断也记录历史
    recs = history.recent(limit=10, session_id=None)
    assert len(recs) == 1
    assert recs[0].exit_code is None


@pytest.mark.asyncio
async def test_pre_exec_hook_can_rewrite_ir(pipeline):
    """P3 场景：PreExec 钩子改写 ctx.ir（命令角色化）"""
    pipe, history, aliases, hooks, cfg = pipeline

    class _RewriteHook(PreExecHook):
        priority = 10

        async def pre_exec(self, ctx: ExecContext):
            # 把第一个命令名改成 echo
            if ctx.ir.nodes:
                ctx.ir.nodes[0].name = "echo"
                ctx.ir.nodes[0].args = ["rewritten"]
            return ExecDecision.CONTINUE, None

    hooks.register_pre(_RewriteHook())

    result = await pipe.run("anything_at_all", session_id="s1")
    assert result.success
    assert "rewritten" in result.stdout


@pytest.mark.asyncio
async def test_post_exec_hook_runs(pipeline):
    """PostExec 钩子能拿到结果"""
    pipe, history, aliases, hooks, cfg = pipeline

    captured = {}

    class _CaptureHook(PostExecHook):
        priority = 10

        async def post_exec(self, ctx: ExecContext, result):
            captured["exit_code"] = result.exit_code
            captured["stdout"] = result.stdout

    hooks.register_post(_CaptureHook())

    await pipe.run("echo captured", session_id="s1")
    assert captured.get("exit_code") == 0
    assert "captured" in captured.get("stdout", "")


@pytest.mark.asyncio
async def test_pipe_operator_passthrough(pipeline):
    """管道透传原生 shell"""
    pipe, *_ = pipeline
    result = await pipe.run("echo hello | cat", session_id="s1")
    assert result.success
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_and_operator_passthrough(pipeline):
    pipe, *_ = pipeline
    result = await pipe.run("echo first && echo second", session_id="s1")
    assert result.success
    assert "first" in result.stdout
    assert "second" in result.stdout


@pytest.mark.asyncio
async def test_redirect_passthrough(pipeline, tmp_path):
    pipe, *_ = pipeline
    out_file = tmp_path / "redirect_out.txt"
    result = await pipe.run(f"echo redirected > {out_file}", session_id="s1")
    assert result.success
    # 文件应被创建
    assert out_file.exists()
    assert "redirected" in out_file.read_text()


@pytest.mark.asyncio
async def test_multi_command_alias_with_operators(pipeline):
    """mydeploy = git 不可用，但管线应正确展开并执行"""
    pipe, history, aliases, *_ = pipeline
    aliases.add("ll", "echo a && echo b")

    result = await pipe.run("ll", session_id="s1")
    assert result.success
    assert result.alias_used == "ll"
    assert "a" in result.stdout
    assert "b" in result.stdout
    assert "&&" in result.mapped_command


# ===== Draco 函数路由测试 =====


@pytest.mark.asyncio
async def test_draco_func_routes_to_python(pipeline, tmp_path):
    """命中 Draco 函数走 Python 通道，不走原生 shell"""
    pipe, history, aliases, hooks, cfg = pipeline

    # 注册一个测试用的 Draco 函数
    from opendracocli.agent.function_registry import FunctionRegistry
    from opendracocli.agent.python_executor import PythonChannelExecutor

    registry = FunctionRegistry()

    def myecho(ctx, text: str = "default"):
        """测试函数"""
        print(f"ECHO:{text}")
        return text

    registry.register("myecho", myecho, builtin=True)
    py_exec = PythonChannelExecutor(config=cfg, history_store=history)
    pipe.set_function_registry(registry)
    pipe.set_python_executor(py_exec)

    result = await pipe.run("myecho text=hello", session_id="s1")
    assert result.success
    assert result.exit_code == 0
    assert "ECHO:hello" in result.stdout
    assert "myecho" in result.mapped_command


@pytest.mark.asyncio
async def test_draco_func_ffind(pipeline, tmp_path):
    """内置 ffind 函数端到端：跨平台文件搜索"""
    pipe, history, aliases, hooks, cfg = pipeline

    from opendracocli.draco_funcs.registry import register_builtins
    from opendracocli.agent.function_registry import FunctionRegistry
    from opendracocli.agent.python_executor import PythonChannelExecutor

    registry = FunctionRegistry()
    register_builtins(registry)
    py_exec = PythonChannelExecutor(config=cfg, history_store=history)
    pipe.set_function_registry(registry)
    pipe.set_python_executor(py_exec)

    # 创建测试文件
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("y")

    result = await pipe.run(f"ffind *.py path={tmp_path}", session_id="s1")
    assert result.success
    assert "a.py" in result.stdout
    assert "b.txt" not in result.stdout


@pytest.mark.asyncio
async def test_unmapped_command_passthrough(pipeline):
    """未命中 Draco 函数的命令透传原生 shell"""
    pipe, *_ = pipeline
    # 不注入 function_registry，或注入空 registry
    from opendracocli.agent.function_registry import FunctionRegistry

    registry = FunctionRegistry()  # 空 registry
    pipe.set_function_registry(registry)
    # 不注入 python_executor → 即使命中也走 shell（但空 registry 不会命中）

    result = await pipe.run("echo passthrough", session_id="s1")
    assert result.success
    assert "passthrough" in result.stdout
