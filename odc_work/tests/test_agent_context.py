"""agent/context 测试"""
import asyncio
import pytest

from opendracocli.agent.context import AgentContext
from opendracocli.config import DracoConfig


class FakeResult:
    """模拟 PipelineResult"""

    def __init__(self, success=True, exit_code=0, stdout="out", stderr="", mapped="", blocked=False):
        self.success = success
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.mapped_command = mapped
        self.blocked = blocked
        self.block_reason = ""


@pytest.mark.asyncio
async def test_shell_calls_runner():
    calls = []

    async def runner(cmd):
        calls.append(cmd)
        return FakeResult(stdout=f"ran:{cmd}")

    ctx = AgentContext(shell_runner=runner)
    result = await ctx.shell("ls")
    assert calls == ["ls"]
    assert result["success"] is True
    assert result["stdout"] == "ran:ls"
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_shell_returns_blocked():
    async def runner(cmd):
        return FakeResult(success=False, blocked=True, stderr="风控拦截")

    ctx = AgentContext(shell_runner=runner)
    result = await ctx.shell("rm -rf /")
    assert result["blocked"] is True
    assert result["success"] is False


@pytest.mark.asyncio
async def test_shell_failure_propagates():
    async def runner(cmd):
        return FakeResult(success=False, exit_code=1, stderr="err")

    ctx = AgentContext(shell_runner=runner)
    result = await ctx.shell("false")
    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["stderr"] == "err"


def test_env_snapshot():
    ctx = AgentContext(shell_runner=lambda c: None, env={"PATH": "/usr/bin"})
    assert ctx.env == {"PATH": "/usr/bin"}
    # 修改不影响外部
    ctx.env["NEW"] = "1"
    import os
    assert "NEW" not in os.environ


def test_cwd_default():
    ctx = AgentContext(shell_runner=lambda c: None)
    assert ctx.cwd  # 非空（os.getcwd()）


def test_cwd_custom():
    ctx = AgentContext(shell_runner=lambda c: None, cwd="/tmp/test")
    assert ctx.cwd == "/tmp/test"


def test_config_access():
    cfg = DracoConfig()
    ctx = AgentContext(shell_runner=lambda c: None, config=cfg)
    assert ctx.config is cfg


def test_log_uses_sink():
    logs = []
    ctx = AgentContext(shell_runner=lambda c: None, log_sink=lambda m: logs.append(m))
    ctx.log("hello")
    ctx.log(42)
    assert logs == ["hello", "42"]


def test_log_no_sink_does_not_crash():
    ctx = AgentContext(shell_runner=lambda c: None)
    ctx.log("should not crash")  # 无 log_sink


def test_print_writes_stdout(capsys):
    ctx = AgentContext(shell_runner=lambda c: None)
    ctx.print("output line")
    captured = capsys.readouterr()
    assert "output line" in captured.out


def test_print_adds_newline(capsys):
    ctx = AgentContext(shell_runner=lambda c: None)
    ctx.print("no newline")
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")


def test_session_id():
    ctx = AgentContext(shell_runner=lambda c: None, session_id="sess-123")
    assert ctx.session_id == "sess-123"
