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
