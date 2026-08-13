"""agent/python_executor 测试"""
import asyncio
import inspect
import pytest

from opendracocli.agent.function_registry import RegisteredFunction
from opendracocli.agent.python_executor import AgentExecResult, PythonChannelExecutor
from opendracocli.config import DracoConfig
from opendracocli.events import EVT_AGENT_EXECUTED, EVT_FUNCTION_CALLED, EventBus, set_global_bus


class FakeHistory:
    """模拟 HistoryStore"""

    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def _make_reg_func(func, name="f"):
    sig = inspect.signature(func)
    return RegisteredFunction(
        name=name,
        func=func,
        signature=sig,
        doc=inspect.getdoc(func) or "",
        source=inspect.getsource(func),
        is_async=inspect.iscoroutinefunction(func),
    )


@pytest.fixture
def setup(tmp_path):
    cfg = DracoConfig(history_db_path=str(tmp_path / "h.db"))
    history = FakeHistory()
    bus = EventBus()
    set_global_bus(bus)

    async def shell_runner(cmd):
        class R:
            success = True
            exit_code = 0
            stdout = f"shell:{cmd}"
            stderr = ""
            mapped_command = cmd
            blocked = False
            block_reason = ""

        return R()

    executor = PythonChannelExecutor(
        config=cfg, history_store=history, shell_runner=shell_runner
    )
    return executor, history, bus


@pytest.mark.asyncio
async def test_execute_sync_function(setup):
    executor, history, bus = setup

    def f(ctx):
        ctx.print("hello")
        return {"status": "ok"}

    rf = _make_reg_func(f)
    result = await executor.execute(rf, [], {}, session_id="s1")

    assert result.success is True
    assert result.exit_code == 0
    assert result.func_name == "f"
    assert "hello" in result.stdout
    assert result.return_value == {"status": "ok"}


@pytest.mark.asyncio
async def test_execute_async_function(setup):
    executor, history, bus = setup

    async def f(ctx):
        r = await ctx.shell("ls")
        return {"shell_result": r["stdout"]}

    rf = _make_reg_func(f, name="af")
    result = await executor.execute(rf, [], {}, session_id="s1")

    assert result.success is True
    assert result.return_value == {"shell_result": "shell:ls"}


@pytest.mark.asyncio
async def test_execute_function_exception(setup):
    executor, history, bus = setup

    def f(ctx):
        raise ValueError("boom")

    rf = _make_reg_func(f, name="boom")
    result = await executor.execute(rf, [], {}, session_id="s1")

    assert result.success is False
    assert result.exit_code == 1
    assert "ValueError" in result.stderr
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_execute_writes_history(setup):
    executor, history, bus = setup

    def f(ctx):
        return 1

    rf = _make_reg_func(f)
    await executor.execute(rf, [], {}, session_id="s1", cwd="/tmp")
    assert len(history.records) == 1
    rec = history.records[0]
    assert rec["session_id"] == "s1"
    assert rec["exit_code"] == 0
    assert rec["cwd"] == "/tmp"
    assert "f()" in rec["raw_input"]


@pytest.mark.asyncio
async def test_execute_publishes_events(setup):
    executor, history, bus = setup
    func_events = []
    agent_events = []
    bus.subscribe(EVT_FUNCTION_CALLED, lambda e: func_events.append(e))
    bus.subscribe(EVT_AGENT_EXECUTED, lambda e: agent_events.append(e))

    def f(ctx):
        return 1

    rf = _make_reg_func(f)
    await executor.execute(rf, [], {}, session_id="s1")

    assert len(func_events) == 1
    assert func_events[0].payload["func_name"] == "f"
    assert len(agent_events) == 1
    assert agent_events[0].payload["success"] is True


@pytest.mark.asyncio
async def test_execute_with_kwargs(setup):
    executor, history, bus = setup

    def f(ctx, x, y=10):
        return x + y

    rf = _make_reg_func(f)
    result = await executor.execute(rf, [], {"x": 5, "y": 20}, session_id="s1")
    assert result.success is True
    assert result.return_value == 25


@pytest.mark.asyncio
async def test_execute_no_history_store(tmp_path):
    """history_store=None 时不写历史但不崩溃"""
    cfg = DracoConfig(history_db_path=str(tmp_path / "h.db"))
    bus = EventBus()
    set_global_bus(bus)

    async def runner(cmd):
        class R:
            success = True
            exit_code = 0
            stdout = ""
            stderr = ""
            mapped_command = ""
            blocked = False
            block_reason = ""

        return R()

    executor = PythonChannelExecutor(config=cfg, history_store=None, shell_runner=runner)

    def f(ctx):
        return 1

    rf = _make_reg_func(f)
    result = await executor.execute(rf, [], {})
    assert result.success is True


@pytest.mark.asyncio
async def test_execute_timeout(tmp_path):
    cfg = DracoConfig(history_db_path=str(tmp_path / "h.db"), exec_timeout=0.1)
    bus = EventBus()
    set_global_bus(bus)

    async def runner(cmd):
        class R:
            success = True
            exit_code = 0
            stdout = ""
            stderr = ""
            mapped_command = ""
            blocked = False
            block_reason = ""

        return R()

    executor = PythonChannelExecutor(config=cfg, history_store=None, shell_runner=runner)

    async def slow(ctx):
        await asyncio.sleep(5)
        return 1

    rf = _make_reg_func(slow, name="slow")
    result = await executor.execute(rf, [], {}, timeout=0.1)
    assert result.cancelled is True
    assert "超时" in result.error


@pytest.mark.asyncio
async def test_mapped_command_format(setup):
    executor, history, bus = setup

    def f(ctx, x, y=2):
        return x

    rf = _make_reg_func(f)
    result = await executor.execute(rf, [], {"x": 1, "y": 2})
    assert "f(" in result.mapped_command
    assert "x=1" in result.mapped_command
    assert "y=2" in result.mapped_command


@pytest.mark.asyncio
async def test_stdout_captured_and_visible(setup, capsys):
    """函数内 ctx.print 应同时捕获到 stdout 并对用户可见"""
    executor, history, bus = setup

    def f(ctx):
        ctx.print("visible output")
        return None

    rf = _make_reg_func(f)
    result = await executor.execute(rf, [], {})
    captured = capsys.readouterr()
    assert "visible output" in captured.out
    assert "visible output" in result.stdout
