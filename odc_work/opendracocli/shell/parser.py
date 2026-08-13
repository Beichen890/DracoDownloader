"""Parser — 识别管道/重定向/逻辑操作符，产出 CommandIR

策略：遍历 lexer 产出的 token 流，按操作符切分为命令节点序列。
组合语义透传 shell，IR 只记录元数据。
"""

from __future__ import annotations

from typing import List

from ..errors import ERR_SHELL_PARSE_ERROR, make_error
from .ir import (
    OP_AND,
    OP_NONE,
    OP_OR,
    OP_PIPE,
    OP_SEQ,
    CommandIR,
    CommandNode,
    Redirect,
)
from .lexer import tokenize

# 操作符 token → IR 操作符常量
_OP_MAP = {
    "&&": OP_AND,
    "||": OP_OR,
    "|": OP_PIPE,
    ";": OP_SEQ,
}

# 重定向 token → kind
_REDIRECT_MAP = {
    ">": "stdout",
    ">>": "append",
    "<": "stdin",
    "2>": "stderr",
}

_OPERATOR_TOKENS = set(_OP_MAP.keys()) | set(_REDIRECT_MAP.keys())


def parse(text: str) -> CommandIR:
    """解析用户输入为 CommandIR

    Args:
        text: 用户原始输入

    Returns:
        CommandIR

    Raises:
        DracoError(ERR_SHELL_PARSE_ERROR): 解析失败
    """
    raw = text
    tokens = tokenize(text)
    if not tokens:
        return CommandIR(nodes=[], operators=[], redirects=[], raw_input=raw)

    nodes: List[CommandNode] = []
    operators: List[str] = []
    redirects: List[Redirect] = []

    # 当前命令节点的累积
    cur_name: str | None = None
    cur_args: List[str] = []
    cur_raw: List[str] = []

    def flush_node() -> None:
        nonlocal cur_name, cur_args, cur_raw
        if cur_name is not None:
            nodes.append(
                CommandNode(
                    name=cur_name,
                    args=list(cur_args),
                    raw=" ".join(cur_raw),
                )
            )
        cur_name = None
        cur_args = []
        cur_raw = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok in _OP_MAP:
            # 逻辑/管道操作符：结束当前节点
            if cur_name is None:
                raise make_error(
                    ERR_SHELL_PARSE_ERROR,
                    detail={"token": tok, "position": i, "input": text},
                    reason=f"操作符 '{tok}' 前缺少命令",
                )
            flush_node()
            operators.append(_OP_MAP[tok])
            i += 1
            continue

        if tok in _REDIRECT_MAP:
            # 重定向：下一个 token 是目标文件
            kind = _REDIRECT_MAP[tok]
            if i + 1 >= len(tokens):
                raise make_error(
                    ERR_SHELL_PARSE_ERROR,
                    detail={"token": tok, "position": i, "input": text},
                    reason=f"重定向 '{tok}' 后缺少目标文件",
                )
            target = tokens[i + 1]
            redirects.append(Redirect(kind=kind, target=target))
            i += 2
            continue

        # 普通参数/命令名
        if cur_name is None:
            cur_name = tok
        else:
            cur_args.append(tok)
        cur_raw.append(tok)
        i += 1

    # flush 最后一个节点
    flush_node()

    # 校验：操作符数量应为节点数 - 1
    if nodes and operators and len(operators) != len(nodes) - 1:
        raise make_error(
            ERR_SHELL_PARSE_ERROR,
            detail={"nodes": len(nodes), "operators": len(operators), "input": text},
            reason="操作符与命令节点数量不匹配",
        )

    if not nodes:
        raise make_error(
            ERR_SHELL_PARSE_ERROR,
            detail={"input": text},
            reason="未找到任何命令",
        )

    return CommandIR(
        nodes=nodes,
        operators=operators,
        redirects=redirects,
        raw_input=raw,
    )
