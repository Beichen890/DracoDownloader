"""parser 测试"""
import pytest

from opendracocli.shell.ir import OP_AND, OP_OR, OP_PIPE, OP_SEQ
from opendracocli.shell.parser import parse


def test_single_command():
    ir = parse("ls -la")
    assert len(ir.nodes) == 1
    assert ir.nodes[0].name == "ls"
    assert ir.nodes[0].args == ["-la"]
    assert ir.operators == []
    assert ir.raw_input == "ls -la"


def test_pipe():
    ir = parse("ls | grep foo")
    assert len(ir.nodes) == 2
    assert ir.nodes[0].name == "ls"
    assert ir.nodes[1].name == "grep"
    assert ir.operators == [OP_PIPE]


def test_and():
    ir = parse("a && b")
    assert ir.operators == [OP_AND]


def test_or():
    ir = parse("a || b")
    assert ir.operators == [OP_OR]


def test_seq():
    ir = parse("a ; b")
    assert ir.operators == [OP_SEQ]


def test_redirect_stdout():
    ir = parse("ls > out.txt")
    assert len(ir.redirects) == 1
    assert ir.redirects[0].kind == "stdout"
    assert ir.redirects[0].target == "out.txt"


def test_redirect_append():
    ir = parse("echo hi >> log.txt")
    assert ir.redirects[0].kind == "append"


def test_redirect_stdin():
    ir = parse("cat < in.txt")
    assert ir.redirects[0].kind == "stdin"


def test_empty_input():
    ir = parse("")
    assert ir.nodes == []


def test_operator_without_command_raises():
    from opendracocli.errors import DracoError, ERR_SHELL_PARSE_ERROR

    with pytest.raises(DracoError) as exc:
        parse("&& b")
    assert exc.value.code == ERR_SHELL_PARSE_ERROR


def test_redirect_without_target_raises():
    from opendracocli.errors import DracoError, ERR_SHELL_PARSE_ERROR

    with pytest.raises(DracoError) as exc:
        parse("ls >")
    assert exc.value.code == ERR_SHELL_PARSE_ERROR


def test_three_commands_chain():
    ir = parse("a && b | c")
    assert len(ir.nodes) == 3
    assert ir.operators == [OP_AND, OP_PIPE]
