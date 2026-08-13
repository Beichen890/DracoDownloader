"""Draco 内置函数扩展测试

覆盖新增的 ftar/fzip/fgrep/fhash/fjson，以及 stdin 透传。
"""
from __future__ import annotations

import json
import os
import tarfile
import zipfile

import pytest

from opendracocli.aliases.manager import AliasManager
from opendracocli.agent.function_registry import FunctionRegistry
from opendracocli.agent.python_executor import PythonChannelExecutor
from opendracocli.config import DracoConfig
from opendracocli.draco_funcs.registry import register_builtins
from opendracocli.hooks.registry import HookRegistry
from opendracocli.history.store import HistoryStore
from opendracocli.shell.pipeline import ShellPipeline

pytestmark = pytest.mark.asyncio


class _StubAliasManager(AliasManager):
    def __init__(self, aliases: dict[str, str] | None = None):
        self._config = DracoConfig()
        self._aliases = dict(aliases or {})

    def _load(self) -> None:
        pass

    def _save(self) -> None:
        pass


@pytest.fixture
def pipeline(tmp_path):
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "hist.db"),
        aliases_file=str(tmp_path / "aliases.json"),
    )
    aliases = _StubAliasManager({})
    history = HistoryStore(config=cfg)
    hooks = HookRegistry()
    pipe = ShellPipeline(
        config=cfg,
        alias_manager=aliases,
        history_store=history,
        hook_registry=hooks,
    )
    # 注册所有内置函数
    registry = FunctionRegistry()
    register_builtins(registry)
    py_exec = PythonChannelExecutor(config=cfg, history_store=history)
    pipe.set_function_registry(registry)
    pipe.set_python_executor(py_exec)
    return pipe, history, aliases, hooks, cfg


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld\n")
    (tmp_path / "b.txt").write_text("foo\nbar\nbaz\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("nested\n")
    (tmp_path / "data.json").write_text(
        json.dumps({"users": [{"name": "alice"}, {"name": "bob"}], "count": 2})
    )
    return tmp_path


# ===== ftar / fzip =====


async def test_ftar_pack_and_extract(pipeline, workdir):
    pipe, *_ = pipeline
    archive = workdir / "out.tar"
    # 打包（sources 用引号包裹，多文件空格分隔）
    r = await pipe.run(
        f'ftar {archive} "a.txt b.txt"', session_id="s1", cwd=str(workdir)
    )
    assert r.success
    assert archive.exists()
    # 解压到 out/
    out_dir = workdir / "out"
    r2 = await pipe.run(
        f"ftar {archive} extract=true dest={out_dir}",
        session_id="s1",
        cwd=str(workdir),
    )
    assert r2.success
    assert (out_dir / "a.txt").exists()
    assert (out_dir / "b.txt").exists()


async def test_ftar_gzip(pipeline, workdir):
    pipe, *_ = pipeline
    archive = workdir / "out.tar.gz"
    r = await pipe.run(
        f"ftar {archive} a.txt gzip=true", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # 验证是 gzip 格式
    with open(archive, "rb") as fh:
        assert fh.read(2) == b"\x1f\x8b"


async def test_ftar_missing_source(pipeline, workdir):
    pipe, *_ = pipeline
    archive = workdir / "out.tar"
    r = await pipe.run(
        f"ftar {archive} nonexist.txt", session_id="s1", cwd=str(workdir)
    )
    # 警告但不致命
    assert r.success


async def test_fzip_pack_and_extract(pipeline, workdir):
    pipe, *_ = pipeline
    archive = workdir / "out.zip"
    # 打包目录
    r = await pipe.run(
        f"fzip {archive} sub", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    assert archive.exists()
    # 解压
    out_dir = workdir / "out"
    r2 = await pipe.run(
        f"fzip {archive} extract=true dest={out_dir}",
        session_id="s1",
        cwd=str(workdir),
    )
    assert r2.success
    assert (out_dir / "sub" / "c.txt").exists()


# ===== fgrep =====


async def test_fgrep_file(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fgrep hello path=a.txt", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    assert r.exit_code == 0  # 有匹配
    assert "hello" in r.stdout


async def test_fgrep_no_match(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fgrep nomatch path=a.txt", session_id="s1", cwd=str(workdir)
    )
    # 管线成功，但 exit_code=1（无匹配，对齐 grep）
    assert r.success
    assert r.exit_code == 1


async def test_fgrep_directory_recursive(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fgrep nested path=.", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # 命中 sub/c.txt 中的 nested
    assert "nested" in r.stdout
    # 输出包含文件名前缀
    assert "sub" in r.stdout or "c.txt" in r.stdout


async def test_fgrep_line_numbers(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fgrep foo path=b.txt line_numbers=true", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # 输出含行号
    assert "1:" in r.stdout or ":1:" in r.stdout


async def test_fgrep_ignore_case(pipeline, workdir):
    pipe, *_ = pipeline
    # 写入混合大小写内容
    (workdir / "mixed.txt").write_text("Hello\nWORLD\n")
    r = await pipe.run(
        "fgrep hello path=mixed.txt ignore_case=true",
        session_id="s1",
        cwd=str(workdir),
    )
    assert r.success
    assert "Hello" in r.stdout


async def test_fgrep_invert(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fgrep hello path=a.txt invert=true", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # 反向：应输出 world 而非 hello
    assert "world" in r.stdout
    assert "hello" not in r.stdout.replace("a.txt", "").replace(":", "")


async def test_fgrep_count(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fgrep . path=b.txt count=true regex=true",
        session_id="s1",
        cwd=str(workdir),
    )
    assert r.success
    # b.txt 有 3 行
    assert "3" in r.stdout


async def test_fgrep_stdin_from_pipe(pipeline, workdir):
    """管道：echo 内容 | fgrep"""
    pipe, *_ = pipeline
    r = await pipe.run(
        "echo hello | fgrep hello", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    assert "hello" in r.stdout


# ===== fhash =====


async def test_fhash_sha256(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fhash a.txt", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # SHA-256 是 64 字符十六进制
    first_line = r.stdout.strip().split("\n")[0].split()[0]
    assert len(first_line) == 64
    assert all(c in "0123456789abcdef" for c in first_line)


async def test_fhash_md5(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fhash a.txt algo=md5", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    first_line = r.stdout.strip().split("\n")[0].split()[0]
    # MD5 是 32 字符
    assert len(first_line) == 32


async def test_fhash_consistent(pipeline, workdir):
    pipe, *_ = pipeline
    r1 = await pipe.run("fhash a.txt", session_id="s1", cwd=str(workdir))
    r2 = await pipe.run("fhash a.txt", session_id="s1", cwd=str(workdir))
    assert r1.stdout == r2.stdout


# ===== fjson =====


async def test_fjson_pretty_print(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fjson data.json", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # 美化输出含缩进
    assert "  " in r.stdout
    assert "users" in r.stdout


async def test_fjson_query(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fjson data.json query=count", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    assert "2" in r.stdout


async def test_fjson_query_array_index(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fjson data.json query=users.0.name", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    assert "alice" in r.stdout


async def test_fjson_stdin_from_pipe(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run(
        'echo \'{"a":{"b":1}}\' | fjson query=a.b', session_id="s1"
    )
    assert r.success
    assert "1" in r.stdout


async def test_fjson_raw_output(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run(
        "fjson data.json raw=true", session_id="s1", cwd=str(workdir)
    )
    assert r.success
    # 紧凑：无换行缩进
    assert "  " not in r.stdout.replace("\n", "")


# ===== 注册完整性 =====


async def test_all_new_functions_registered(pipeline):
    """ftar/fzip/fgrep/fhash/fjson 都应已注册"""
    pipe, history, aliases, hooks, cfg = pipeline
    # 通过 pipe 内部拿到 registry（间接验证：能命中）
    for name in ("ftar", "fzip", "fgrep", "fhash", "fjson"):
        r = await pipe.run(f"{name}", session_id="s1")
        # 没参数应触发参数错误或正常返回（非 127 = 命令未找到）
        assert r.exit_code != 127, f"{name} 未被路由到 Python 通道"
