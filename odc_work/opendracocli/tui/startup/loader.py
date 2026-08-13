"""启动界面加载器: 上下文/配置/模板注册

纯文本生成逻辑, 不依赖 textual / rich 运行时。
所有加载/解析失败均回退到默认值, 绝不抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable
import importlib
import importlib.util
import os
import sys


@dataclass
class StartupContext:
    """启动界面渲染所需的上下文数据"""

    version: str = "0.1.0"
    platform: str = ""                # 'win' | 'linux' | 'darwin'
    python_version: str = ""          # 如 '3.11.5'
    session_id: str = ""              # 会话 ID (取前 8 位显示)
    cwd: str = ""
    config_path: str = ""             # 加载的启动配置文件路径 (可空)
    last_session_summary: str = ""    # 上次会话最后 N 条命令摘要 (可空)
    uptime_hint: str = ""             # 启动耗时提示 (如 'loaded in 0.3s')


@dataclass
class StartupConfig:
    """启动界面配置 (对应 ~/.opendracocli/startup.toml)"""

    enabled: bool = True
    template: str = "draco"            # 内置模板名或自定义模板文件名(不带.py)
    animation: str = "spinner"         # 'spinner' | 'progress' | 'typewriter' | 'none'
    duration_ms: int = 1500            # 显示时长
    skip_on_key: bool = True           # 任意按键跳过
    custom_templates_dir: str = "~/.opendracocli/startup_templates"


# 模板协议: (StartupContext) -> str
StartupTemplateFunc = Callable[[StartupContext], str]


def _builtin_minimal_fallback(ctx: StartupContext) -> str:
    """内置兜底: 当 templates/minimal.py 也加载失败时使用, 至少保证有输出"""
    head = f"OpenDracoCLI v{ctx.version}"
    if ctx.platform:
        head += f" ({ctx.platform})"
    return head


def load_startup_config(path: Path | None = None) -> StartupConfig:
    """从 TOML 加载 StartupConfig。

    path 为 None 用默认 ~/.opendracocli/startup.toml。
    文件不存在返回默认 StartupConfig()。解析失败也返回默认 (不抛异常)。
    用 tomllib (3.11+) 或 tomli (3.10)。
    """
    # 解析目标路径 (展开 ~ 与环境变量)
    if path is None:
        target = Path(os.path.expanduser("~/.opendracocli/startup.toml"))
    else:
        target = Path(os.path.expanduser(os.path.expandvars(str(path))))

    if not target.exists():
        return StartupConfig()

    # 读取并解析 TOML, 任何失败都回退默认
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:  # pragma: no cover - 仅 3.10 走此分支
            import tomli as tomllib  # type: ignore
        with open(target, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return StartupConfig()

    # 兼容顶层字段或 [startup] 段
    if isinstance(data, dict):
        section = data.get("startup", data)
    else:
        section = {}
    if not isinstance(section, dict):
        section = {}

    cfg = StartupConfig()
    try:
        if "enabled" in section:
            cfg.enabled = bool(section["enabled"])
        if "template" in section:
            cfg.template = str(section["template"])
        if "animation" in section:
            cfg.animation = str(section["animation"])
        if "duration_ms" in section:
            cfg.duration_ms = int(section["duration_ms"])
        if "skip_on_key" in section:
            cfg.skip_on_key = bool(section["skip_on_key"])
        if "custom_templates_dir" in section:
            cfg.custom_templates_dir = str(section["custom_templates_dir"])
    except Exception:
        return StartupConfig()
    return cfg


class StartupLoader:
    """启动模板注册与加载"""

    def __init__(self) -> None:
        self._builtins: dict[str, StartupTemplateFunc] = {}   # 内置模板
        self._customs: dict[str, StartupTemplateFunc] = {}    # 自定义模板
        self._load_builtins()

    def _load_builtins(self) -> None:
        """注册 4 个内置模板: minimal/figlet/ascii_art/neofetch_style

        从 .templates 子包 import 对应 render 函数。
        每个 import 失败都要捕获, 至少 minimal 必须注册成功。
        """
        pkg = __package__ or "opendracocli.tui.startup"
        for name in ("minimal", "figlet", "ascii_art", "neofetch_style"):
            try:
                mod = importlib.import_module(f".templates.{name}", package=pkg)
                fn = getattr(mod, "render", None)
                if callable(fn):
                    self._builtins[name] = fn
            except Exception:
                # 单个模板加载失败不阻断其它模板
                continue
        # minimal 是最后兜底, 必须存在
        if "minimal" not in self._builtins:
            self._builtins["minimal"] = _builtin_minimal_fallback

    def register_custom_dir(self, dir_path: Path) -> int:
        """扫描自定义模板目录, 加载每个 .py 文件 (文件名去掉 .py 为模板名)。

        .py 文件必须导出 render(ctx: StartupContext) -> str 函数。
        加载失败的文件跳过 (记录但不抛)。返回成功加载数量。
        用 importlib.util.spec_from_file_location 动态加载。
        """
        try:
            dir_path = Path(dir_path)
        except Exception:
            return 0
        if not dir_path.exists() or not dir_path.is_dir():
            return 0

        count = 0
        try:
            py_files = sorted(dir_path.glob("*.py"))
        except Exception:
            return 0

        for py_file in py_files:
            # 跳过 __init__.py 等以下划线开头的文件
            if py_file.name.startswith("_"):
                continue
            name = py_file.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"_draco_startup_custom_{name}", py_file
                )
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fn = getattr(mod, "render", None)
                if callable(fn):
                    self._customs[name] = fn
                    count += 1
            except Exception:
                # 单个自定义模板失败不影响其它
                continue
        return count

    def get(self, name: str) -> StartupTemplateFunc:
        """获取模板函数: 先查内置, 再查自定义。不存在抛 KeyError。"""
        if name in self._builtins:
            return self._builtins[name]
        if name in self._customs:
            return self._customs[name]
        raise KeyError(name)

    def list_templates(self) -> list[str]:
        """列出所有可用模板名 (内置 + 自定义)"""
        return sorted(set(self._builtins) | set(self._customs))
