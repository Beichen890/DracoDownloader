"""canonical IR 定义

IR 只负责**单个命令节点**的命令名/参数表示。
组合语义（管道/重定向/逻辑操作符）作为元数据记录，但执行语义透传原生 shell。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


# === 操作符常量 ===
OP_AND = "&&"
OP_OR = "||"
OP_PIPE = "|"
OP_SEQ = ";"  # 顺序执行
OP_NONE = ""  # 单命令，无操作符


@dataclass
class CommandNode:
    """单个命令节点

    Attributes:
        name: 归一化后的命令名（小写）
        args: 参数列表
        raw: 该节点的原始文本片段
    """

    name: str
    args: List[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Redirect:
    """重定向元数据

    注意：P1 只记录元数据，执行语义透传 shell。
    kind 取值: 'stdout' (>) / 'append' (>>) / 'stdin' (<) / 'stderr' (2>)
    """

    kind: str
    target: str


@dataclass
class CommandIR:
    """一条用户输入归一化后的完整 IR

    nodes 与 operators 的关系：
      nodes = [A, B, C]
      operators = [OP_AND, OP_PIPE]
      表示 A && B | C
      即 operators[i] 是 nodes[i] 与 nodes[i+1] 之间的操作符。
      len(operators) == len(nodes) - 1（单命令时 operators 为空）。

    redirects 作用于整条命令（透传 shell，元数据用）。

    Attributes:
        nodes: 命令节点序列
        operators: 节点间操作符
        redirects: 重定向列表
        raw_input: 用户原始完整输入
    """

    nodes: List[CommandNode] = field(default_factory=list)
    operators: List[str] = field(default_factory=list)
    redirects: List[Redirect] = field(default_factory=list)
    raw_input: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "operators": list(self.operators),
            "redirects": [asdict(r) for r in self.redirects],
            "raw_input": self.raw_input,
        }

    def to_json(self) -> str:
        """序列化为 JSON 字符串（存入历史）"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def single(cls, name: str, args: List[str] | None = None, raw: str = "") -> "CommandIR":
        """构造单命令 IR（便捷方法）"""
        return cls(
            nodes=[CommandNode(name=name, args=list(args or []), raw=raw or name)],
            operators=[],
            redirects=[],
            raw_input=raw or name,
        )

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CommandIR":
        """从字典反序列化"""
        return cls(
            nodes=[CommandNode(**n) for n in d.get("nodes", [])],
            operators=list(d.get("operators", [])),
            redirects=[Redirect(**r) for r in d.get("redirects", [])],
            raw_input=d.get("raw_input", ""),
        )
