"""character.py 测试 — 命令角色卡"""
import json
from pathlib import Path

import pytest

from opendracocli.ai.character import (
    CommandRole,
    DEFAULT_ROLE,
    RoleRegistry,
    reset_global_registry,
)


@pytest.fixture
def builtin_dir():
    """包内置角色卡目录"""
    from opendracocli.config import DracoConfig

    cfg = DracoConfig()
    return cfg.builtin_roles_dir


@pytest.fixture
def empty_registry():
    """空注册表（无内置无用户）"""
    return RoleRegistry(builtin_dir=None, user_dir=None)


def test_default_role_has_skeleton():
    """默认角色有骨架"""
    assert DEFAULT_ROLE.skeleton
    assert DEFAULT_ROLE.name


def test_load_builtin_roles(builtin_dir):
    """加载内置角色卡"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    roles = reg.list()
    assert "rm" in roles
    assert "git" in roles
    assert "ls" in roles


def test_rm_role_fields(builtin_dir):
    """rm 角色卡字段正确"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    rm = reg.get("rm")
    assert rm is not None
    assert rm.name == "清道夫"
    assert "冷静" in rm.personality_core
    assert rm.speech_style
    assert rm.emotion_profile
    assert "冷静" in rm.emotion_profile


def test_match_exact(builtin_dir):
    """精确匹配"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    role = reg.match("rm")
    assert role.cmd == "rm"


def test_match_prefix(builtin_dir):
    """前缀匹配 git push → git"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    role = reg.match("git")
    assert role.cmd == "git"


def test_match_unknown_returns_default(builtin_dir):
    """无匹配返回默认角色"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    role = reg.match("nonexistent_cmd")
    assert role is DEFAULT_ROLE


def test_user_dir_overrides_builtin(tmp_path, builtin_dir):
    """用户角色覆盖内置"""
    # 自定义 rm 角色
    user_rm = {
        "name": "我的清道夫",
        "cmd": "rm",
        "skeleton": "自定义骨架",
        "personality": {"core": ["自定义"], "description": "测试"},
    }
    (tmp_path / "rm.json").write_text(json.dumps(user_rm), encoding="utf-8")
    reg = RoleRegistry(builtin_dir=builtin_dir, user_dir=tmp_path)
    rm = reg.get("rm")
    assert rm.name == "我的清道夫"
    assert rm.skeleton == "自定义骨架"


def test_user_dir_adds_new_role(tmp_path, builtin_dir):
    """用户目录添加新角色"""
    user_role = {
        "name": "网络管家",
        "cmd": "curl",
        "skeleton": "你是网络管家...",
        "personality": {"core": ["细心"], "description": "管网络"},
    }
    (tmp_path / "curl.json").write_text(json.dumps(user_role), encoding="utf-8")
    reg = RoleRegistry(builtin_dir=builtin_dir, user_dir=tmp_path)
    curl = reg.get("curl")
    assert curl is not None
    assert curl.name == "网络管家"


def test_invalid_role_skipped(tmp_path):
    """无效角色卡被跳过"""
    # 缺 skeleton
    (tmp_path / "bad.json").write_text(
        json.dumps({"name": "无骨架", "cmd": "bad"}), encoding="utf-8"
    )
    reg = RoleRegistry(builtin_dir=None, user_dir=tmp_path)
    assert reg.get("bad") is None


def test_invalid_json_skipped(tmp_path):
    """非法 JSON 被跳过"""
    (tmp_path / "broken.json").write_text("not json{", encoding="utf-8")
    reg = RoleRegistry(builtin_dir=None, user_dir=tmp_path)
    # 不抛异常
    assert reg.list() == {}


def test_nonexistent_dir_ok():
    """不存在的目录不抛异常"""
    reg = RoleRegistry(builtin_dir=Path("/nonexistent/path"), user_dir=None)
    assert reg.list() == {}


def test_get_case_insensitive(builtin_dir):
    """get 大小写不敏感"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    assert reg.get("RM") is not None
    assert reg.get("Rm") is not None


def test_match_case_insensitive(builtin_dir):
    """match 大小写不敏感"""
    reg = RoleRegistry(builtin_dir=builtin_dir)
    role = reg.match("RM")
    assert role.cmd == "rm"


def test_global_registry_singleton():
    """全局注册表单例"""
    from opendracocli.ai.character import get_global_registry, reset_global_registry

    reset_global_registry()
    r1 = get_global_registry()
    r2 = get_global_registry()
    assert r1 is r2
    reset_global_registry()
