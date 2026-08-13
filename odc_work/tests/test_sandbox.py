"""sandbox 测试"""
import os
from pathlib import Path

import pytest

from opendracocli.config import DracoConfig
from opendracocli.security.sandbox import SandboxExecutor
from opendracocli.shell.executor import SubprocessExecutor
from opendracocli.shell.ir import CommandIR, CommandNode, Redirect
from opendracocli.shell.parser import parse


@pytest.fixture
def sandbox(tmp_path):
    """白名单包含 tmp_path 的沙箱"""
    inner = SubprocessExecutor(config=DracoConfig())
    return SandboxExecutor(
        inner=inner,
        writable_paths=[tmp_path],
    )


@pytest.mark.asyncio
async def test_write_to_whitelisted_path(sandbox, tmp_path):
    """写白名单内路径：放行"""
    out = tmp_path / "out.txt"
    ir = CommandIR(
        nodes=[CommandNode(name="echo", args=["hi"])],
        redirects=[Redirect(kind="stdout", target=str(out))],
    )
    result = await sandbox.execute(ir, timeout=10)
    assert result.exit_code == 0
    assert out.exists()


@pytest.mark.asyncio
async def test_write_to_non_whitelisted_path(sandbox):
    """写白名单外路径（/etc）：拦截"""
    ir = CommandIR(
        nodes=[CommandNode(name="echo", args=["hi"])],
        redirects=[Redirect(kind="stdout", target="/etc/should_not_exist.txt")],
    )
    result = await sandbox.execute(ir, timeout=10)
    assert result.exit_code == -1
    assert "sandbox.violation" in result.stderr or "draco.sandbox" in result.stderr


@pytest.mark.asyncio
async def test_append_redirect_checked(sandbox, tmp_path):
    """追加重定向也检查"""
    out = tmp_path / "log.txt"
    ir = CommandIR(
        nodes=[CommandNode(name="echo", args=["line"])],
        redirects=[Redirect(kind="append", target=str(out))],
    )
    result = await sandbox.execute(ir, timeout=10)
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_stderr_redirect_checked(sandbox):
    """stderr 重定向也检查"""
    ir = CommandIR(
        nodes=[CommandNode(name="echo", args=["err"])],
        redirects=[Redirect(kind="stderr", target="/etc/bad.txt")],
    )
    result = await sandbox.execute(ir, timeout=10)
    assert result.exit_code == -1


@pytest.mark.asyncio
async def test_rm_path_argument_checked(sandbox, tmp_path):
    """rm 的路径参数检查：白名单内放行"""
    target = tmp_path / "deleteme.txt"
    target.write_text("x")
    ir = CommandIR(nodes=[CommandNode(name="rm", args=[str(target)])])
    result = await sandbox.execute(ir, timeout=10)
    # rm 可能不存在或退出码非0，但不应被沙箱拦截
    assert "sandbox.violation" not in result.stderr


@pytest.mark.asyncio
async def test_rm_non_whitelisted_path_blocked(sandbox):
    """rm 白名单外路径：拦截"""
    ir = CommandIR(nodes=[CommandNode(name="rm", args=["/etc/important.conf"])])
    result = await sandbox.execute(ir, timeout=10)
    assert result.exit_code == -1
    assert "draco.sandbox" in result.stderr


@pytest.mark.asyncio
async def test_safe_command_not_blocked(sandbox, tmp_path):
    """echo 无重定向：不触发路径检查，正常执行"""
    ir = CommandIR(nodes=[CommandNode(name="echo", args=["hello"])])
    result = await sandbox.execute(ir, cwd=str(tmp_path), timeout=10)
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_home_expansion(tmp_path):
    """~ 展开"""
    inner = SubprocessExecutor(config=DracoConfig())
    sandbox = SandboxExecutor(
        inner=inner,
        writable_paths=[Path(os.path.expanduser("~"))],
    )
    # 写到 ~/ 下应该放行
    ir = CommandIR(
        nodes=[CommandNode(name="echo", args=["h"])],
        redirects=[Redirect(kind="stdout", target="~/sandbox_test_file.tmp")],
    )
    result = await sandbox.execute(ir, timeout=10)
    # 清理
    f = Path(os.path.expanduser("~/sandbox_test_file.tmp"))
    if f.exists():
        f.unlink()
    assert result.exit_code == 0
