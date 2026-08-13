"""风险评估器 — 输入 IR → 输出 RiskAssessment

规则匹配逻辑:
  对 IR 中每个 CommandNode:
    遍历 rules.json 中所有等级的 patterns:
      if pattern.cmd == node.name (大小写不敏感):
        if pattern.args_contain 为空: 匹配（命令名命中即匹配）
        elif pattern.args_contain 中任一 token 在 node.args 中: 匹配
    取命中的最高等级作为该节点的风险等级
  整个 IR 的风险等级 = 所有节点中的最高等级

规则加载:
  - 包内 security/rules.json 为默认规则
  - 用户 ~/.opendracocli/rules.json 覆盖扩展（同等级 patterns 追加）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import DracoConfig, get_global_config
from ..logger import get_logger
from ..shell.ir import CommandIR

log = get_logger("security.risk_assessor")


class RiskLevel(Enum):
    """风险等级（severity 越高越危险）"""

    SAFE = "safe"
    CAUTION = "caution"
    DANGER = "danger"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return {"safe": 0, "caution": 1, "danger": 2, "critical": 3}[self.value]

    @classmethod
    def from_str(cls, s: str) -> "RiskLevel":
        return cls(s.lower())

    @classmethod
    def max(cls, *levels: "RiskLevel") -> "RiskLevel":
        """返回多个等级中最高的"""
        if not levels:
            return cls.SAFE
        return max(levels, key=lambda l: l.severity)


@dataclass
class MatchedRule:
    """命中的规则"""

    node_index: int       # 命中的 IR 节点序号
    cmd: str
    level: RiskLevel
    desc: str


@dataclass
class RiskAssessment:
    """风险评估结果"""

    level: RiskLevel
    matched_rules: List[MatchedRule] = field(default_factory=list)
    reason: str = ""

    @property
    def is_high_risk(self) -> bool:
        """是否高危（danger 或 critical）"""
        return self.level.severity >= RiskLevel.DANGER.severity


@dataclass
class _Pattern:
    """单条规则模式（内部用）

    匹配语义（AND 组合）:
      - cmd 必须匹配（大小写不敏感）
      - args_contain: 任一 token 作为子串在 args 中即满足此条件
        （空 list 表示此条件自动满足）
      - exact_args: 所有 token 必须在 args 中精确存在
        （空 list 表示此条件自动满足）
      - 两者都为空: 仅命令名匹配即命中
    """

    cmd: str
    args_contain: List[str]  # 子串匹配（任一命中）
    exact_args: List[str]    # 精确匹配（全部必须存在）
    desc: str


class RiskAssessor:
    """风险评估器"""

    def __init__(
        self,
        builtin_rules_path: Optional[Path] = None,
        user_rules_path: Optional[Path] = None,
    ) -> None:
        self._patterns_by_level: Dict[RiskLevel, List[_Pattern]] = {
            RiskLevel.CRITICAL: [],
            RiskLevel.DANGER: [],
            RiskLevel.CAUTION: [],
        }
        self._load_rules(builtin_rules_path, user_rules_path)

    def _load_rules(
        self,
        builtin_path: Optional[Path],
        user_path: Optional[Path],
    ) -> None:
        """加载规则：内置 + 用户覆盖扩展"""
        # 内置规则
        if builtin_path and builtin_path.exists():
            self._merge_from_file(builtin_path)
        # 用户规则（覆盖扩展）
        if user_path and user_path.exists():
            self._merge_from_file(user_path)
            log.info("loaded user rules from %s", user_path)
        total = sum(len(v) for v in self._patterns_by_level.values())
        log.debug("risk rules loaded: %d patterns", total)

    def _merge_from_file(self, path: Path) -> None:
        """从 JSON 文件合并规则到规则表"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.error("failed to load rules %s: %s", path, e)
            return

        for level_str, group in data.items():
            if level_str.startswith("_"):
                continue
            try:
                level = RiskLevel.from_str(level_str)
            except ValueError:
                log.warning("unknown risk level: %s", level_str)
                continue
            if level == RiskLevel.SAFE:
                continue  # safe 无规则
            if not isinstance(group, dict):
                continue
            patterns = group.get("patterns", [])
            if not isinstance(patterns, list):
                continue
            for p in patterns:
                if not isinstance(p, dict) or "cmd" not in p:
                    continue
                self._patterns_by_level[level].append(
                    _Pattern(
                        cmd=str(p["cmd"]).lower(),
                        args_contain=[
                            str(a) for a in p.get("args_contain", [])
                        ],
                        exact_args=[
                            str(a) for a in p.get("exact_args", [])
                        ],
                        desc=str(p.get("desc", "")),
                    )
                )

    def assess(self, ir: CommandIR) -> RiskAssessment:
        """评估 IR 的风险等级

        对每个节点匹配规则，取所有节点中的最高等级。
        """
        matched: List[MatchedRule] = []
        levels: List[RiskLevel] = [RiskLevel.SAFE]

        for idx, node in enumerate(ir.nodes):
            node_level, node_matched = self._assess_node(node, idx)
            if node_level != RiskLevel.SAFE:
                levels.append(node_level)
                matched.extend(node_matched)

        final_level = RiskLevel.max(*levels)
        reason = self._build_reason(matched)
        return RiskAssessment(level=final_level, matched_rules=matched, reason=reason)

    def _assess_node(
        self, node, idx: int
    ) -> tuple[RiskLevel, List[MatchedRule]]:
        """评估单个命令节点

        遍历 critical → danger → caution，命中即返回该等级。
        （高等级优先，因为同节点可能匹配多个等级，取最高）
        """
        cmd_lower = node.name.lower()
        args_lower = [a.lower() for a in node.args]

        # 按等级从高到低检查
        for level in [RiskLevel.CRITICAL, RiskLevel.DANGER, RiskLevel.CAUTION]:
            for pattern in self._patterns_by_level[level]:
                if pattern.cmd != cmd_lower:
                    continue
                if self._pattern_matches(pattern, args_lower):
                    return level, [
                        MatchedRule(
                            node_index=idx,
                            cmd=node.name,
                            level=level,
                            desc=pattern.desc,
                        )
                    ]
        return RiskLevel.SAFE, []

    @staticmethod
    def _pattern_matches(pattern: "_Pattern", args_lower: List[str]) -> bool:
        """检查 pattern 是否匹配 args

        语义（AND 组合）:
          - args_contain 为空 + exact_args 为空: 仅命令名匹配即命中
          - args_contain 非空: 任一 token 作为子串在 args 中
          - exact_args 非空: 所有 token 在 args 中精确存在
          - 两者非空: 都需满足
        """
        # args_contain 条件（任一子串命中）
        if pattern.args_contain:
            if not any(
                RiskAssessor._arg_contains(args_lower, token)
                for token in pattern.args_contain
            ):
                return False
        # exact_args 条件（全部精确存在）
        if pattern.exact_args:
            if not all(token in args_lower for token in pattern.exact_args):
                return False
        return True

    @staticmethod
    def _arg_contains(args: List[str], token: str) -> bool:
        """token 是否作为子串在 args 某项中"""
        for a in args:
            if token in a:
                return True
        return False

    @staticmethod
    def _build_reason(matched: List[MatchedRule]) -> str:
        if not matched:
            return ""
        parts = []
        for m in matched:
            parts.append(f"[{m.level.value}] {m.cmd}: {m.desc}")
        return "; ".join(parts)


_global_assessor: Optional[RiskAssessor] = None


def get_global_assessor(config: Optional[DracoConfig] = None) -> RiskAssessor:
    """获取全局 RiskAssessor 单例"""
    global _global_assessor
    if _global_assessor is None:
        cfg = config or get_global_config()
        _global_assessor = RiskAssessor(
            builtin_rules_path=cfg.builtin_rules_path,
            user_rules_path=cfg.security_rules_file_resolved,
        )
    return _global_assessor


def set_global_assessor(a: RiskAssessor) -> None:
    global _global_assessor
    _global_assessor = a
