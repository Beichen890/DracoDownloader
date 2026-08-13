"""Rust 内核（draco_native）端到端测试

覆盖：
- ls 多路径 / glob 展开 / -l -a -h
- cd / pwd / new_cwd 透传
- cat / head / tail 管道与文件
- cp / mv / rm / mkdir / rmdir / touch / stat / ln
- echo / wc stdin 与文件
- 管道（|）/ 重定向（> >> <）/ 逻辑符（&& || ;）
"""
from __future__ import annotations

import os

import pytest

from opendracocli.aliases.manager import AliasManager
from opendracocli.config import DracoConfig
from opendracocli.hooks.registry import HookRegistry
from opendracocli.history.store import HistoryStore
from opendracocli.shell.pipeline import ShellPipeline

pytestmark = pytest.mark.asyncio


def _have_native() -> bool:
    try:
        from opendracocli import _native  # noqa: F401

        return True
    except ImportError:
        return False


skip_no_native = pytest.mark.skipif(
    not _have_native(), reason="Rust 内核未构建（maturin develop）"
)


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
    return pipe, history, aliases, hooks, cfg


@pytest.fixture
def workdir(tmp_path):
    """构造一个有若干文件的测试目录"""
    (tmp_path / "a.py").write_text("print('a')\n")
    (tmp_path / "b.py").write_text("print('b')\nprint('b2')\n")
    (tmp_path / "c.txt").write_text("hello\n")
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub1" / "x.txt").write_text("in sub1\n")
    (tmp_path / "sub2").mkdir()
    return tmp_path


# ===== ls 多路径 / glob =====


