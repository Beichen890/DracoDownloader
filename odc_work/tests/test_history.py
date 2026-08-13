"""history 测试"""
import time

import pytest

from opendracocli.config import DracoConfig
from opendracocli.errors import DracoError
from opendracocli.history.store import HistoryStore


@pytest.fixture
def store(tmp_path):
    cfg = DracoConfig(history_db_path=str(tmp_path / "test_history.db"))
    return HistoryStore(config=cfg)


def test_record_and_recent(store):
    rid = store.record(
        raw_input="ls -la",
        canonical_ir="{}",
        mapped_command="dir",
        platform="linux",
        cwd="/tmp",
        exit_code=0,
        started_at=time.time(),
        duration_ms=10,
        stdout="file1\nfile2",
        stderr="",
        session_id="sess1",
    )
    assert rid > 0

    recs = store.recent(limit=10, session_id=None)
    assert len(recs) == 1
    assert recs[0].raw_input == "ls -la"
    assert recs[0].exit_code == 0
    assert recs[0].platform == "linux"
    assert recs[0].stdout_summary == "file1\nfile2"


def test_summary_truncation(store):
    """stdout 超过 max_lines 被截断"""
    long_output = "\n".join(f"line{i}" for i in range(50))
    store.record(
        raw_input="big",
        canonical_ir="",
        mapped_command="big",
        platform="linux",
        cwd="/tmp",
        exit_code=0,
        started_at=time.time(),
        duration_ms=5,
        stdout=long_output,
        stderr="",
        session_id="s1",
    )
    recs = store.recent(limit=10, session_id=None)
    assert "more lines" in recs[0].stdout_summary
    assert len(recs[0].stdout_summary) < len(long_output)


def test_search(store):
    store.record(
        raw_input="git status", canonical_ir="", mapped_command="git status",
        platform="linux", cwd="/tmp", exit_code=0, started_at=time.time(),
        duration_ms=5, stdout="", stderr="", session_id="s1",
    )
    store.record(
        raw_input="ls -la", canonical_ir="", mapped_command="dir",
        platform="linux", cwd="/tmp", exit_code=0, started_at=time.time(),
        duration_ms=5, stdout="", stderr="", session_id="s1",
    )

    results = store.search("git")
    assert len(results) == 1
    assert results[0].raw_input == "git status"


def test_filter_by_session(store):
    store.record(
        raw_input="a", canonical_ir="", mapped_command="a",
        platform="linux", cwd="/tmp", exit_code=0, started_at=time.time(),
        duration_ms=5, stdout="", stderr="", session_id="sess_a",
    )
    store.record(
        raw_input="b", canonical_ir="", mapped_command="b",
        platform="linux", cwd="/tmp", exit_code=0, started_at=time.time(),
        duration_ms=5, stdout="", stderr="", session_id="sess_b",
    )

    recs = store.recent(limit=10, session_id="sess_a")
    assert len(recs) == 1
    assert recs[0].raw_input == "a"


def test_alias_used_stored(store):
    store.record(
        raw_input="ll", canonical_ir="", mapped_command="ls -la",
        platform="linux", cwd="/tmp", exit_code=0, started_at=time.time(),
        duration_ms=5, stdout="", stderr="", alias_used="ll", session_id="s1",
    )
    recs = store.recent(limit=10, session_id=None)
    assert recs[0].alias_used == "ll"


def test_high_risk_flag(store):
    store.record(
        raw_input="rm -rf /", canonical_ir="", mapped_command="rm -rf /",
        platform="linux", cwd="/", exit_code=0, started_at=time.time(),
        duration_ms=5, stdout="", stderr="", is_high_risk=True, session_id="s1",
    )
    recs = store.recent(limit=10, session_id=None)
    assert recs[0].is_high_risk is True


def test_failed_command_exit_code_none(store):
    """被取消/未执行的命令 exit_code=None"""
    store.record(
        raw_input="bad", canonical_ir="", mapped_command="",
        platform="linux", cwd="/tmp", exit_code=None, started_at=time.time(),
        duration_ms=5, stdout="", stderr="parse error", session_id="s1",
    )
    recs = store.recent(limit=10, session_id=None)
    assert recs[0].exit_code is None
