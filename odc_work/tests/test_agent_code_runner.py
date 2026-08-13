"""agent/code_runner 测试"""
import pytest

from opendracocli.agent.code_runner import CodeRunner
from opendracocli.errors import DracoError


@pytest.fixture
def runner():
    return CodeRunner()


def test_check_safety_clean_code(runner):
    violations = runner.check_safety("def f(ctx):\n    return 1", strict=True)
    assert violations == []


def test_check_safety_subprocess_blocked(runner):
    code = "import subprocess\ndef f(ctx):\n    pass"
    violations = runner.check_safety(code, strict=True)
    assert any("subprocess" in v for v in violations)


def test_check_safety_ctypes_blocked(runner):
    code = "import ctypes\ndef f(ctx):\n    pass"
    violations = runner.check_safety(code, strict=True)
    assert any("ctypes" in v for v in violations)


def test_check_safety_eval_blocked(runner):
    code = "def f(ctx):\n    eval('1+1')"
    violations = runner.check_safety(code, strict=True)
    assert any("eval" in v for v in violations)


def test_check_safety_os_system_blocked(runner):
    code = "import os\ndef f(ctx):\n    os.system('ls')"
    violations = runner.check_safety(code, strict=True)
    assert any("os.system" in v for v in violations)


def test_check_safety_dunder_blocked_non_strict(runner):
    """dunder 访问在非 strict 也拦截（防类型逃逸）"""
    code = "def f(ctx):\n    x = (1).__class__"
    violations = runner.check_safety(code, strict=False)
    assert any("__class__" in v for v in violations)


def test_check_safety_subprocess_allowed_non_strict(runner):
    """非 strict（用户函数）允许 import subprocess"""
    code = "import subprocess\ndef f(ctx):\n    pass"
    violations = runner.check_safety(code, strict=False)
    # 用户函数不检查 import，仅 dunder
    assert all("subprocess" not in v for v in violations)


def test_check_safety_syntax_error():
    runner = CodeRunner()
    violations = runner.check_safety("def f(ctx:\n    pass", strict=True)
    assert any("语法错误" in v for v in violations)


def test_exec_function_code_basic(runner):
    code = "def greet(ctx, name='world'):\n    return f'hello {name}'"
    func, name, violations = runner.exec_function_code(code, strict=False)
    assert name == "greet"
    assert violations == []
    assert callable(func)
    assert func(None, name="draco") == "hello draco"


def test_exec_function_code_async(runner):
    code = "async def task(ctx):\n    return 42"
    func, name, _ = runner.exec_function_code(code, strict=False)
    assert name == "task"
    import inspect
    assert inspect.iscoroutinefunction(func)


def test_exec_function_code_strict_blocks_subprocess(runner):
    code = "import subprocess\ndef f(ctx):\n    subprocess.run(['ls'])"
    with pytest.raises(DracoError) as exc:
        runner.exec_function_code(code, strict=True)
    assert "sandbox" in exc.value.code or "draco.agent.sandbox" in exc.value.code


def test_exec_function_code_syntax_error(runner):
    code = "def f(ctx:\n    pass"
    with pytest.raises(DracoError) as exc:
        runner.exec_function_code(code, strict=False)
    assert "draco.agent.syntax" in exc.value.code


def test_exec_function_code_with_namespace(runner):
    """注入 helper 到命名空间"""
    code = "def f(ctx):\n    return helper()"
    func, name, _ = runner.exec_function_code(
        code, namespace={"helper": lambda: "injected"}, strict=False
    )
    assert func(None) == "injected"


def test_extract_functions(runner):
    code = """
def f(ctx):
    pass

async def g(ctx):
    pass

def _private():
    pass
"""
    names = runner.extract_functions(code)
    assert "f" in names
    assert "g" in names
    assert "_private" in names


def test_check_safety_from_import_blocked(runner):
    code = "from subprocess import run\ndef f(ctx):\n    pass"
    violations = runner.check_safety(code, strict=True)
    assert any("subprocess" in v for v in violations)


def test_check_safety_shutil_blocked(runner):
    code = "import shutil\ndef f(ctx):\n    pass"
    violations = runner.check_safety(code, strict=True)
    assert any("shutil" in v for v in violations)