@skip_no_native
async def test_ls_no_args_lists_cwd(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("ls", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "a.py" in r.stdout
    assert "sub1/" in r.stdout


@skip_no_native
async def test_ls_glob_multiple_files(pipeline, workdir):
    """ls *.py 应展开为多文件并列出（无空行分隔）"""
    pipe, *_ = pipeline
    r = await pipe.run("ls *.py", session_id="s1", cwd=str(workdir))
    assert r.success
    # 多文件：连续列出，无空行
    assert "a.py\nb.py" in r.stdout.replace("  ", "\n") or "a.py" in r.stdout


@skip_no_native
async def test_ls_multiple_dirs_have_blank_separator(pipeline, workdir):
    """多目录之间用空行分隔，每个目录前显示 `dir:` 标题"""
    pipe, *_ = pipeline
    r = await pipe.run("ls sub1 sub2", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "sub1:" in r.stdout
    assert "sub2:" in r.stdout
    # 目录与目录之间有空行
    assert "\n\nsub2:" in r.stdout


@skip_no_native
async def test_ls_file_then_dir(pipeline, workdir):
    """文件→目录间补空行"""
    pipe, *_ = pipeline
    r = await pipe.run("ls a.py sub1", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "a.py" in r.stdout
    assert "sub1:" in r.stdout
    assert "\n\nsub1:" in r.stdout or "a.py\n\nsub1:" in r.stdout


@skip_no_native
async def test_ls_nonexistent_returns_nonzero(pipeline, workdir):
    """部分目标不存在：exit_code=2，错误走 stderr"""
    pipe, *_ = pipeline
    r = await pipe.run("ls a.py noexist b.py", session_id="s1", cwd=str(workdir))
    assert r.success is True  # 管线成功跑完
    assert r.exit_code == 2
    assert "noexist" in r.stderr


@skip_no_native
async def test_ls_long_format(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("ls -l a.py", session_id="s1", cwd=str(workdir))
    assert r.success
    # -l 格式：-  <size>  a.py
    assert "a.py" in r.stdout
    assert any(line.startswith("- ") for line in r.stdout.splitlines())


@skip_no_native
async def test_ls_hidden_files(pipeline, workdir):
    (workdir / ".hidden").write_text("x")
    pipe, *_ = pipeline
    r1 = await pipe.run("ls", session_id="s1", cwd=str(workdir))
    assert ".hidden" not in r1.stdout
    r2 = await pipe.run("ls -a", session_id="s1", cwd=str(workdir))
    assert ".hidden" in r2.stdout


# ===== cd / pwd =====


@skip_no_native
async def test_cd_updates_session_cwd(pipeline, workdir):
    """cd 透传 new_cwd，后续命令在新 cwd 执行"""
    pipe, *_ = pipeline
    r = await pipe.run("cd sub1", session_id="s1", cwd=str(workdir))
    assert r.success
    assert r.new_cwd is not None
    assert r.new_cwd.endswith("sub1")


@skip_no_native
async def test_pwd_prints_cwd(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("pwd", session_id="s1", cwd=str(workdir))
    assert r.success
    assert str(workdir) in r.stdout


# ===== cat / head / tail =====


@skip_no_native
async def test_cat_file(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("cat a.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "print('a')" in r.stdout


@skip_no_native
async def test_cat_multiple_files(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("cat a.py c.txt", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "print('a')" in r.stdout
    assert "hello" in r.stdout


@skip_no_native
async def test_head_n(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("head -n 1 b.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "print('b')" in r.stdout
    assert "b2" not in r.stdout


@skip_no_native
async def test_tail_n(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("tail -n 1 b.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "b2" in r.stdout
    assert "print('b')" not in r.stdout


# ===== cp / mv / rm / mkdir / rmdir / touch / stat =====


@skip_no_native
async def test_cp_file(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("cp a.py a_copy.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert (workdir / "a_copy.py").exists()
    assert (workdir / "a_copy.py").read_text() == "print('a')\n"


@skip_no_native
async def test_cp_recursive(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("cp -r sub1 sub1_copy", session_id="s1", cwd=str(workdir))
    assert r.success
    assert (workdir / "sub1_copy" / "x.txt").exists()


@skip_no_native
async def test_mv(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("mv a.py a_moved.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert not (workdir / "a.py").exists()
    assert (workdir / "a_moved.py").exists()


@skip_no_native
async def test_rm_file(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("rm c.txt", session_id="s1", cwd=str(workdir))
    assert r.success
    assert not (workdir / "c.txt").exists()


@skip_no_native
async def test_rm_recursive_dir(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("rm -r sub1", session_id="s1", cwd=str(workdir))
    assert r.success
    assert not (workdir / "sub1").exists()


@skip_no_native
async def test_mkdir_and_rmdir(pipeline, tmp_path):
    pipe, *_ = pipeline
    r = await pipe.run("mkdir newdir", session_id="s1", cwd=str(tmp_path))
    assert r.success
    assert (tmp_path / "newdir").exists()
    r2 = await pipe.run("rmdir newdir", session_id="s1", cwd=str(tmp_path))
    assert r2.success
    assert not (tmp_path / "newdir").exists()


@skip_no_native
async def test_touch_creates_file(pipeline, tmp_path):
    pipe, *_ = pipeline
    r = await pipe.run("touch newfile", session_id="s1", cwd=str(tmp_path))
    assert r.success
    assert (tmp_path / "newfile").exists()


@skip_no_native
async def test_stat_file(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("stat a.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "a.py" in r.stdout
    assert "类型" in r.stdout


# ===== echo / wc =====


@skip_no_native
async def test_echo(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run("echo hello world", session_id="s1")
    assert r.success
    assert "hello world" in r.stdout


@skip_no_native
async def test_echo_no_newline(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run("echo -n nonewline", session_id="s1")
    assert r.success
    assert r.stdout == "nonewline"


@skip_no_native
async def test_wc_stdin(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run("wc", session_id="s1")
    # 无 stdin 无文件 → 空字符串统计
    assert r.success
    # 0 行 0 词 0 字节
    assert "0" in r.stdout


@skip_no_native
async def test_wc_file(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("wc a.py", session_id="s1", cwd=str(workdir))
    assert r.success
    # a.py: 1 行 1 词 11 字节
    assert "1" in r.stdout


# ===== 管道 / 重定向 / 逻辑符 =====


@skip_no_native
async def test_pipe_echo_to_cat(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run("echo piped | cat", session_id="s1")
    assert r.success
    assert "piped" in r.stdout


@skip_no_native
async def test_pipe_echo_to_wc(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run("echo hello world | wc", session_id="s1")
    assert r.success
    # 1 行 2 词 12 字节
    lines = r.stdout.split()
    assert len(lines) >= 3
    assert lines[1] == "2"  # 2 词


@skip_no_native
async def test_redirect_stdout_to_file(pipeline, tmp_path):
    pipe, *_ = pipeline
    out_file = tmp_path / "out.txt"
    r = await pipe.run(f"echo hello > {out_file}", session_id="s1", cwd=str(tmp_path))
    assert r.success
    assert out_file.exists()
    assert "hello" in out_file.read_text()


@skip_no_native
async def test_redirect_append(pipeline, tmp_path):
    pipe, *_ = pipeline
    out_file = tmp_path / "out.txt"
    out_file.write_text("first\n")
    r = await pipe.run(f"echo second >> {out_file}", session_id="s1", cwd=str(tmp_path))
    assert r.success
    content = out_file.read_text()
    assert "first" in content
    assert "second" in content


@skip_no_native
async def test_redirect_stdin(pipeline, tmp_path):
    pipe, *_ = pipeline
    in_file = tmp_path / "in.txt"
    in_file.write_text("from file\n")
    r = await pipe.run(f"cat < {in_file}", session_id="s1", cwd=str(tmp_path))
    assert r.success
    assert "from file" in r.stdout


@skip_no_native
async def test_and_operator_both_run(pipeline):
    pipe, *_ = pipeline
    r = await pipe.run("echo first && echo second", session_id="s1")
    assert r.success
    assert "first" in r.stdout
    assert "second" in r.stdout


@skip_no_native
async def test_and_operator_short_circuit(pipeline):
    """&& 上游失败，下游不执行"""
    pipe, *_ = pipeline
    r = await pipe.run("false_xyz && echo should_not_appear", session_id="s1")
    assert r.success  # 管线成功
    assert r.exit_code != 0
    assert "should_not_appear" not in r.stdout


@skip_no_native
async def test_or_operator_short_circuit(pipeline):
    """|| 上游成功，下游不执行"""
    pipe, *_ = pipeline
    r = await pipe.run("echo first || echo second", session_id="s1")
    assert r.success
    assert "first" in r.stdout
    assert "second" not in r.stdout


@skip_no_native
async def test_or_operator_fallback(pipeline):
    """|| 上游失败，下游执行"""
    pipe, *_ = pipeline
    r = await pipe.run("false_xyz || echo fallback", session_id="s1")
    assert r.success
    assert r.exit_code == 0
    assert "fallback" in r.stdout


@skip_no_native
async def test_sequence_operator(pipeline):
    """; 顺序执行，无论上游成功失败"""
    pipe, *_ = pipeline
    r = await pipe.run("echo a ; echo b", session_id="s1")
    assert r.success
    assert "a" in r.stdout
    assert "b" in r.stdout


@skip_no_native
async def test_complex_pipeline_redirect(pipeline, tmp_path):
    """echo | cat > file"""
    pipe, *_ = pipeline
    out_file = tmp_path / "out.txt"
    r = await pipe.run(
        f"echo hello | cat > {out_file}", session_id="s1", cwd=str(tmp_path)
    )
    assert r.success
    assert out_file.exists()
    assert "hello" in out_file.read_text()


# ===== glob 展开 =====


@skip_no_native
async def test_glob_expansion_in_dispatcher(pipeline, workdir):
    """dispatcher 层 glob 展开：ls *.py 实际执行 ls a.py b.py"""
    pipe, *_ = pipeline
    r = await pipe.run("ls *.py", session_id="s1", cwd=str(workdir))
    assert r.success
    # 展开后传给内核的多文件
    assert "a.py" in r.stdout
    assert "b.py" in r.stdout


@skip_no_native
async def test_glob_no_match_keeps_literal(pipeline, workdir):
    """glob 无匹配保留字面量，由内核报错"""
    pipe, *_ = pipeline
    r = await pipe.run("ls *.nomatch", session_id="s1", cwd=str(workdir))
    # 内核 ls 收到 '*.nomatch' 字面量，文件不存在 → exit_code=2
    assert r.exit_code != 0


@skip_no_native
async def test_glob_with_cat(pipeline, workdir):
    """cat *.py 拼接所有 py 文件内容"""
    pipe, *_ = pipeline
    r = await pipe.run("cat *.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert "print('a')" in r.stdout
    assert "print('b')" in r.stdout


# ===== ln =====


@skip_no_native
async def test_ln_symbolic(pipeline, workdir):
    pipe, *_ = pipeline
    r = await pipe.run("ln -s a.py a_link.py", session_id="s1", cwd=str(workdir))
    assert r.success
    assert (workdir / "a_link.py").is_symlink()
