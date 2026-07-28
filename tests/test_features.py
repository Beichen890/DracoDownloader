"""
新特性测试：错误目录、步骤管线、配置系统、BT 多源加载、Web Tracker、做种
"""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader import (
    DracoError, make_error,
    ERR_UNSUPPORTED_PROTOCOL, ERR_HTTP_STATUS, ERR_NETWORK,
    TaskStep, StepPipeline, StepStatus, build_standard_pipeline,
    STEP_PROBE, STEP_DOWNLOAD, STEP_MERGE, STEP_VERIFY,
    DracoConfig, ConfigItem,
    RangeValidator, ChoiceValidator, IntValidator, BoolValidator,
)
from DracoDownloader.bittorrent.loaders import resolve as resolve_source, ResolvedTorrent
from DracoDownloader.bittorrent.trackers import (
    merge_trackers, WebTrackerFetcher, enrich_trackers, FALLBACK_TRACKERS,
)
from DracoDownloader.bittorrent.seeding import (
    SeedingPolicy, SeedingStats, SeedingController,
)
from DracoDownloader.bittorrent.bencode import encode


# ===== 错误目录 =====

class TestErrorCatalog:
    def test_make_error_basic(self):
        err = make_error(ERR_HTTP_STATUS, status=404, url="http://x.com")
        assert err.code == "draco.http_status"
        assert "404" in err.message
        assert "http://x.com" in err.message
        assert err.retryable is True

    def test_make_error_non_retryable(self):
        err = make_error(ERR_UNSUPPORTED_PROTOCOL, url="gopher://x")
        assert err.retryable is False
        assert err.code == ERR_UNSUPPORTED_PROTOCOL

    def test_draco_error_to_dict(self):
        err = make_error(ERR_NETWORK, detail="timeout")
        d = err.to_dict()
        assert d["code"] == "draco.network"
        assert d["retryable"] is True
        assert "timeout" in d["message"]
        assert d["context"]["detail"] == "timeout"

    def test_draco_error_is_exception(self):
        err = DracoError(code="draco.test", message="test")
        assert isinstance(err, Exception)
        with pytest.raises(DracoError):
            raise err


# ===== 步骤管线 =====

