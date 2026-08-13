"""AliasExpander — 查别名表，宏展开

决策 1b：别名展开先于平台映射。展开产出的是抽象命令名（如 ls），尚未平台映射。

别名定义示例：
  "ll": "ls -la"
  "mydeploy": "git add . && git commit -m x && git push"

展开规则：
  - 命中别名时，把别名替换为展开内容，重新解析为子 IR
  - 递归展开：展开结果中的命令名若也是别名，继续展开
  - 递归深度限制 max_alias_depth（默认 3），超限抛 draco.alias.recursive
  - 多命令别名（含 && || | ;）展开为多个节点，**保留别名体原始操作符**
  - 原节点的额外参数附加到别名展开后最后一个节点

注意：别名只匹配命令名（节点 name），不匹配参数。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from ..config import DracoConfig, get_global_config
from ..errors import ERR_ALIAS_RECURSIVE, make_error
from ..logger import get_logger
from .ir import CommandIR, CommandNode
from .parser import parse

if TYPE_CHECKING:
    from ..aliases.manager import AliasManager

log = get_logger("shell.alias_expander")


class AliasExpander:
    """别名/宏展开器"""

    def __init__(
        self,
        manager: Optional["AliasManager"] = None,
        config: DracoConfig | None = None,
    ) -> None:
        self._manager = manager
        self._config = config or get_global_config()

    def set_manager(self, manager: "AliasManager") -> None:
        self._manager = manager

    def expand(self, ir: CommandIR) -> Tuple[CommandIR, Optional[str]]:
        """展开 IR 中的别名

        Returns:
            (展开后的 IR, 命中的第一个别名名 或 None)

        Raises:
            DracoError(ERR_ALIAS_RECURSIVE): 递归超过 max_alias_depth 或检测到环
        """
        if self._manager is None:
            return ir, None

        max_depth = self._config.max_alias_depth
        new_nodes: List[CommandNode] = []
        new_operators: List[str] = []
        first_alias: Optional[str] = None

        for idx, node in enumerate(ir.nodes):
            expanded_nodes, expanded_ops, hit_alias = self._expand_node(
                node, max_depth, frozenset(), depth=0
            )
            if hit_alias is not None and first_alias is None:
                first_alias = hit_alias

            # 拼接展开结果
            if new_nodes:
                # 当前节点与之前的连接操作符（来自父 IR）
                # new_operators 已在上一轮 append 了父操作符
                pass
            new_nodes.extend(expanded_nodes)
            # 展开节点内部的操作符
            new_operators.extend(expanded_ops)
            # 父 IR 中此节点之后的操作符
            if idx < len(ir.operators):
                new_operators.append(ir.operators[idx])

        ir.nodes = new_nodes
        ir.operators = new_operators
        return ir, first_alias

    def _expand_node(
        self,
        node: CommandNode,
        max_depth: int,
        chain: frozenset,
        depth: int,
    ) -> Tuple[List[CommandNode], List[str], Optional[str]]:
        """展开单个命令节点

        Returns:
            (展开后的节点列表, 节点间操作符列表, 命中的别名名 或 None)
            操作符列表长度 = 节点列表长度 - 1（单节点时为空）
        """
        if depth > max_depth:
            raise make_error(
                ERR_ALIAS_RECURSIVE,
                detail={"alias": node.name, "depth": depth, "chain": sorted(chain)},
                alias=node.name,
                depth=depth,
            )

        assert self._manager is not None
        expansion = self._manager.get(node.name)
        if expansion is None:
            # 非别名，原样返回
            return [node], [], None

        # 检测直接环
        if node.name in chain:
            raise make_error(
                ERR_ALIAS_RECURSIVE,
                detail={"alias": node.name, "chain": sorted(chain)},
                alias=node.name,
                depth=depth,
            )

        hit_alias = node.name
        log.debug("alias hit: %s -> %s (depth=%d)", node.name, expansion, depth)

        # 解析别名体为子 IR（保留其内部操作符）
        sub_ir = parse(expansion)
        new_chain = chain | {node.name}

        out_nodes: List[CommandNode] = []
        out_ops: List[str] = []
        for i, sn in enumerate(sub_ir.nodes):
            sub_nodes, sub_ops, _ = self._expand_node(
                sn, max_depth, new_chain, depth + 1
            )
            out_nodes.extend(sub_nodes)
            out_ops.extend(sub_ops)
            # 子 IR 内部操作符（保留别名体的 && 等）
            if i < len(sub_ir.operators):
                out_ops.append(sub_ir.operators[i])

        # 原节点的额外参数附加到最后一个展开节点
        if node.args and out_nodes:
            out_nodes[-1].args.extend(node.args)

        return out_nodes, out_ops, hit_alias
