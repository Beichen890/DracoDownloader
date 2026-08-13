"""别名加载器 — 从 JSON 文件加载别名定义

文件格式（~/.opendracocli/aliases.json）：
{
  "ll": "ls -la",
  "mydeploy": "git add . && git commit -m x && git push"
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from ..logger import get_logger

log = get_logger("aliases.loader")


def load_aliases(path: Path) -> Dict[str, str]:
    """从 JSON 文件加载别名定义

    文件不存在时返回空 dict（不报错，首次运行正常）。
    """
    if not path.exists():
        log.debug("aliases file not found, using empty: %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("aliases file is not a dict: %s", path)
            return {}
        # 只保留 str: str
        result: Dict[str, str] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str):
                result[k] = v
            else:
                log.warning("skip non-str alias entry: %r -> %r", k, v)
        return result
    except (OSError, json.JSONDecodeError) as e:
        log.error("failed to load aliases %s: %s", path, e)
        return {}


def save_aliases(path: Path, aliases: Dict[str, str]) -> None:
    """保存别名定义到 JSON 文件"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(aliases, f, ensure_ascii=False, indent=2, sort_keys=True)
        log.debug("saved %d aliases to %s", len(aliases), path)
    except OSError as e:
        log.error("failed to save aliases %s: %s", path, e)
