"""别名管理器 — CRUD + 持久化"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from ..config import DracoConfig, get_global_config
from ..logger import get_logger
from .loader import load_aliases, save_aliases

log = get_logger("aliases.manager")


class AliasManager:
    """别名管理器

    提供别名表给 AliasExpander 查询（get 方法），
    同时支持用户 CRUD（add/remove/list）并持久化到配置文件。
    """

    def __init__(self, config: DracoConfig | None = None) -> None:
        self._config = config or get_global_config()
        self._aliases: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        path = self._config.aliases_file_resolved
        self._aliases = load_aliases(path)
        log.info("loaded %d aliases from %s", len(self._aliases), path)

    def _save(self) -> None:
        path = self._config.aliases_file_resolved
        save_aliases(path, self._aliases)

    def get(self, name: str) -> Optional[str]:
        """查询别名展开式（供 AliasExpander 调用）"""
        return self._aliases.get(name)

    def add(self, name: str, expansion: str) -> bool:
        """添加/更新别名。返回是否新增（False=覆盖已有）"""
        is_new = name not in self._aliases
        self._aliases[name] = expansion
        self._save()
        log.info("alias %s: %s", "added" if is_new else "updated", name)
        return is_new

    def remove(self, name: str) -> bool:
        """删除别名。返回是否删除成功"""
        if name in self._aliases:
            del self._aliases[name]
            self._save()
            log.info("alias removed: %s", name)
            return True
        return False

    def list(self) -> Dict[str, str]:
        """列出所有别名（返回副本）"""
        return dict(self._aliases)

    def names(self) -> List[str]:
        """所有别名名（供 TUI Tab 补全）"""
        return sorted(self._aliases.keys())

    def reload(self) -> None:
        """重新从磁盘加载（外部编辑文件后调用）"""
        self._load()


_global_manager: AliasManager | None = None


def get_global_manager() -> AliasManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = AliasManager()
    return _global_manager


def set_global_manager(m: AliasManager) -> None:
    global _global_manager
    _global_manager = m