class TestStepPipeline:
    def test_build_standard_pipeline(self):
        async def noop():
            return None
        pipeline = build_standard_pipeline(
            probe_fn=noop, download_fn=noop, merge_fn=noop, verify_fn=noop,
        )
        assert len(pipeline.steps) == 4
        names = [s.name for s in pipeline.steps]
        assert names == [STEP_PROBE, STEP_DOWNLOAD, STEP_MERGE, STEP_VERIFY]

    def test_describe(self):
        async def noop():
            return None
        pipeline = build_standard_pipeline(probe_fn=noop, download_fn=noop)
        desc = pipeline.describe()
        assert desc[0]["name"] == STEP_PROBE
        assert desc[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_execute_success(self):
        async def probe():
            return {"size": 100}
        async def download():
            return {"downloaded": 100}
        pipeline = build_standard_pipeline(probe_fn=probe, download_fn=download)
        results = await pipeline.execute()
        assert len(results) == 2
        assert results[0].success is True
        assert results[0].data == {"size": 100}
        assert results[1].data == {"downloaded": 100}

    @pytest.mark.asyncio
    async def test_execute_failure_stops(self):
        async def probe():
            raise RuntimeError("boom")
        async def download():
            return "should not run"
        pipeline = build_standard_pipeline(probe_fn=probe, download_fn=download)
        results = await pipeline.execute()
        assert results[0].success is False
        assert results[0].error is not None
        # 下载步骤未执行
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_retry_step(self):
        call_count = {"n": 0}
        async def flaky():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first fail")
            return "ok"
        step = TaskStep(name="test", title="test", coroutine_factory=flaky)
        pipeline = StepPipeline([step])
        await pipeline.execute()
        assert step.status == StepStatus.FAILED
        result = await pipeline.retry_step("test")
        assert result is not None
        assert result.success is True


# ===== 配置系统 =====

class TestConfigSystem:
    def test_default_values(self):
        cfg = DracoConfig()
        assert cfg.get("max_concurrent") == 5
        assert cfg.get("auto_optimize") is True
        assert cfg.get("bt_enable_dht") is True

    def test_set_with_validation(self):
        cfg = DracoConfig()
        cfg.set("max_concurrent", 10)
        assert cfg.get("max_concurrent") == 10
        with pytest.raises(ValueError):
            cfg.set("max_concurrent", 0)

    def test_choice_validator(self):
        cfg = DracoConfig()
        cfg.set("mirror_region", "global")
        assert cfg.get("mirror_region") == "global"
        with pytest.raises(ValueError):
            cfg.set("mirror_region", "invalid")

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DRACO_MAX_CONCURRENT", "8")
        cfg = DracoConfig()
        assert cfg.get("max_concurrent") == 8

    def test_describe(self):
        cfg = DracoConfig()
        items = cfg.describe()
        keys = [i["key"] for i in items]
        assert "max_concurrent" in keys
        assert "bt_seeding_ratio_limit" in keys

    def test_save_load(self, tmp_path):
        cfg = DracoConfig()
        cfg.set("max_concurrent", 15)
        path = tmp_path / "config.json"
        cfg.save(path)
        cfg2 = DracoConfig()
        cfg2.load(path)
        assert cfg2.get("max_concurrent") == 15


# ===== BT 多源加载器 =====

class TestBTLoaders:
    @pytest.mark.asyncio
    async def test_resolve_magnet(self):
        magnet = ("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
                  "&dn=test&tr=http://tracker.com")
        resolved = await resolve_source(magnet)
        assert resolved.is_magnet is True
        assert resolved.source_type == "magnet"
        assert resolved.info_hash_hex == "0123456789abcdef0123456789abcdef01234567"
        assert resolved.name == "test"
        assert "http://tracker.com" in resolved.trackers

    @pytest.mark.asyncio
    async def test_resolve_file(self, tmp_path):
        # 构造最小合法 torrent
        info = {
            b'name': b'test.txt',
            b'length': 100,
            b'piece length': 16384,
            b'pieces': b'x' * 20,
        }
        torrent = {b'announce': b'http://tracker.com', b'info': info}
        data = encode(torrent)
        path = tmp_path / "test.torrent"
        path.write_bytes(data)

        resolved = await resolve_source(str(path))
        assert resolved.source_type == "file"
        assert resolved.is_magnet is False
        assert resolved.name == "test.txt"
        assert resolved.total_size == 100
        assert resolved.piece_length == 16384
        assert len(resolved.pieces_hashes) == 1
        assert "http://tracker.com" in resolved.trackers

    @pytest.mark.asyncio
    async def test_resolve_invalid_source(self):
        with pytest.raises(Exception):
            await resolve_source("/nonexistent/path.torrent")


# ===== Web Tracker =====

class TestWebTrackers:
    def test_merge_trackers_basic(self):
        existing = ["http://a.com/announce", "http://b.com/announce"]
        web = ["http://b.com/announce", "http://c.com/announce"]
        result = merge_trackers(existing, web, announce_to_all=True)
        # existing 优先
        assert result[0] == "http://a.com/announce"
        assert "http://c.com/announce" in result
        # 去重
        assert len(result) == 3

    def test_merge_trackers_no_announce_all(self):
        existing = ["http://a.com/announce"]
        web = ["http://b.com/announce"]
        result = merge_trackers(existing, web, announce_to_all=False)
        assert result == ["http://a.com/announce"]

    def test_fallback_trackers_present(self):
        assert len(FALLBACK_TRACKERS) > 0
        assert all(t.startswith(("udp://", "http://", "https://", "wss://", "ws://"))
                   for t in FALLBACK_TRACKERS)

    def test_valid_tracker_check(self):
        fetcher = WebTrackerFetcher()
        assert fetcher._is_valid_tracker("udp://tracker.example.com:1337")
        assert fetcher._is_valid_tracker("https://tracker.example.com/announce")
        assert not fetcher._is_valid_tracker("not a url")
        assert not fetcher._is_valid_tracker("")

    @pytest.mark.asyncio
    async def test_enrich_trackers_disabled(self):
        existing = ["http://a.com/announce"]
        result = await enrich_trackers(existing, enable_web=False)
        assert result == existing


# ===== 做种策略 =====

class TestSeeding:
    def test_seeding_policy_disabled(self):
        policy = SeedingPolicy(enabled=False)
        ctrl = SeedingController(policy)
        ctrl.start(downloaded=1000)
        assert ctrl.should_stop() is True

    def test_seeding_time_limit(self):
        policy = SeedingPolicy(enabled=True, time_limit=0.001, min_seed_time=0)
        ctrl = SeedingController(policy)
        ctrl.start(downloaded=1000)
        # time_limit=0.001s 已经超过
        import time
        time.sleep(0.002)
        assert ctrl.should_stop() is True

    def test_seeding_ratio_limit(self):
        policy = SeedingPolicy(enabled=True, ratio_limit=1.0, min_seed_time=0)
        ctrl = SeedingController(policy)
        ctrl.start(downloaded=1000)
        ctrl.update_upload(1000)  # ratio = 1.0
        assert ctrl.should_stop() is True

    def test_seeding_not_reached(self):
        policy = SeedingPolicy(enabled=True, ratio_limit=2.0, min_seed_time=0)
        ctrl = SeedingController(policy)
        ctrl.start(downloaded=1000)
        ctrl.update_upload(500)  # ratio = 0.5
        assert ctrl.should_stop() is False

    def test_seeding_stats(self):
        stats = SeedingStats(downloaded=1000, uploaded=500)
        assert stats.ratio == 0.5

    @pytest.mark.asyncio
    async def test_seeding_controller_wait(self):
        policy = SeedingPolicy(enabled=True, time_limit=0.01, min_seed_time=0)
        ctrl = SeedingController(policy)
        ctrl.start(downloaded=100)
        await ctrl.wait_until_stop(poll_interval=0.005)
        assert ctrl.should_stop() is True


# ===== BT 下载器数据完整性 =====

class TestBTDownloaderDataIntegrity:
    def test_piece_block_size_validation(self):
        from DracoDownloader.bittorrent.downloader import Piece

        piece = Piece(index=0, length=32768, hash=None)

        valid_block = b'\x00' * 16384
        piece.add_block(0, valid_block)
        assert piece.downloaded == 16384
        assert 0 in piece.blocks

    def test_piece_duplicate_block_prevention(self):
        from DracoDownloader.bittorrent.downloader import Piece

        piece = Piece(index=0, length=32768, hash=None)

        block1 = b'\x01' * 16384
        block2 = b'\x02' * 16384

        piece.add_block(0, block1)
        original_downloaded = piece.downloaded

        piece.add_block(0, block2)

        assert piece.downloaded == original_downloaded
        assert piece.blocks[0] == block1

    def test_multifile_write_boundaries(self):
        import tempfile
        import os
        from DracoDownloader.bittorrent.downloader import TorrentMeta

        with tempfile.TemporaryDirectory() as tmpdir:
            meta = TorrentMeta(
                info_hash=b'\x00' * 20,
                info_hash_hex='00' * 20,
                name='test',
                piece_length=1024,
                is_multi_file=True,
                files=[
                    {'path': 'file1.txt', 'length': 512},
                    {'path': 'file2.txt', 'length': 512},
                ]
            )

            for file_info in meta.files:
                file_path = Path(tmpdir) / file_info['path']
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'wb') as f:
                    f.write(b'\x00' * file_info['length'])

            data = b'A' * 1024

            offset = 0
            data_end = offset + len(data)

            for i, file_info in enumerate(meta.files):
                file_path = Path(tmpdir) / file_info['path']
                f_start = sum(f['length'] for f in meta.files[:i])
                f_end = f_start + file_info['length']

                if data_end <= f_start:
                    break
                if offset >= f_end:
                    continue

                overlap_start = max(offset, f_start)
                overlap_end = min(data_end, f_end)

                file_write_start = overlap_start - f_start
                data_read_start = overlap_start - offset
                data_read_end = overlap_end - offset

                with open(file_path, 'r+b') as f:
                    f.seek(file_write_start)
                    f.write(data[data_read_start:data_read_end])

            with open(Path(tmpdir) / 'file1.txt', 'rb') as f:
                content1 = f.read()
            with open(Path(tmpdir) / 'file2.txt', 'rb') as f:
                content2 = f.read()

            assert content1 == b'A' * 512
            assert content2 == b'A' * 512


