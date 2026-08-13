"""executor 测试"""
import asyncio

import pytest

from opendracocli.shell.executor import ExecResult, SubprocessExecutor, serialize_ir
from opendracocli.shell.ir import CommandIR, CommandNode


def test_serialize_single_command():
    ir = CommandIR(nodes=[CommandNode(name="ls", args=["-la"])])
    assert serialize_ir(ir) == "ls -la"


def test_serialize_with_operator():
    ir = CommandIR(
        nodes=[CommandNode(name="a"), CommandNode(name="b")],
        operators=["&&"],
    )
    assert serialize_ir(ir) == "a && b"


def test_serialize_with_redirect():
    ir = CommandIR(
        nodes=[CommandNode(name="ls")],
        redirects=[],
    )
    from opendracocli.shell.ir import Redirect

    ir.redirects = [Redirect(kind="stdout", target="out.txt")]
    assert serialize_ir(ir) == "ls > out.txt"


def test_serialize_empty():
    ir = CommandIR(nodes=[])
    assert serialize_ir(ir) == ""


def test_serialize_quotes_spaces():
    ir = CommandIR(nodes=[CommandNode(name="echo", args=["hello world"])])
    s = serialize_ir(ir)
    assert "hello world" in s
    # 含空格的参数应被引号包裹
    assert '"hello world"' in s


@pytest.mark.asyncio
async def test_executor_runs_echo():
    """实际执行 echo（跨平台兼容）"""
    executor = SubprocessExecutor()
    ir = CommandIR(nodes=[CommandNode(name="echo", args=["hello"])])
    result = await executor.execute(ir, timeout=10)
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_executor_nonzero_exit():
    """执行不存在的命令，非 0 退出码（不抛异常）"""
    executor = SubprocessExecutor()
    ir = CommandIR(nodes=[CommandNode(name="this_command_does_not_exist_xyz")])
    result = await executor.execute(ir, timeout=10)
    # 非零退出码是正常结果，不是异常
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_executor_timeout():
    """超时抛 DracoError"""
    from opendracocli.errors import DracoError, ERR_EXEC_TIMEOUT

    executor = SubprocessExecutor()
    # sleep 5 但超时 0.1
    ir = CommandIR(nodes=[CommandNode(name="sleep", args=["5"])])
    with pytest.raises(DracoError) as exc:
        await executor.execute(ir, timeout=0.1)
    assert exc.value.code == ERR_EXEC_TIMEOUT


@pytest.mark.asyncio
async def test_executor_records_duration():
    executor = SubprocessExecutor()
    ir = CommandIR(nodes=[CommandNode(name="echo", args=["hi"])])
    result = await executor.execute(ir, timeout=10)
    assert result.duration_ms >= 0
    assert result.mapped_command  # 非空


@pytest.mark.asyncio
async def test_executor_cancel_safe_terminate():
    """Ctrl+C 取消时走 _safe_terminate，不抛 'exit status already read' 警告"""
    from opendracocli.errors import DracoError, ERR_EXEC_CANCELLED

    executor = SubprocessExecutor()
    ir = CommandIR(nodes=[CommandNode(name="sleep", args=["5"])])

    task = asyncio.ensure_future(executor.execute(ir, timeout=30))
    await asyncio.sleep(0.3)  # 让子进程起来
    task.cancel()

    with pytest.raises(DracoError) as exc:
        await task
    assert exc.value.code == ERR_EXEC_CANCELLED


@pytest.mark.asyncio
async def test_safe_terminate_idempotent_on_dead_process():
    """_safe_terminate 对已退出进程不重复发信号"""
    from opendracocli.shell.executor import _safe_terminate

    executor = SubprocessExecutor()
    ir = CommandIR(nodes=[CommandNode(name="true")])
    # 先正常跑完，proc 已退出
    await executor.execute(ir, timeout=5)
    # 构造一个已退出的伪 proc 不可行，改用真实进程跑完后立即调用
    proc = await asyncio.create_subprocess_exec(
        "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    # 进程已退出 (returncode 已知)，_safe_terminate 不应抛异常
    await _safe_terminate(proc)
    assert proc.returncode == 0
