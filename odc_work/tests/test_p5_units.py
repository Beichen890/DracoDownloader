"""P5 单元测试 — 主题系统 / 启动界面 / 渲染协议层 / 快捷键"""
import importlib
import sys
from pathlib import Path

import pytest

from opendracocli.config import DracoConfig


# ==================== 主题系统 ====================


def test_theme_provider_load_builtin():
    from opendracocli.tui.theme import ThemeProvider

    p = ThemeProvider()
    t = p.load("draco")
    assert t.name == "draco"
    assert t.css  # 非空
    assert "bg" in t.tokens
    assert len(t.css) > 100  # CSS 有实质内容


def test_theme_provider_list_builtin():
    from opendracocli.tui.theme import ThemeProvider

    names = ThemeProvider().list_themes()
    assert "draco" in names
    assert "dark" in names
    assert "light" in names


def test_theme_provider_switch():
    from opendracocli.tui.theme import ThemeProvider

    p = ThemeProvider()
    p.load("draco")
    t = p.switch("light")
    assert t.name == "light"
    assert p.current().name == "light"


def test_theme_provider_unknown_raises():
    from opendracocli.tui.theme import ThemeError, ThemeProvider

    with pytest.raises(ThemeError):
        ThemeProvider().load("not_exist")


def test_theme_provider_switch_failure_keeps_current():
    from opendracocli.tui.theme import ThemeProvider

    p = ThemeProvider()
    p.load("draco")
    with pytest.raises(Exception):
        p.switch("not_exist")
    assert p.current().name == "draco"  # 失败不改变当前


def test_theme_custom_dir(tmp_path):
    from opendracocli.tui.theme import ThemeProvider

    custom = tmp_path / "mytheme.toml"
    custom.write_text(
        'name = "mytheme"\n[tokens]\nbg = "#000000"\nfg = "#ffffff"\n'
        'accent = "#ff0000"\nerror = "#ff0000"\nwarn = "#ffff00"\n'
        'info = "#00ff00"\nmuted = "#888888"\n',
        encoding="utf-8",
    )
    p = ThemeProvider()
    p.register_custom_dir(tmp_path)
    names = p.list_themes()
    assert "mytheme" in names
    t = p.load("mytheme")
    assert t.tokens["accent"] == "#ff0000"


def test_theme_generate_css_has_design_tokens():
    from opendracocli.tui.theme import generate_css

    css = generate_css(
        {"bg": "#0d1117", "fg": "#c9d1d9", "accent": "#58a6ff",
         "error": "#f85149", "warn": "#d29922", "info": "#7ee787", "muted": "#6e7681"},
    )
    assert "$background" in css or "$accent" in css
    assert "#58a6ff" in css


# ==================== 启动界面 ====================


def _ctx():
    from opendracocli.tui.startup.loader import StartupContext

    return StartupContext(
        version="0.1.0", platform="linux", python_version="3.11",
        session_id="abcd1234efgh", cwd="/tmp", uptime_hint="loaded in 0.3s",
    )


def test_startup_templates_all_render():
    from opendracocli.tui.startup.renderer import StartupRenderer

    r = StartupRenderer()
    for name in ["minimal", "figlet", "ascii_art", "neofetch_style"]:
        text = r.render(name, _ctx())
        assert isinstance(text, str)
        assert len(text) > 0
        assert "0.1.0" in text or "OpenDracoCLI" in text


def test_startup_renderer_unknown_falls_back():
    from opendracocli.tui.startup.renderer import StartupRenderer

    r = StartupRenderer()
    text = r.render("not_exist", _ctx())
    assert len(text) > 0  # 回退 minimal


def test_startup_loader_list():
    from opendracocli.tui.startup.loader import StartupLoader

    loader = StartupLoader()
    names = loader.list_templates()
    assert "minimal" in names
    assert "figlet" in names
    assert "ascii_art" in names
    assert "neofetch_style" in names