# ===== Zip-slip 路径安全过滤（关键缺陷回归测试） =====

class TestZipSlipSanitization:
    """
    针对 BitTorrent 多文件模式 path 字段的 Zip-slip/路径穿越防护测试。

    触发场景：攻击者构造恶意 .torrent，在 info.files[].path 中放入
    `..`、绝对路径、Windows 盘符、或在单个段内嵌入分隔符，
    企图让 Path(download_dir) / malicious_path 解析到下载目录之外。
    """

    def test_sanitize_normal_path(self):
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts
        result = sanitize_torrent_path_parts([b'dir', b'subdir', 'file.txt'])
        assert result == 'dir/subdir/file.txt'

    def test_sanitize_strips_dotdot_and_empty(self):
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts
        # 单独的 '..'、'.'、'' 段必须被过滤
        result = sanitize_torrent_path_parts(
            ['', '.', '..', 'safe_dir', '..', '.', '', 'safe_file.txt']
        )
        # 不能以 '..' 开头，也不能包含父目录逃逸
        from pathlib import PurePosixPath
        normalized = PurePosixPath(result)
        assert not normalized.is_absolute()
        assert '..' not in normalized.parts
        assert result == 'safe_dir/safe_file.txt'

    def test_sanitize_rejects_absolute_path_segment(self):
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts
        # 以 / 或 \ 开头的段必须被丢弃
        result = sanitize_torrent_path_parts(['/etc', b'passwd'])
        # '/etc' 被丢弃，仅保留 'passwd'
        assert result == 'passwd'
        # 整体结果不能是绝对路径
        from pathlib import PurePosixPath
        assert not PurePosixPath(result).is_absolute()

    def test_sanitize_rejects_windows_drive_letter(self):
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts
        result = sanitize_torrent_path_parts(['C:', 'Windows', 'System32'])
        # 'C:' 段必须被丢弃
        assert 'C:' not in result
        assert result == 'Windows/System32' or result == 'Windows/System32'

    def test_sanitize_bypass_with_separator_in_segment(self):
        """
        关键绕过用例：单个段内含分隔符 + .. 组合。

        旧实现仅逐段比对是否等于 '..'，未检查段内是否存在 '/' 或 '\\'。
        恶意构造 path = ['subdir/../../../etc/passwd'] 会绕过检查，
        但 Path(download_dir) / 'subdir/../../../etc/passwd' 实际会
        解析到 download_dir 的 3 级父目录下的 etc/passwd，
        造成任意文件覆盖（zip-slip 经典变种）。
        """
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts

        # 正向斜线嵌入
        malicious = ['subdir/../../../etc/passwd']
        result = sanitize_torrent_path_parts(malicious)
        # 含 '/' 的段必须整个被丢弃；剩余段为空 → 返回 'unnamed'
        assert result == 'unnamed', (
            f"段内分隔符未被拦截：{result!r}，存在 zip-slip 风险"
        )

        # 反斜线嵌入（Windows/UNC 变种）
        malicious_backslash = ['subdir\\..\\..\\..\\etc\\passwd']
        result_bs = sanitize_torrent_path_parts(malicious_backslash)
        assert result_bs == 'unnamed', (
            f"段内反斜线未被拦截：{result_bs!r}，存在 zip-slip 风险"
        )

    def test_sanitize_all_malicious_returns_unnamed(self):
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts
        # 全部是恶意段时，返回占位 'unnamed'，不能是空串或 '..'
        assert sanitize_torrent_path_parts(['..', '..']) == 'unnamed'
        assert sanitize_torrent_path_parts(['/etc']) == 'unnamed'
        assert sanitize_torrent_path_parts(['D:']) == 'unnamed'
        assert sanitize_torrent_path_parts([]) == 'unnamed'

    def test_sanitized_path_never_escapes_output_dir(self):
        """
        端到端校验：对净化后的结果，
        `Path(output_dir) / sanitized_path` 仍必须位于 output_dir 之下。
        """
        from pathlib import Path
        from DracoDownloader.bittorrent.utils import sanitize_torrent_path_parts

        output_dir = Path('/tmp/draco_test_downloads').resolve()

        malicious_cases = [
            ['..', '..', 'etc', 'passwd'],
            ['/etc', 'passwd'],
            ['nested/../../evil.sh'],
            ['dir', '..\\..\\evil.dat'],
        ]

        for case in malicious_cases:
            safe = sanitize_torrent_path_parts(case)
            target = (output_dir / safe).resolve()
            # 关键断言：最终解析路径必须在输出目录内
            assert str(target).startswith(str(output_dir) + '/') or target == output_dir, (
                f"净化失败：case={case!r} → safe={safe!r} → target={target}，"
                f"逃出了输出目录 {output_dir}"
            )

    @pytest.mark.asyncio
    async def test_loader_parse_bytes_zip_slip(self, tmp_path):
        """
        BT 多源加载器 `_parse_torrent_bytes` 必须净化恶意 path 段。
        （原代码完全无过滤 → 任意路径写入）
        """
        from DracoDownloader.bittorrent.bencode import encode
        from DracoDownloader.bittorrent.loaders import _parse_torrent_bytes

        info = {
            b'name': b'malicious',
            b'piece length': 16384,
            b'pieces': b'x' * 20,
            b'files': [
                # Case A: 多段式 '..' 穿越
                {b'path': [b'..', b'..', b'etc', b'passwd.sh'], b'length': 100},
                # Case B: 段内分隔符绕过（单段含多个 '/' + '..'）
                {b'path': [b'subdir/../../run/evil.pwn'], b'length': 200},
                # Case C: 安全对照组
                {b'path': [b'data', b'good.txt'], b'length': 50},
            ],
        }
        torrent = {b'announce': b'http://tracker.example', b'info': info}
        data = encode(torrent)

        resolved = _parse_torrent_bytes(data, source='test', source_type='file')
        assert len(resolved.files) == 3

        # Case A: '..' 段被剔除 → 只剩 etc/passwd.sh（安全子路径）
        f0 = resolved.files[0]['path']
        assert '..' not in f0.split('/')
        # Case B: 段内分隔符 → 整段丢弃 → 'unnamed'
        assert resolved.files[1]['path'] == 'unnamed'
        # Case C: 正常路径不变
        assert resolved.files[2]['path'] == 'data/good.txt'

        # 二次校验：所有结果在拼接后都不能逃出下载目录
        out = (tmp_path / 'download_dir').resolve()
        for finfo in resolved.files:
            target = (out / finfo['path']).resolve()
            assert str(target).startswith(str(out) + '/') or target == out

    @pytest.mark.asyncio
    async def test_downloader_parse_torrent_segment_separator_bypass(self, tmp_path):
        """
        BTDownloader._parse_torrent_file 对段内分隔符的绕过必须被拦截。
        （原实现仅检查段是否 == '..'，未检查段内含 '/' → zip-slip 绕过）
        """
        from DracoDownloader.bittorrent.bencode import encode

        info = {
            b'name': b'bypass_test',
            b'piece length': 16384,
            b'pieces': b'x' * 20,
            b'files': [
                # 单段恶意：a/b/../../../x  + 段内分隔符
                {b'path': [b'a/b/../../../overwrite_me'], b'length': 10},
            ],
        }
        torrent = {b'announce': b'http://t', b'info': info}
        torrent_path = tmp_path / "m.torrent"
        torrent_path.write_bytes(encode(torrent))

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        # 直接实例化 BTDownloader 并解析（构造 source 为本地 torrent）
        from DracoDownloader.bittorrent.downloader import BTDownloader
        dl = BTDownloader.__new__(BTDownloader)
        dl.output_path = out_dir / "target"
        dl._parse_torrent_file(str(torrent_path))

        assert dl.meta is not None
        assert len(dl.meta.files) == 1
        sanitized = dl.meta.files[0]['path']
        # 恶意段内分隔符必须被拦截，占位 unnamed
        assert sanitized == 'unnamed', (
            f"段内分隔符绕过未拦截，得到 path={sanitized!r}"
        )
        # 最终写入路径不能跳出 out_dir
        target = (out_dir / sanitized).resolve()
        assert str(target).startswith(str(out_dir.resolve()) + '/') or target == out_dir.resolve()


