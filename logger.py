"""
DracoDownloader 日志系统
提供模块级日志输出，支持控制台和文件
"""

import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional


_LOG_CONFIGURED = False


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    """
    获取模块级日志器

    Args:
        name: 模块名 (如 'core', 'bittorrent.downloader')
        level: 日志级别，默认从环境变量 DRACO_LOG_LEVEL 读取

    Returns:
        logging.Logger 实例
    """
    global _LOG_CONFIGURED

    if not _LOG_CONFIGURED:
        _configure_root()
        _LOG_CONFIGURED = True

    logger = logging.getLogger(f'DracoDownloader.{name}')

    if level is not None:
        logger.setLevel(level)

    return logger


def _configure_root():
    """配置根日志器"""
    log_level_name = os.environ.get('DRACO_LOG_LEVEL', 'INFO').upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    root = logging.getLogger('DracoDownloader')
    root.setLevel(log_level)
    root.handlers.clear()

    # 控制台 handler (stderr，避免干扰 stdout 输出)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    ))
    root.addHandler(console)

    # 文件 handler (可选，通过 DRACO_LOG_FILE 环境变量控制)
    log_file = os.environ.get('DRACO_LOG_FILE', '')
    if log_file:
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(log_path), maxBytes=10*1024*1024, backupCount=3
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s'
            ))
            root.addHandler(file_handler)
        except (OSError, PermissionError):
            pass
