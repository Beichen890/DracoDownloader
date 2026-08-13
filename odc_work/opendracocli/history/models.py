"""历史数据模型"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CommandRecord:
    """一条命令历史记录（对应 SQLite 一行）

    Attributes:
        id: 自增主键（None 表示尚未入库）
        raw_input: 用户原始输入
        canonical_ir: 归一化 IR 的 JSON
        mapped_command: 映射+展开后最终交给 shell 的命令
        platform: 'win' | 'linux' | 'darwin'
        cwd: 执行时工作目录
        exit_code: 进程退出码（None 表示未执行/被取消）
        started_at: Unix 时间戳
        duration_ms: 耗时毫秒
        stdout_summary: stdout 前 N 行摘要
        stderr_summary: stderr 前 N 行摘要
        is_high_risk: P1 恒 False，P2 风控填充
        alias_used: 命中的别名名，None 表示无别名
        session_id: 会话 ID（TUI 启动生成）
    """

    id: Optional[int] = None
    raw_input: str = ""
    canonical_ir: str = ""
    mapped_command: str = ""
    platform: str = ""
    cwd: str = ""
    exit_code: Optional[int] = None
    started_at: float = 0.0
    duration_ms: int = 0
    stdout_summary: str = ""
    stderr_summary: str = ""
    is_high_risk: bool = False
    alias_used: Optional[str] = None
    session_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # alias_used None → SQL NULL
        return d

    @classmethod
    def from_row(cls, row: tuple) -> "CommandRecord":
        """从 SQLite 行构造"""
        return cls(
            id=row[0],
            raw_input=row[1],
            canonical_ir=row[2] or "",
            mapped_command=row[3] or "",
            platform=row[4],
            cwd=row[5],
            exit_code=row[6],
            started_at=row[7],
            duration_ms=row[8] or 0,
            stdout_summary=row[9] or "",
            stderr_summary=row[10] or "",
            is_high_risk=bool(row[11]),
            alias_used=row[12],
            session_id=row[13],
        )
