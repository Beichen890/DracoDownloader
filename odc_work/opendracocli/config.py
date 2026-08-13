"""OpenDracoCLI 配置

环境变量 DRACO_* 覆盖，参考 DracoDownloader config.py 的哲学。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, List


def _expand(p: str) -> str:
    """展开 ~ 和环境变量"""
    return os.path.expanduser(os.path.expandvars(p))


@dataclass
class DracoConfig:
    """全局配置

    所有字段支持 DRACO_<FIELD_NAME_UPPER> 环境变量覆盖。
    """

    history_db_path: str = "~/.opendracocli/history.db"
    aliases_file: str = "~/.opendracocli/aliases.json"
    stdout_summary_lines: int = 20
    stderr_summary_lines: int = 20
    exec_timeout: float = 3600.0
    max_alias_depth: int = 3
    log_level: str = "INFO"
    # Windows 用 cmd /c, Unix 用 bash -c（不兼容 PowerShell）
    windows_shell: str = "cmd"
    unix_shell: str = "bash"

    # P2 安全与风控
    security_rules_file: str = "~/.opendracocli/rules.json"  # 用户规则覆盖（与包内默认合并）
    auth_hash_file: str = "~/.opendracocli/auth.hash"
    sandbox_writable_paths: list[str] = field(
        default_factory=lambda: ["~", "/tmp"]
    )
    require_auth_for: str = "critical"  # critical / danger / off
    pbkdf2_iterations: int = 100000

    # P3 AI 智能层（默认关闭，显式开启）
    ai_enabled: bool = False
    ai_api_base: str = "http://localhost:8000/v1"
    ai_api_key: str = ""
    ai_model: str = "deepseek-chat"
    ai_max_tokens: int = 512
    ai_temperature: float = 0.4
    ai_timeout: float = 30.0
    ai_cooldown: float = 1.0
    ai_auto_correct: bool = True
    ai_roles_dir: str = "~/.opendracocli/roles"
    ai_context_file: str = "~/.opendracocli/context.json"
    ai_risk_enabled: bool = True
    ai_perception_enabled: bool = True
    emotion_weights: dict = field(
        default_factory=lambda: {"user": 0.4, "history": 0.3, "character": 0.2, "random": 0.1}
    )
    emotion_inertia: float = 0.3

    # P4 Agent 自动化（默认关闭，显式开启）
    agent_enabled: bool = False
    functions_file: str = "~/.opendracocli/functions.py"
    agent_code_sandbox: bool = True          # AI 生成代码做 AST 检查
    agent_auto_confirm_generated: bool = False  # AI 生成代码默认需确认
    agent_max_gen_tokens: int = 1024

    # P5 打磨（界面细化）— 全部有默认值，向后兼容 P1-P4
    tui_engine: str = "textual"              # 'textual' | 'simple' (非 TTY 自动降级)
    theme: str = "draco"                     # 默认主题名
    custom_themes_dir: str = "~/.opendracocli/themes"
    startup_config: str = "~/.opendracocli/startup.toml"
    startup_enabled: bool = True             # 启动界面总开关
    startup_template: str = "draco"          # 启动模板
    startup_animation: str = "spinner"       # 'spinner'|'progress'|'typewriter'|'none'
    startup_duration_ms: int = 1500
    startup_skip_on_key: bool = True
    startup_custom_templates_dir: str = "~/.opendracocli/startup_templates"
    keymap_file: str = "~/.opendracocli/keymap.toml"
    fallback_simple_in_non_tty: bool = True  # 非 TTY 自动降级 SimpleRenderer

    def __post_init__(self) -> None:
        # 环境变量覆盖
        for f in fields(self):
            env_key = f"DRACO_{f.name.upper()}"
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            self._set_from_str(f.name, raw, f.type)

    def _set_from_str(self, name: str, raw: str, type_hint: Any) -> None:
        cur = getattr(self, name)
        if isinstance(cur, bool) or type_hint is bool:
            setattr(self, name, raw.lower() in ("1", "true", "yes", "on"))
        elif isinstance(cur, int) or type_hint is int:
            setattr(self, name, int(raw))
        elif isinstance(cur, float) or type_hint is float:
            setattr(self, name, float(raw))
        elif isinstance(cur, list) or type_hint is list:
            # 列表字段：用 ; 或 : 分隔（Windows 优先 ;，Unix 优先 :）
            sep = ";" if sys.platform.startswith("win") else ":"
            setattr(self, name, [s.strip() for s in raw.split(sep) if s.strip()])
        elif isinstance(cur, dict) or type_hint is dict:
            # dict 字段：用 JSON 解析
            import json as _json
            try:
                setattr(self, name, _json.loads(raw))
            except (ValueError, TypeError):
                pass
        else:
            setattr(self, name, raw)

    @property
    def history_db_resolved(self) -> Path:
        return Path(_expand(self.history_db_path))

    @property
    def aliases_file_resolved(self) -> Path:
        return Path(_expand(self.aliases_file))

    @property
    def security_rules_file_resolved(self) -> Path:
        return Path(_expand(self.security_rules_file))

    @property
    def auth_hash_file_resolved(self) -> Path:
        return Path(_expand(self.auth_hash_file))

    @property
    def sandbox_writable_paths_resolved(self) -> List[Path]:
        """可写路径白名单（展开 ~ 和环境变量）"""
        return [Path(_expand(p)) for p in self.sandbox_writable_paths]

    @property
    def builtin_rules_path(self) -> Path:
        """包内置默认规则表"""
        return Path(__file__).parent / "security" / "rules.json"

    @property
    def ai_roles_dir_resolved(self) -> Path:
        return Path(_expand(self.ai_roles_dir))

    @property
    def ai_context_file_resolved(self) -> Path:
        return Path(_expand(self.ai_context_file))

    @property
    def builtin_roles_dir(self) -> Path:
        """包内置命令角色卡目录"""
        return Path(__file__).parent / "ai" / "roles"

    @property
    def functions_file_resolved(self) -> Path:
        """P4 用户函数文件路径（展开 ~ 和环境变量）"""
        return Path(_expand(self.functions_file))

    # === P5 resolved ===
    @property
    def custom_themes_dir_resolved(self) -> Path:
        return Path(_expand(self.custom_themes_dir))

    @property
    def startup_config_resolved(self) -> Path:
        return Path(_expand(self.startup_config))

    @property
    def startup_custom_templates_dir_resolved(self) -> Path:
        return Path(_expand(self.startup_custom_templates_dir))

    @property
    def keymap_file_resolved(self) -> Path:
        return Path(_expand(self.keymap_file))

    @property
    def is_tty(self) -> bool:
        """当前 stdout 是否为 TTY（决定是否降级 SimpleRenderer）"""
        import sys

        return bool(sys.stdout.isatty())

    @property
    def effective_tui_engine(self) -> str:
        """实际使用的 TUI 引擎：非 TTY 且开启降级时返回 'simple'"""
        if self.tui_engine == "simple":
            return "simple"
        if self.fallback_simple_in_non_tty and not self.is_tty:
            return "simple"
        return "textual"

    @property
    def ai_api_key_effective(self) -> str:
        """有效 API key（占位符视为空）"""
        key = self.ai_api_key.strip()
        placeholders = {"", "your_api_key", "sk-your", "your-key", "placeholder"}
        if key.lower() in placeholders:
            return ""
        return key

    @property
    def current_platform(self) -> str:
        """返回 'win' | 'linux' | 'darwin'"""
        import sys

        if sys.platform.startswith("win"):
            return "win"
        if sys.platform == "darwin":
            return "darwin"
        return "linux"

    @property
    def native_shell(self) -> tuple[str, list[str]]:
        """返回当前平台原生 shell 的 (executable, prefix_args)

        Windows 统一用 cmd /c，Unix 用 bash -c（不兼容 PowerShell）。
        用于 subprocess 调用：shell_executable + prefix + [command_str]
        """
        if self.current_platform == "win":
            shell = self.windows_shell
            if shell == "cmd":
                return ("cmd.exe", ["/c"])
            # 用户若强行配置其他 shell，按 cmd 风格 /c 调用
            return (shell, ["/c"])
        # Unix-like
        shell = self.unix_shell
        if shell == "bash":
            return ("bash", ["-c"])
        if shell == "zsh":
            return ("zsh", ["-c"])
        if shell == "sh":
            return ("sh", ["-c"])
        return (shell, ["-c"])


_global_config: DracoConfig | None = None


def get_global_config() -> DracoConfig:
    """获取全局配置单例"""
    global _global_config
    if _global_config is None:
        _global_config = DracoConfig()
    return _global_config


def set_global_config(cfg: DracoConfig) -> None:
    """注入全局配置（测试用）"""
    global _global_config
    _global_config = cfg
