"""快捷键绑定 (P5)

默认快捷键表，可被 ~/.opendracocli/keymap.toml 覆盖。
textual 用 binding 形式 (如 'ctrl+t')，这里以字符串存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# 默认快捷键 → 动作名
DEFAULT_KEYMAP: dict[str, str] = {
    "ctrl+t": "switch_theme",       # 切换主题
    "ctrl+h": "toggle_history",     # 切换历史侧栏显隐
    "ctrl+l": "clear_output",       # 清屏 (清 OutputPanel)
    "ctrl+r": "search_history",     # 历史搜索
    "ctrl+a": "toggle_agent",       # 切换 Agent 进度面板 (P4)
    "ctrl+d": "quit",               # 退出 (发布 SessionEnded)
    "ctrl+e": "toggle_ai",          # 切换 AI 层 (P3)
    "escape": "close_modal",        # 关闭弹窗
}

# 动作名 → 中文说明 (供 /help 或设置界面显示)
ACTION_LABELS: dict[str, str] = {
    "switch_theme": "切换主题",
    "toggle_history": "历史侧栏显隐",
    "clear_output": "清屏",
    "search_history": "历史搜索",
    "toggle_agent": "Agent 面板显隐",
    "quit": "退出",
    "toggle_ai": "AI 层开关",
    "close_modal": "关闭弹窗",
}


@dataclass
class KeymapConfig:
    """快捷键配置"""

    bindings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYMAP))

    def action_for(self, key: str) -> Optional[str]:
        """按键 → 动作名，未绑定返回 None"""
        return self.bindings.get(key)

    def keys_for_action(self, action: str) -> list[str]:
        """动作 → 所有绑定的按键"""
        return [k for k, a in self.bindings.items() if a == action]

    def conflicts(self) -> list[str]:
        """检测重复绑定 (同一按键绑定多个动作本就是 dict 不可能，这里检测动作重复)
        返回有多个按键绑定的动作名列表 (信息性，不阻断)"""
        from collections import Counter

        counts = Counter(self.bindings.values())
        return [a for a, c in counts.items() if c > 1]


def load_keymap(path: Path | None = None) -> KeymapConfig:
    """从 TOML 加载快捷键配置

    path 为 None 时用默认 ~/.opendracocli/keymap.toml。
    文件不存在或解析失败返回默认 KeymapConfig (不抛异常)。

    TOML 格式:
        [bindings]
        "ctrl+t" = "switch_theme"
        "ctrl+l" = "clear_output"
    """
    if path is None:
        path = Path.home() / ".opendracocli" / "keymap.toml"
    if not path.exists():
        return KeymapConfig()
    try:
        data = _load_toml(path)
    except Exception:
        return KeymapConfig()
    raw = data.get("bindings", data) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return KeymapConfig()
    # 合并：用户配置覆盖默认 (而非替换)，保证新版本新增的默认绑定仍可用
    merged = dict(DEFAULT_KEYMAP)
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str):
            merged[k] = v
    return KeymapConfig(bindings=merged)


def _load_toml(path: Path) -> dict:
    """读 TOML (3.11+ tomllib，3.10 tomli)"""
    try:
        import tomllib  # type: ignore[import-not-found]

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ModuleNotFoundError:
        import tomli  # type: ignore[import-not-found]

        with open(path, "rb") as f:
            return tomli.load(f)
