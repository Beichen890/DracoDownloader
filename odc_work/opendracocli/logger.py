"""OpenDracoCLI 日志

结构化日志，参考 DracoHub DebugLogger 的轻量风格。
默认输出到 stderr，不影响 TUI 的 stdout 渲染。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _init_logging() -> None:
    global _initialized
    if _initialized:
        return
    level_name = os.environ.get("DRACO_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root = logging.getLogger("opendracocli")
    root.setLevel(level)
    # 避免重复 handler
    if not root.handlers:
        root.addHandler(handler)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """获取子 logger

    Args:
        name: 模块名（如 'shell.parser'）

    Returns:
        logging.Logger 实例
    """
    _init_logging()
    if name.startswith("opendracocli"):
        return logging.getLogger(name)
    return logging.getLogger(f"opendracocli.{name}")


def set_log_file(path: str | Path) -> None:
    """额外把日志写入文件（可选，调试用）"""
    _init_logging()
    root = logging.getLogger("opendracocli")
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    root.addHandler(fh)
