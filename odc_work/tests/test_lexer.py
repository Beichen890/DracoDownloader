"""lexer 测试"""
from opendracocli.shell.lexer import tokenize


def test_simple_command():
    assert tokenize("ls -la") == ["ls", "-la"]


def test_empty_input():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_quoted_args():
    # 引号内空格不切分
    assert tokenize('echo "hello world"') == ["echo", "hello world"]
    assert tokenize("echo 'a b c'") == ["echo", "a b c"]


def test_pipe_operator():
    assert tokenize("ls | grep foo") == ["ls", "|", "grep", "foo"]


def test_and_operator():
    assert tokenize("a && b") == ["a", "&&", "b"]


def test_or_operator():
    assert tokenize("a || b") == ["a", "||", "b"]


def test_redirect_stdout():
    assert tokenize("ls > out.txt") == ["ls", ">", "out.txt"]


def test_redirect_append():
    assert tokenize("echo hi >> log.txt") == ["echo", "hi", ">>", "log.txt"]


def test_redirect_stdin():
    assert tokenize("cat < in.txt") == ["cat", "<", "in.txt"]


def test_redirect_stderr():
    assert tokenize("cmd 2> err.txt") == ["cmd", "2>", "err.txt"]


def test_pipe_inside_quotes_not_split():
    # 引号内的 | 不当操作符
    assert tokenize('echo "a|b"') == ["echo", "a|b"]


def test_multiple_operators():
    tokens = tokenize("a && b | c > f")
    assert tokens == ["a", "&&", "b", "|", "c", ">", "f"]


def test_unclosed_quote_raises():
    from opendracocli.errors import DracoError, ERR_SHELL_PARSE_ERROR

    try:
        tokenize('echo "unclosed')
        assert False, "should raise"
    except DracoError as e:
        assert e.code == ERR_SHELL_PARSE_ERROR
