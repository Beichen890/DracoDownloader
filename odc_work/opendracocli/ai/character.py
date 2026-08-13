"""命令角色卡 — 复用 DracoHub character_card.json schema 子集

角色卡格式（复用上游 skeleton/personality/speech/emotion_profile/trigger_map）:
  {
    "name": "清道夫",
    "cmd": "rm",
    "skeleton": "你是清道夫，冷静利落的运维专家...",
    "personality": {"core": [...], "description": "..."},
    "speech": {"style": "...", "examples": [{"context":"...", "line":"..."}]},
    "emotion_profile": {"冷静": 0.5, "谨慎": 0.3},
    "trigger_map": {"删|清|remove": "谨慎"}
  }

加载顺序: 包内置 ai/roles/ → 用户 ~/.opendracocli/roles/（后者覆盖前者）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..logger import get_logger

log = get_logger("ai.character")


@dataclass
class CommandRole:
    """命令角色卡（character_card.json schema 子集）"""

    cmd: str
    name: str
    skeleton: str
    personality_core: List[str] = field(default_factory=list)
    personality_desc: str = ""
    speech_style: str = ""
    speech_examples: List[Dict[str, str]] = field(default_factory=list)
    emotion_profile: Dict[str, float] = field(default_factory=dict)
    trigger_map: Dict[str, str] = field(default_factory=dict)


# 默认中性角色（无匹配时使用，不角色化）
DEFAULT_ROLE = CommandRole(
    cmd="",
    name="终端助手",
    skeleton="你是终端助手，简洁专业地协助用户分析命令问题。回答简短直接。",
    personality_core=["专业", "简洁"],
    personality_desc="专业简洁的终端助手",
    speech_style="直接、技术性、不啰嗦",
    speech_examples=[],
    emotion_profile={"冷静": 0.6, "中性": 0.4},
    trigger_map={},
)


class RoleRegistry:
    """命令角色注册表

    内置角色 + 用户角色覆盖扩展（复用 RiskAssessor 的合并思路）。
    """

    def __init__(
        self,
        builtin_dir: Optional[Path] = None,
        user_dir: Optional[Path] = None,
    ) -> None:
        self._roles: Dict[str, CommandRole] = {}
        if builtin_dir:
            self._load_dir(builtin_dir)
        if user_dir:
            self._load_dir(user_dir)
            log.info("loaded user roles from %s", user_dir)
        log.debug("roles loaded: %d", len(self._roles))

    def _load_dir(self, dir_path: Path) -> None:
        """从目录加载所有 *.json 角色卡"""
        if not dir_path.exists():
            return
        for f in sorted(dir_path.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                role = self._parse(data)
                if role:
                    self._roles[role.cmd.lower()] = role
            except (OSError, json.JSONDecodeError, ValueError) as e:
                log.warning("failed to load role %s: %s", f, e)

    @staticmethod
    def _parse(data: dict) -> Optional[CommandRole]:
        """解析角色卡 dict → CommandRole"""
        if not isinstance(data, dict):
            return None
        cmd = str(data.get("cmd", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        skeleton = str(data.get("skeleton", "")).strip()
        if not cmd or not skeleton:
            return None
        personality = data.get("personality", {}) or {}
        speech = data.get("speech", {}) or {}
        return CommandRole(
            cmd=cmd,
            name=name or cmd,
            skeleton=skeleton,
            personality_core=[str(x) for x in personality.get("core", [])],
            personality_desc=str(personality.get("description", "")),
            speech_style=str(speech.get("style", "")),
            speech_examples=[
                {"context": str(ex.get("context", "")), "line": str(ex.get("line", ""))}
                for ex in speech.get("examples", [])
                if isinstance(ex, dict)
            ],
            emotion_profile={
                str(k): float(v)
                for k, v in (data.get("emotion_profile", {}) or {}).items()
            },
            trigger_map={
                str(k): str(v)
                for k, v in (data.get("trigger_map", {}) or {}).items()
            },
        )

    def match(self, cmd_first_token: str) -> CommandRole:
        """匹配命令角色

        Args:
            cmd_first_token: 命令首 token（别名展开+平台映射后）

        Returns:
            匹配的 CommandRole，无匹配返回 DEFAULT_ROLE
        """
        token = cmd_first_token.strip().lower()
        # 精确匹配
        if token in self._roles:
            return self._roles[token]
        # 前缀匹配（如 git push → git）
        for prefix, role in self._roles.items():
            if token.startswith(prefix):
                return role
        return DEFAULT_ROLE

    def list(self) -> Dict[str, CommandRole]:
        """列出所有角色"""
        return dict(self._roles)

    def get(self, cmd: str) -> Optional[CommandRole]:
        """精确获取某角色"""
        return self._roles.get(cmd.strip().lower())


# 全局单例
_global_registry: Optional[RoleRegistry] = None


def get_global_registry(
    builtin_dir: Optional[Path] = None,
    user_dir: Optional[Path] = None,
) -> RoleRegistry:
    """获取全局 RoleRegistry 单例"""
    global _global_registry
    if _global_registry is None:
        from ..config import get_global_config

        cfg = get_global_config()
        _global_registry = RoleRegistry(
            builtin_dir=cfg.builtin_roles_dir,
            user_dir=cfg.ai_roles_dir_resolved,
        )
    return _global_registry


def reset_global_registry() -> None:
    """重置全局注册表（测试用）"""
    global _global_registry
    _global_registry = None
