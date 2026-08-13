"""agent/arg_parser 测试"""
import pytest

from opendracocli.agent.arg_parser import (
    ArgParseError,
    parse_call_args,
    split_kv,
    tokenize,
)


def test_tokenize_simple():
    assert tokenize("a b c") == ["a", "b", "c"]


def test_tokenize_quoted():
    assert tokenize('greet "hello world"') == ["greet", "hello world"]


def test_tokenize_empty():
    assert tokenize("") == []


def test_split_kv_positional_only():
    p = split_kv(["a", "b", "c"])
    assert p.args == ["a", "b", "c"]
    assert p.kwargs == {}


def test_split_kv_keyword_only():
    p = split_kv(["x=1", "y=2"])
    assert p.args == []
    assert p.kwargs == {"x": "1", "y": "2"}


def test_split_kv_mixed():
    p = split_kv(["a", "b=2"])
    assert p.args == ["a"]
    assert p.kwargs == {"b": "2"}


def test_split_kv_positional_after_keyword_fails():
    with pytest.raises(ArgParseError):
        split_kv(["x=1", "positional"])


def test_split_kv_value_with_equals():
    """值中含 = 不应被当作 keyword（key 必须是合法标识符）"""
    p = split_kv(["a=b=c"])
    assert p.kwargs == {"a": "b=c"}


def test_split_kv_not_identifier_key():
    """非标识符 key 不算 keyword"""
    p = split_kv(["1=a"])
    assert p.args == ["1=a"]
    assert p.kwargs == {}


def test_parse_call_args_empty():
    args, kwargs = parse_call_args("", ["days"], {"days": int}, {"days": 7})
    assert args == []
    assert kwargs == {"days": 7}


def test_parse_call_args_positional():
    args, kwargs = parse_call_args("3", ["days"], {"days": int}, {"days": 7})
    assert kwargs == {"days": 3}


def test_parse_call_args_keyword():
    args, kwargs = parse_call_args("days=3", ["days"], {"days": int}, {"days": 7})
    assert kwargs == {"days": 3}


def test_parse_call_args_type_conversion_int():
    args, kwargs = parse_call_args("n=42", ["n"], {"n": int}, {})
    assert kwargs == {"n": 42}
    assert isinstance(kwargs["n"], int)


def test_parse_call_args_type_conversion_float():
    args, kwargs = parse_call_args("x=3.14", ["x"], {"x": float}, {})
    assert kwargs["x"] == pytest.approx(3.14)
    assert isinstance(kwargs["x"], float)


def test_parse_call_args_type_conversion_bool():
    args, kwargs = parse_call_args("flag=true", ["flag"], {"flag": bool}, {})
    assert kwargs["flag"] is True

    args, kwargs = parse_call_args("flag=no", ["flag"], {"flag": bool}, {})
    assert kwargs["flag"] is False


def test_parse_call_args_int_conversion_fails():
    with pytest.raises(ArgParseError):
        parse_call_args("n=abc", ["n"], {"n": int}, {})


def test_parse_call_args_multiple_positional():
    args, kwargs = parse_call_args(
        "src dst", ["src", "dst"], {"src": str, "dst": str}, {}
    )
    assert kwargs == {"src": "src", "dst": "dst"}


def test_parse_call_args_mixed():
    args, kwargs = parse_call_args(
        "src dst=/tmp", ["src", "dst"], {"src": str, "dst": str}, {}
    )
    assert kwargs == {"src": "src", "dst": "/tmp"}


def test_parse_call_args_unknown_keyword():
    with pytest.raises(ArgParseError, match="未知关键字"):
        parse_call_args("foo=1", ["bar"], {"bar": str}, {})


def test_parse_call_args_too_many_positional():
    with pytest.raises(ArgParseError, match="位置参数过多"):
        parse_call_args("a b c", ["x", "y"], {"x": str, "y": str}, {})


def test_parse_call_args_missing_required():
    with pytest.raises(ArgParseError, match="缺少必填参数"):
        parse_call_args("", ["required"], {"required": str}, {})


def test_parse_call_args_duplicate_param():
    """位置 + 关键字重复指定同一参数"""
    with pytest.raises(ArgParseError, match="重复"):
        parse_call_args("1 x=2", ["x"], {"x": int}, {})


def test_parse_call_args_quoted_value():
    args, kwargs = parse_call_args(
        'name="hello world"', ["name"], {"name": str}, {}
    )
    assert kwargs == {"name": "hello world"}