# ===== 调度器 CancelledError 传播 + Pending Task 清理 =====

class TestSchedulerCancellation:
    """
    验证调度器取消/超时后，pending 的 asyncio.Task 被正确 cancel + await，
    确保其 finally 清理块能执行（原实现只 cancel 不 await → 资源泄漏）。
    """

    @pytest.mark.asyncio
    async def test_cancelled_task_cleanup_runs(self):
        from DracoDownloader.scheduler import Scheduler, TaskStatus

        cleanup_flag = {"ran": False}
        task_started = asyncio.Event()

        async def slow_executor(handle, task_id):
            try:
                task_started.set()
                await asyncio.sleep(10.0)
                return "should-not-reach"
            finally:
                # 这是底层驱动释放连接、关闭句柄的关键路径；
                # 如果 pending task 只被 cancel() 而不 await，
                # 该块可能直到 GC 才运行或报 "Task destroyed but pending"。
                cleanup_flag["ran"] = True

        s = Scheduler(max_concurrent=1)
        s.set_executor(slow_executor)
        task_id = s.add("handle", timeout=3600)

        # 等 executor 进入 sleep（确认 task 实际启动了）
        await asyncio.wait_for(task_started.wait(), timeout=2.0)

        # 触发取消（走 scheduler.cancel → cancel_event → wait_cancel → CancelledError）
        assert s.cancel(task_id) is True

        # 等待关联的 Future 完成（这意味着 _run_with_timeout 的异常
        # 处理/清理流程已经全部结束，pending tasks 的 await 也已执行）
        fut = s._futures.get(task_id)
        if fut is not None:
            done, _ = await asyncio.wait(
                [asyncio.shield(fut)], timeout=5.0
            )
            # 即使 fut 被 set_exception(CancelledError) 也会进入 done 集合

        # 为保险起见，再轮询一小段时间让 finally 有机会跑
        deadline = asyncio.get_running_loop().time() + 3.0
        while (not cleanup_flag["ran"]
               and asyncio.get_running_loop().time() < deadline):
            await asyncio.sleep(0.05)

        # 关键断言：finally 块必须已执行
        assert cleanup_flag["ran"] is True, (
            "被取消的 task 未被 await，finally 清理块未执行 → 资源泄漏"
        )

    @pytest.mark.asyncio
    async def test_timeout_branch_pending_tasks_awaited(self):
        """
        超时分支中，pending tasks 也必须被 await（触发 finally）。
        """
        from DracoDownloader.scheduler import Scheduler, TaskStatus

        cleanup_flag = {"ran": False}
        task_started = asyncio.Event()

        async def slow_executor(handle, task_id):
            try:
                task_started.set()
                await asyncio.sleep(30.0)
                return "nope"
            finally:
                cleanup_flag["ran"] = True

        s = Scheduler(max_concurrent=1)
        s.set_executor(slow_executor)
        # 极短超时：让 wait(..., timeout=0.05) 先完成，进入 timeout 分支
        task_id = s.add("handle", timeout=0.05)

        await asyncio.wait_for(task_started.wait(), timeout=2.0)

        # 等待任务 future 进入终态（set_result / set_exception 被调用过）
        fut = s._futures.get(task_id)
        if fut is not None:
            await asyncio.wait([asyncio.shield(fut)], timeout=8.0)

        # 再轮询 finally 是否有机会执行
        deadline = asyncio.get_running_loop().time() + 5.0
        terminal = {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
                    TaskStatus.COMPLETED.value}
        while (not cleanup_flag["ran"]
               and asyncio.get_running_loop().time() < deadline):
            await asyncio.sleep(0.05)

        assert cleanup_flag["ran"] is True, (
            "超时分支中 pending task 未被 await，finally 未执行 → 资源泄漏"
        )
