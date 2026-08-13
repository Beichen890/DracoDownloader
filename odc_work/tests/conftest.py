"""pytest fixtures"""
import os
import sys
from pathlib import Path

# 确保能 import opendracocli
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from opendracocli.config import DracoConfig, set_global_config
from opendracocli.events import EventBus, set_global_bus
from opendracocli.hooks.registry import HookRegistry, set_global_registry


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """隔离的配置：历史 DB、别名文件都用 tmp_path"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        # 映射表用包内自带
    )
    set_global_config(cfg)
    return cfg


@pytest.fixture
def fresh_bus():
    """每次测试全新的 EventBus"""
    bus = EventBus()
    set_global_bus(bus)
    return bus


@pytest.fixture
def fresh_registry():
    """每次测试全新的 HookRegistry"""
    reg = HookRegistry()
    set_global_registry(reg)
    return reg
