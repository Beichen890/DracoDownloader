"""
进度持久化管理
"""

import json
import time
from pathlib import Path
from typing import Dict, Optional, Any
from dataclasses import dataclass, field


@dataclass
class ProgressData:
    """进度数据"""
    task_id: str
    downloaded: int = 0
    total: int = 0
    speed: int = 0
    progress: float = 0.0
    updated_at: float = field(default_factory=time.time)
    chunks: list[bool] = field(default_factory=list)


class ProgressManager:
    """进度管理器 - 持久化到 .progress 文件"""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or Path(".download_progress")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, ProgressData] = {}

    def update(self, task_id: str, downloaded: int, total: int, speed: int = 0):
        """更新进度"""
        if task_id not in self._cache:
            self._cache[task_id] = ProgressData(task_id=task_id)

        data = self._cache[task_id]
        data.downloaded = downloaded
        data.total = total
        data.speed = speed
        data.progress = (downloaded / total * 100) if total > 0 else 0
        data.updated_at = time.time()

        self._save(task_id)

    def get(self, task_id: str) -> Optional[ProgressData]:
        """获取进度"""
        if task_id in self._cache:
            return self._cache[task_id]
        return self._load(task_id)

    def delete(self, task_id: str):
        """删除进度"""
        self._cache.pop(task_id, None)
        file_path = self.storage_dir / f"{task_id}.json"
        file_path.unlink(missing_ok=True)

    def _save(self, task_id: str):
        """保存到文件"""
        data = self._cache.get(task_id)
        if data is None:
            return

        file_path = self.storage_dir / f"{task_id}.json"
        with open(file_path, 'w') as f:
            json.dump({
                'task_id': data.task_id,
                'downloaded': data.downloaded,
                'total': data.total,
                'speed': data.speed,
                'progress': data.progress,
                'updated_at': data.updated_at,
                'chunks': data.chunks
            }, f)

    def _load(self, task_id: str) -> Optional[ProgressData]:
        """从文件加载"""
        file_path = self.storage_dir / f"{task_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path) as f:
                data = json.load(f)
            return ProgressData(
                task_id=data['task_id'],
                downloaded=data.get('downloaded', 0),
                total=data.get('total', 0),
                speed=data.get('speed', 0),
                progress=data.get('progress', 0.0),
                updated_at=data.get('updated_at', time.time()),
                chunks=data.get('chunks', [])
            )
        except (json.JSONDecodeError, KeyError, FileNotFoundError, OSError):
            return None

    def list_active(self) -> list[str]:
        """列出所有活动任务"""
        return [f.stem for f in self.storage_dir.glob("*.json") if f.suffix == '.json']
