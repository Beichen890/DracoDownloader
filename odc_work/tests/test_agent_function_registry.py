"""agent/function_registry 测试"""
import pytest

from opendracocli.agent.code_runner import CodeRunner
from opendracocli.agent.function_registry import (
    FunctionRegistry,
    RegisteredFunction,
)


@pytest.fixture
def registry():
    return FunctionRegistry(runner=CodeRunner())


def _sample_functions_py(tmp_path):
    """生成示例 functions.py"""
    code = '''
def greet(ctx, name="world"):
    """问候"""
    return f"hello {name}"


async def deploy(ctx):
    """部署"""
    r = await ctx.shell("git pull")
    return r


def _private(ctx):
    """应被跳过（下划线开头）"""
    return "private"


class NotAFunction:
    """应被跳过（非函数）"""
    pass


CONST = 42  # 应被跳过（非 callable）
'''
    path = tmp_path / "functions.py"
    path.write_text(code, encoding="utf-8")
    return path


def test_register_and_get(registry):
    def f(ctx):
        return 1

    registry.register("f", f)
    rf = registry.get("f")
    assert rf is not None
    assert rf.name == "f"
    assert callable(rf.func)


def test_match(registry):
    def greet(ctx):
        return "hi"

    registry.register("greet", greet)
    assert registry.match("greet") is not None
    assert registry.match("nonexistent") is None
    assert registry.match("") is None


def test_param_names_no_ctx(registry):
    def f(ctx, a, b=2):
        return a + b

    registry.register("f", f)
    rf = registry.get("f")
    assert rf.param_names_no_ctx == ["a", "b"]


def test_param_types(registry):
    def f(ctx, a: int, b: str = "x"):
        return a

    registry.register("f", f)
    rf = registry.get("f")
    assert rf.param_types == {"a": int, "b": str}


def test_defaults(registry):
    def f(ctx, a, b=2, c="x"):
        return a

    registry.register("f", f)
    rf = registry.get("f")
    assert rf.defaults == {"b": 2, "c": "x"}


def test_is_async_detection(registry):
    async def af(ctx):
        return 1

    def sf(ctx):
        return 1

    registry.register("af", af)
    registry.register("sf", sf)
    assert registry.get("af").is_async is True
    assert registry.get("sf").is_async is False


def test_load_user_file(registry, tmp_path):
    path = _sample_functions_py(tmp_path)
    n = registry.load_user_file(path)
    # greet + deploy = 2（_private 跳过，class 跳过，CONST 跳过）
    assert n == 2
    assert registry.get("greet") is not None
    assert registry.get("deploy") is not None
    assert registry.get("_private") is None


def test_load_user_file_nonexistent(registry, tmp_path):
    n = registry.load_user_file(tmp_path / "nonexistent.py")
    assert n == 0


def test_load_user_file_syntax_error(registry, tmp_path):
    path = tmp_path / "bad.py"
    path.write_text("def f(ctx:\n    pass", encoding="utf-8")
    from opendracocli.errors import DracoError

    with pytest.raises(DracoError):
        registry.load_user_file(path)


def test_load_user_file_skips_non_ctx_first_param(registry, tmp_path):
    """首参数不是 ctx 的函数应被跳过"""
    path = tmp_path / "f.py"
    path.write_text(
        "def no_ctx(a, b):\n    return a+b\n\ndef good(ctx):\n    return 1\n",
        encoding="utf-8",
    )
    n = registry.load_user_file(path)
    assert n == 1
    assert registry.get("good") is not None
    assert registry.get("no_ctx") is None


def test_reload(registry, tmp_path):
    path = _sample_functions_py(tmp_path)
    registry.load_user_file(path)
    assert registry.get("greet") is not None

    # 修改文件
    path.write_text(
        'def newfunc(ctx):\n    """new"""\n    return "new"\n', encoding="utf-8"
    )
    n = registry.reload()
    assert n == 1
    assert registry.get("newfunc") is not None
    # 旧函数被清空
    assert registry.get("greet") is None


def test_reload_without_file(registry):
    """未设置文件时 reload 返回 0"""
    assert registry.reload() == 0


def test_list(registry):
    def f1(ctx):
        pass

    def f2(ctx):
        pass

    registry.register("f1", f1, builtin=True)
    registry.register("f2", f2)
    all_funcs = registry.list()
    assert "f1" in all_funcs
    assert "f2" in all_funcs
    user_only = registry.list_user()
    assert "f1" not in user_only  # builtin
    assert "f2" in user_only


def test_doc_extraction(registry):
    def f(ctx):
        """这是文档"""
        return 1

    registry.register("f", f)
    assert registry.get("f").doc == "这是文档"


def test_source_extraction(registry):
    def f(ctx):
        return 1

    registry.register("f", f)
    assert "def f" in registry.get("f").source
