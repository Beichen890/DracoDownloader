"""词法切分 — 用 shlex

把用户输入切分为 token 流。保留操作符（&&, ||, |, ;, >, >>, <, 2>）为独立 token。
shlex 自带引号/转义处理。
"""

from __future__ import annotations

import re
from typing import List

from ..errors import ERR_SHELL_PARSE_ERROR, make_error

# 操作符正则：按这些 token 切分，但保留它们本身
# 顺序重要：长的在前（>> 在 > 前，&& 在 & 前）
_OPERATORS = ["&&", "||", "|", ";", ">>", ">", "<", "2>"]
_OP_PATTERN = re.compile(
    r"(\s+|" + "|".join(re.escape(op) for op in _OPERATORS) + r")"
)


def tokenize(text: str) -> List[str]:
    """将用户输入切分为 token 流

    保留操作符为独立 token，空白被丢弃。
    引号内容被 shlex 当作整体（操作符在引号内不切分）。

    Args:
        text: 用户原始输入

    Returns:
        token 列表，例如 ['ls', '-la', '&&', 'grep', 'foo']

    Raises:
        DracoError(ERR_SHELL_PARSE_ERROR): 引号不闭合等
    """
    if not text or not text.strip():
        return []

    # 先用正则把操作符两侧加保护，再用 shlex 切分
    # 这样能保留操作符为独立 token
    # 做法：把每个操作符替换为 " OP " 形式（前后加空格），
    # 但要避免破坏引号内的内容。
    protected = _protect_operators(text)

    import shlex

    try:
        tokens = shlex.split(protected, posix=True)
    except ValueError as e:
        raise make_error(
            ERR_SHELL_PARSE_ERROR,
            detail={"input": text},
            reason=str(e),
        ) from e

    # 把保护占位符还原为操作符
    restored = [_restore_token(t) for t in tokens]
    return [t for t in restored if t != ""]


# 保护机制：把操作符替换为不可被 shlex 切分的唯一占位符
# 占位符格式：\x00OP\x00，shlex 会把它当普通 word
def _protect_operators(text: str) -> str:
    """把操作符替换为占位符，但跳过引号内的内容"""
    result: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    in_double = False
    while i < n:
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            result.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            result.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            # 尝试匹配最长操作符
            matched = None
            for op in _OPERATORS:  # 已按长度降序
                if text.startswith(op, i):
                    matched = op
                    break
            if matched:
                # 前后加空格确保 shlex 切分
                result.append(f" \x00{matched}\x00 ")
                i += len(matched)
                continue
        result.append(ch)
        i += 1
    return "".join(result)


def _restore_token(token: str) -> str:
    """还原占位符为操作符"""
    if token.startswith("\x00") and token.endswith("\x00") and len(token) >= 2:
        inner = token[1:-1]
        if inner in _OPERATORS:
            return inner
    return token
