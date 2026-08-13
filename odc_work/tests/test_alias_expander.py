"""alias_expander 测试"""
import pytest

from opendracocli.aliases.manager import AliasManager
from opendracocli.config import DracoConfig
from opendracocli.errors import DracoError, ERR_ALIAS_RECURSIVE
from opendracocli.shell.alias_expander import AliasExpander
from opendracocli.shell.ir import OP_AND
from opendracocli.shell.parser import parse


class _FakeManager(AliasManager):
    """绕过文件 IO 的测试用 manager"""

    def __init__(self, aliases: dict[str, str]):
        # 跳过父类 __init__ 的文件加载
        self._config = DracoConfig()
        self._aliases = dict(aliases)

    def _load(self) -> None:
        pass

    def _save(self) -> None:
        pass


def test_no_alias_passthrough():
    mgr = _FakeManager({})
    expander = AliasExpander(manager=mgr)

    ir = parse("ls -la")
    out, alias = expander.expand(ir)
    assert len(out.nodes) == 1
    assert out.nodes[0].name == "ls"
    assert alias is None


def test_simple_alias_expand():
    mgr = _FakeManager({"ll": "ls -la"})
    expander = AliasExpander(manager=mgr)

    ir = parse("ll")
    out, alias = expander.expand(ir)
    assert alias == "ll"
    assert len(out.nodes) == 1
    assert out.nodes[0].name == "ls"
    assert out.nodes[0].args == ["-la"]


def test_alias_with_extra_args():
    """别名 + 额外参数：ll /tmp → ls -la /tmp"""
    mgr = _FakeManager({"ll": "ls -la"})
    expander = AliasExpander(manager=mgr)

    ir = parse("ll /tmp")
    out, alias = expander.expand(ir)
    assert out.nodes[0].name == "ls"
    assert out.nodes[0].args == ["-la", "/tmp"]


def test_multi_command_alias_preserves_operators():
    """mydeploy = git add . && git commit -m x && git push"""
    mgr = _FakeManager({"mydeploy": "git add . && git commit -m x && git push"})
    expander = AliasExpander(manager=mgr)

    ir = parse("mydeploy")
    out, alias = expander.expand(ir)
    assert alias == "mydeploy"
    assert len(out.nodes) == 3
    assert out.nodes[0].name == "git"
    assert out.nodes[0].args == ["add", "."]
    assert out.nodes[1].name == "git"
    assert out.nodes[1].args == ["commit", "-m", "x"]
    assert out.nodes[2].name == "git"
    assert out.nodes[2].args == ["push"]
    # 操作符保留
    assert out.operators == [OP_AND, OP_AND]


def test_recursive_alias():
    """a → b → c"""
    mgr = _FakeManager({"a": "b", "b": "c", "c": "ls"})
    expander = AliasExpander(manager=mgr)

    ir = parse("a")
    out, alias = expander.expand(ir)
    assert out.nodes[0].name == "ls"


def test_recursive_depth_limit():
    """深度超过 max_alias_depth 抛错"""
    mgr = _FakeManager({"a": "b", "b": "c", "c": "d", "d": "e", "e": "ls"})
    cfg = DracoConfig()
    cfg.max_alias_depth = 3
    expander = AliasExpander(manager=mgr, config=cfg)

    ir = parse("a")
    with pytest.raises(DracoError) as exc:
        expander.expand(ir)
    assert exc.value.code == ERR_ALIAS_RECURSIVE


def test_circular_alias_detected():
    """a → b → a 环"""
    mgr = _FakeManager({"a": "b", "b": "a"})
    expander = AliasExpander(manager=mgr)

    ir = parse("a")
    with pytest.raises(DracoError) as exc:
        expander.expand(ir)
    assert exc.value.code == ERR_ALIAS_RECURSIVE


def test_alias_in_pipeline():
    """ll | grep foo"""
    mgr = _FakeManager({"ll": "ls -la"})
    expander = AliasExpander(manager=mgr)

    ir = parse("ll | grep foo")
    out, alias = expander.expand(ir)
    assert alias == "ll"
    assert len(out.nodes) == 2
    assert out.nodes[0].name == "ls"
    assert out.nodes[0].args == ["-la"]
    assert out.nodes[1].name == "grep"


def test_alias_expands_to_abstract_command():
    """验证别名展开产出命令名 ls（透传原生 shell，不做平台映射）。
    展开后的 name 应该是 ls，由原生 shell 执行。"""
    mgr = _FakeManager({"ll": "ls -la"})
    expander = AliasExpander(manager=mgr)

    ir = parse("ll")
    out, _ = expander.expand(ir)
    # 关键断言：展开后是 ls（透传原生 shell）
    assert out.nodes[0].name == "ls"
