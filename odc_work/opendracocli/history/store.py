"""SQLite 历史存储

中等集粒度：命令+时间+退出码+cwd+输出摘要+映射对+高危标记+别名+会话。
WAL 模式以支持 P4 多通道并发写。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from ..config import DracoConfig, get_global_config
from ..errors import ERR_HISTORY_DB_ERROR, make_error
from ..logger import get_logger
from .models import CommandRecord

log = get_logger("history.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_input       TEXT    NOT NULL,
    canonical_ir    TEXT,
    mapped_command  TEXT,
    platform        TEXT    NOT NULL,
    cwd             TEXT    NOT NULL,
    exit_code       INTEGER,
    started_at      REAL    NOT NULL,
    duration_ms     INTEGER,
    stdout_summary  TEXT,
    stderr_summary  TEXT,
    is_high_risk    INTEGER DEFAULT 0,
    alias_used      TEXT,
    session_id      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_started_at ON commands(started_at);
CREATE INDEX IF NOT EXISTS idx_commands_session    ON commands(session_id);
CREATE INDEX IF NOT EXISTS idx_commands_raw_input  ON commands(raw_input);
"""

_INSERT_SQL = """
INSERT INTO commands
    (raw_input, canonical_ir, mapped_command, platform, cwd,
     exit_code, started_at, duration_ms, stdout_summary, stderr_summary,
     is_high_risk, alias_used, session_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _summarize(text: str, max_lines: int) -> str:
    """取前 max_lines 行作为摘要"""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"


class HistoryStore:
    """SQLite 历史存储"""

    def __init__(self, config: DracoConfig | None = None) -> None:
        self._config = config or get_global_config()
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_db()

    def _ensure_db(self) -> None:
        db_path = self._config.history_db_resolved
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit
            )
            # 开启 WAL 以支持后续多通道并发
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            self._conn.executescript(_SCHEMA)
            log.debug("history db ready: %s", db_path)
        except sqlite3.Error as e:
            raise make_error(
                ERR_HISTORY_DB_ERROR,
                detail={"path": str(db_path)},
                reason=str(e),
            ) from e

    def record(
        self,
        raw_input: str,
        canonical_ir: str,
        mapped_command: str,
        platform: str,
        cwd: str,
        exit_code: Optional[int],
        started_at: float,
        duration_ms: int,
        stdout: str = "",
        stderr: str = "",
        is_high_risk: bool = False,
        alias_used: Optional[str] = None,
        session_id: str = "",
    ) -> int:
        """写入一条历史记录，返回 id"""
        stdout_summary = _summarize(stdout, self._config.stdout_summary_lines)
        stderr_summary = _summarize(stderr, self._config.stderr_summary_lines)
        params = (
            raw_input, canonical_ir, mapped_command, platform, cwd,
            exit_code, started_at, duration_ms, stdout_summary, stderr_summary,
            1 if is_high_risk else 0, alias_used, session_id,
        )
        with self._lock:
            assert self._conn is not None
            try:
                cur = self._conn.execute(_INSERT_SQL, params)
                rid = cur.lastrowid
                log.debug("recorded history id=%s raw=%r", rid, raw_input[:50])
                return rid or 0
            except sqlite3.Error as e:
                raise make_error(
                    ERR_HISTORY_DB_ERROR,
                    detail={"raw_input": raw_input},
                    reason=str(e),
                ) from e

    def recent(self, limit: int = 50, session_id: Optional[str] = None) -> List[CommandRecord]:
        """查最近 N 条"""
        sql = "SELECT * FROM commands"
        params: list = []
        if session_id:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            return [CommandRecord.from_row(r) for r in cur.fetchall()]

    def search(self, keyword: str, limit: int = 50) -> List[CommandRecord]:
        """按 raw_input 关键词搜索"""
        sql = (
            "SELECT * FROM commands WHERE raw_input LIKE ? "
            "ORDER BY started_at DESC LIMIT ?"
        )
        params = (f"%{keyword}%", limit)
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            return [CommandRecord.from_row(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


_global_store: HistoryStore | None = None


def get_global_store() -> HistoryStore:
    global _global_store
    if _global_store is None:
        _global_store = HistoryStore()
    return _global_store


def set_global_store(store: HistoryStore) -> None:
    global _global_store
    _global_store = store