def test_startup_loader_get_missing_raises():
    from opendracocli.tui.startup.loader import StartupLoader

    with pytest.raises(KeyError):
        StartupLoader().get("not_exist")


def test_startup_config_load_default(tmp_path):
    from opendracocli.tui.startup.loader import load_startup_config

    # 不存在文件返回默认
    cfg = load_startup_config(tmp_path / "nofile.toml")
    assert cfg.enabled is True
    assert cfg.template == "draco"


def test_startup_config_load_toml(tmp_path):
    from opendracocli.tui.startup.loader import load_startup_config

    p = tmp_path / "startup.toml"
    p.write_text(
        'enabled = false\ntemplate = "figlet"\nanimation = "typewriter"\nduration_ms = 2000\n',
        encoding="utf-8",
    )
    cfg = load_startup_config(p)
    assert cfg.enabled is False
    assert cfg.template == "figlet"
    assert cfg.animation == "typewriter"
    assert cfg.duration_ms == 2000


def test_startup_animator_frames():
    from opendracocli.tui.startup.animator import StartupAnimator

    assert StartupAnimator.frame("spinner", "hi", 3, 10)  # 非空
    assert "hi" in StartupAnimator.frame("spinner", "hi", 3, 10)
    assert "%" in StartupAnimator.frame("progress", "hi", 5, 20)
    assert StartupAnimator.frame("typewriter", "hello", 2, 5) == "he"
    assert StartupAnimator.frame("none", "hi", 0, 1) == "hi"


def test_startup_animator_total_frames():
    from opendracocli.tui.startup.animator import StartupAnimator

    assert StartupAnimator.total_frames("typewriter", "hello", 1000) == 5
    assert StartupAnimator.total_frames("none", "hi", 1000) == 1
    assert StartupAnimator.total_frames("progress", "hi", 1000) == 20


def test_startup_custom_template_dir(tmp_path):
    """自定义模板文件加载"""
    from opendracocli.tui.startup.loader import StartupLoader

    tpl = tmp_path / "mytpl.py"
    tpl.write_text(
        "def render(ctx):\n    return 'CUSTOM ' + ctx.version\n",
        encoding="utf-8",
    )
    loader = StartupLoader()
    n = loader.register_custom_dir(tmp_path)
    assert n == 1
    assert "mytpl" in loader.list_templates()
    fn = loader.get("mytpl")
    assert "CUSTOM" in fn(_ctx())


# ==================== 渲染协议层 ====================


def test_render_context_kinds():
    from opendracocli.tui.context import (
        KIND_OUTPUT, KIND_ERROR, KIND_BLOCK, RenderContext,
    )

    assert RenderContext(kind=KIND_OUTPUT).kind == "output"
    assert RenderContext(kind=KIND_ERROR).error_code == ""
    assert RenderContext(kind=KIND_BLOCK, blocked=True).blocked is True


def test_context_from_pipeline_result_blocked():
    from opendracocli.tui.context import KIND_BLOCK, context_from_pipeline_result

    class FakeResult:
        blocked = True
        block_reason = "风控"
        raw_input = "rm -rf /"
        mapped_command = ""
        duration_ms = 5
        is_high_risk = True
        error = None
        exit_code = None
        stdout = ""
        stderr = ""

    ctx = context_from_pipeline_result(FakeResult(), session_id="s1", cwd="/tmp")
    assert ctx.kind == KIND_BLOCK
    assert ctx.block_reason == "风控"
    assert ctx.is_high_risk is True


def test_context_from_pipeline_result_error():
    from opendracocli.errors import DracoError
    from opendracocli.tui.context import KIND_ERROR, context_from_pipeline_result

    class FakeResult:
        blocked = False
        raw_input = "bad"
        mapped_command = ""
        duration_ms = 1
        is_high_risk = False
        error = DracoError(code="draco.shell.parse_error", message="语法错")
        exit_code = None
        stdout = ""
        stderr = ""

    ctx = context_from_pipeline_result(FakeResult())
    assert ctx.kind == KIND_ERROR
    assert ctx.error_code == "draco.shell.parse_error"
    assert ctx.error_message == "语法错"


