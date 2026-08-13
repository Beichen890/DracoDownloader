"""归一化 — 命令名小写、参数 trim

对 IR 中每个 CommandNode 的 name 做小写归一、去前后空白。
args 保留原样（参数可能大小写敏感，如文件名）。
"""

from __future__ import annotations

from .ir import CommandIR, CommandNode


def normalize(ir: CommandIR) -> CommandIR:
    """对 IR 做归一化（原地修改并返回）

    - 命令名小写
    - 命令名/参数去前后空白
    - 空名节点过滤（防御性）
    """
    new_nodes: list[CommandNode] = []
    for node in ir.nodes:
        name = node.name.strip().lower()
        if not name:
            continue
        args = [a for a in node.args if a != ""]
        new_nodes.append(
            CommandNode(name=name, args=args, raw=node.raw.strip())
        )
    ir.nodes = new_nodes
    return ir
