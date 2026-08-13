"""函数参数解析 — positional + keyword

支持调用语法:
    deploy                    # 无参
    clean_logs days=3         # 关键字参数
    clean_logs 3              # 位置参数（按签名顺序绑定）
    greet name=draco          # 关键字
    greet draco               # 位置
    backup src=a.py dst=b.py  # 关键字混合

规则:
    - key=value → kwargs
    - 其他 → args（按签名顺序绑定）
    - 含空格的值用引号：greet "hello world"
    - 类型转换：基于函数签名的 type hint（int/float/bool/str）
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from ..errors import ERR_AGENT_ARGS, make_error


class ArgParseError(Exception):
    """参数解析错误"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class ParsedArgs:
    """解析后的参数"""

    args: List[str]
    kwargs: Dict[str, str]  # 原始字符串值，绑定签名时再转换类型


def tokenize(raw: str) -> List[str]:
    """用 shlex 分词，支持引号"""
    try:
        return shlex.split(raw)
    except ValueError as e:
        raise ArgParseError(f"参数分词失败: {e}")


def split_kv(tokens: List[str]) -> ParsedArgs:
    """把 token 列表分为 positional 和 keyword

    规则：
        - 含 `=` 且 `=` 前为合法标识符 → keyword
        - 否则 → positional
        - 一旦出现 keyword，后续 positional 视为错误（Python 语法约定）

    Args:
        tokens: shlex 分词后的 token 列表

    Returns:
        ParsedArgs

    Raises:
        ArgParseError: positional 出现在 keyword 之后
    """
    args: List[str] = []
    kwargs: Dict[str, str] = {}
    seen_kw = False

    for tok in tokens:
        # 检测 key=value（key 必须是合法 Python 标识符）
        eq_idx = tok.find("=")
        if eq_idx > 0:
            key = tok[:eq_idx]
            if key.isidentifier():
                value = tok[eq_idx + 1 :]
                kwargs[key] = value
                seen_kw = True
                continue
        # 不是 keyword
        if seen_kw:
            raise ArgParseError(
                f"位置参数 '{tok}' 出现在关键字参数之后，Python 不允许此语法"
            )
        args.append(tok)

    return ParsedArgs(args=args, kwargs=kwargs)


def _convert_value(value: str, type_hint: Any) -> Any:
    """根据 type hint 转换字符串值

    支持 type 对象（bool/int/float）和字符串形式（'bool'/'int'/'float'），
    后者出现在 `from __future__ import annotations` 模块中（注解被惰性化为字符串）。
    """
    name = type_hint if isinstance(type_hint, str) else getattr(type_hint, "__name__", "")
    if name == "bool":
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        raise ArgParseError(f"无法把 '{value}' 转换为 bool")
    if name == "int":
        try:
            return int(value)
        except ValueError:
            raise ArgParseError(f"无法把 '{value}' 转换为 int")
    if name == "float":
        try:
            return float(value)
        except ValueError:
            raise ArgParseError(f"无法把 '{value}' 转换为 float")
    # str / 无注解 / 其他类型：保持字符串
    return value


def bind_to_signature(
    parsed: ParsedArgs,
    param_names: List[str],
    param_types: Dict[str, Any],
    defaults: Dict[str, Any],
) -> Tuple[List[Any], Dict[str, Any]]:
    """把解析后的参数绑定到函数签名

    Args:
        parsed: 解析后的 ParsedArgs
        param_names: 签名参数名列表（不含 ctx，已剥离）
        param_types: 参数名 → type hint
        defaults: 参数名 → 默认值（无默认值的参数不在此 dict 中）

    Returns:
        (bound_args, bound_kwargs): 绑定后的位置参数和关键字参数

    Raises:
        ArgParseError: 参数数量不匹配 / 未知关键字 / 缺少必填参数
    """
    bound_kwargs: Dict[str, Any] = {}
    consumed_positional = 0

    # 1. 处理位置参数：按签名顺序绑定
    for i, value in enumerate(parsed.args):
        if i >= len(param_names):
            raise ArgParseError(
                f"位置参数过多：函数只接受 {len(param_names)} 个位置参数，"
                f"但传入了 {len(parsed.args)} 个"
            )
        name = param_names[i]
        bound_kwargs[name] = _convert_value(value, param_types.get(name, str))
        consumed_positional += 1

    # 2. 处理关键字参数
    for key, value in parsed.kwargs.items():
        if key not in param_names:
            raise ArgParseError(
                f"未知关键字参数: {key}（可用: {', '.join(param_names) or '无'}）"
            )
        if key in bound_kwargs:
            raise ArgParseError(f"参数 '{key}' 被重复指定（位置 + 关键字）")
        bound_kwargs[key] = _convert_value(value, param_types.get(key, str))

    # 3. 检查必填参数（无默认值的）
    for name in param_names:
        if name not in bound_kwargs and name not in defaults:
            raise ArgParseError(f"缺少必填参数: {name}")

    # 4. 填充默认值（未提供的参数用默认值）
    for name, default in defaults.items():
        if name not in bound_kwargs:
            bound_kwargs[name] = default

    # 返回：位置参数为空（全部绑定到 kwargs），kwargs 为最终绑定
    # 这样调用 func(ctx, **bound_kwargs) 最稳健
    return [], bound_kwargs


def parse_call_args(
    raw_args: str,
    param_names: List[str],
    param_types: Dict[str, Any],
    defaults: Dict[str, Any],
) -> Tuple[List[Any], Dict[str, Any]]:
    """解析调用参数并绑定到函数签名

    主入口：raw_args → tokenize → split_kv → bind_to_signature

    Args:
        raw_args: 原始参数字符串（如 "days=3" 或 "3" 或 ""）
        param_names: 签名参数名列表（不含 ctx）
        param_types: 参数名 → type hint
        defaults: 参数名 → 默认值

    Returns:
        (bound_args, bound_kwargs): 绑定结果（bound_args 通常为空，全部走 kwargs）

    Raises:
        ArgParseError: 解析或绑定失败
    """
    raw_args = raw_args.strip()
    if not raw_args:
        # 无参数：检查是否所有参数都有默认值
        for name in param_names:
            if name not in defaults:
                raise ArgParseError(f"缺少必填参数: {name}")
        # 全部用默认值
        bound = {name: defaults[name] for name in defaults}
        return [], bound

    tokens = tokenize(raw_args)
    parsed = split_kv(tokens)
    return bind_to_signature(parsed, param_names, param_types, defaults)


def to_draco_error(err: ArgParseError) -> "Any":
    """把 ArgParseError 转为 DracoError"""
    return make_error(ERR_AGENT_ARGS, reason=err.message)
