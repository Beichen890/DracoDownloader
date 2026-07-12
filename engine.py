"""
下载引擎 - 管理活跃下载生命周期
"""

import asyncio
import time
from typing import Dict, Any, Optional

from .logger import get_logger

log = get_logger('engine')


class DownloadEngine:
    """下载引擎"""

    def __init__(self):
        self._active: Dict[str, dict] = {}
        self._paused: Dict[str, dict] = {}

    def start(self, task_id: str, handle) -> bool:
        if task_id in self._active:
            log.warning(f"Task {task_id} already active")
            return False
        self._active[task_id] = {
            'handle': handle,
            'started_at': time.time(),
            'progress': 0.0,
        }
        log.debug(f"Task {task_id} started in engine")
        return True

    def pause(self, task_id: str) -> bool:
        if task_id in self._active:
            self._paused[task_id] = self._active.pop(task_id)
            log.debug(f"Task {task_id} paused")
            return True
        return False

    def resume(self, task_id: str) -> bool:
        if task_id in self._paused:
            self._active[task_id] = self._paused.pop(task_id)
            log.debug(f"Task {task_id} resumed")
            return True
        return False

    def stop(self, task_id: str):
        self._active.pop(task_id, None)
        self._paused.pop(task_id, None)
        log.debug(f"Task {task_id} stopped")

    def update_progress(self, task_id: str, progress: float):
        if task_id in self._active:
            self._active[task_id]['progress'] = progress

    def get_active(self) -> Dict[str, dict]:
        return dict(self._active)

    def is_active(self, task_id: str) -> bool:
        return task_id in self._active

    def active_count(self) -> int:
        return len(self._active)