def test_context_from_event():
    from opendracocli.events import Event
    from opendracocli.tui.context import KIND_OUTPUT, context_from_event

    e = Event(type="CommandExecuted", payload={"raw_input": "ls", "exit_code": 0})
    ctx = context_from_event(e)
    assert ctx.kind == KIND_OUTPUT
    assert ctx.raw_input == "ls"


def test_simple_renderer_output(capsys):
    from opendracocli.tui.context import KIND_OUTPUT, RenderContext
    from opendracocli.tui.renderer import SimpleRenderer

    r = SimpleRenderer(use_rich=False)
    r.render(RenderContext(kind=KIND_OUTPUT, stdout="hello\n", exit_code=0))
    assert "hello" in capsys.readouterr().out


def test_simple_renderer_error(capsys):
    from opendracocli.tui.context import KIND_ERROR, RenderContext
    from opendracocli.tui.renderer import SimpleRenderer

    r = SimpleRenderer(use_rich=False)
    r.render(RenderContext(kind=KIND_ERROR, error_code="draco.x", error_message="boom"))
    captured = capsys.readouterr()
    assert "draco.x" in captured.err
    assert "boom" in captured.err


def test_simple_renderer_history(capsys):
    from opendracocli.history.models import CommandRecord
    from opendracocli.tui.renderer import SimpleRenderer

    r = SimpleRenderer(use_rich=False)
    recs = [CommandRecord(raw_input="ls", exit_code=0), CommandRecord(raw_input="bad", exit_code=1)]
    r.render_history(recs)
    out = capsys.readouterr().out
    assert "ls" in out
    assert "bad" in out


def test_renderer_protocol_runtime_checkable():
    from opendracocli.tui.renderer import Renderer, SimpleRenderer

    assert isinstance(SimpleRenderer(use_rich=False), Renderer)


# ==================== 快捷键 ====================


def test_keymap_default():
    from opendracocli.tui.keymap import DEFAULT_KEYMAP, KeymapConfig

    assert "ctrl+t" in DEFAULT_KEYMAP
    cfg = KeymapConfig()
    assert cfg.action_for("ctrl+t") == "switch_theme"
    assert cfg.action_for("not_bound") is None


def test_keymap_load_default_when_missing(tmp_path):
    from opendracocli.tui.keymap import load_keymap

    cfg = load_keymap(tmp_path / "nofile.toml")
    assert "ctrl+l" in cfg.bindings  # 默认仍可用


def test_keymap_load_toml_merge(tmp_path):
    from opendracocli.tui.keymap import load_keymap

    p = tmp_path / "keymap.toml"
    p.write_text('[bindings]\n"ctrl+t" = "clear_output"\n', encoding="utf-8")
    cfg = load_keymap(p)
    assert cfg.bindings["ctrl+t"] == "clear_output"  # 覆盖
    assert "ctrl+l" in cfg.bindings  # 默认仍保留 (合并而非替换)


def test_keymap_keys_for_action():
    from opendracocli.tui.keymap import KeymapConfig

    cfg = KeymapConfig()
    keys = cfg.keys_for_action("quit")
    assert "ctrl+d" in keys


# ==================== config P5 字段 ====================


def test_config_p5_defaults():
    cfg = DracoConfig()
    assert cfg.tui_engine == "textual"
    assert cfg.theme == "draco"
    assert cfg.startup_enabled is True
    assert cfg.startup_template == "draco"
    assert cfg.fallback_simple_in_non_tty is True


def test_config_effective_tui_engine_simple_when_forced():
    cfg = DracoConfig()
    cfg.tui_engine = "simple"
    assert cfg.effective_tui_engine == "simple"


def test_config_resolved_paths():
    cfg = DracoConfig()
    assert cfg.startup_config_resolved.name == "startup.toml"
    assert cfg.keymap_file_resolved.name == "keymap.toml"
    assert cfg.custom_themes_dir_resolved.name == "themes"
