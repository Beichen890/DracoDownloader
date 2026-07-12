"""
DracoDownloader 集成测试套件

测试覆盖:
  - 协议路由
  - Bencode 编解码
  - 磁力链接解析
  - 下载调度器
  - Core API
  - 进度管理

运行: python -m pytest tests/ -v
"""

import asyncio
import pytest  # type: ignore
import tempfile
import os
import sys
from pathlib import Path

# 确保包路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader import DracoDownloader
from DracoDownloader.protocols import ProtocolRouter
from DracoDownloader.scheduler import Scheduler, TaskStatus
from DracoDownloader.bittorrent.bencode import encode, decode, decode_torrent, info_hash
from DracoDownloader.bittorrent.magnet import MagnetParser


# ===== Bencode 编解码 =====

class TestBencode:
    def test_encode_int(self):
        assert encode(42) == b'i42e'
        assert encode(0) == b'i0e'
        assert encode(-1) == b'i-1e'

    def test_encode_string(self):
        assert encode(b'spam') == b'4:spam'
        assert encode('hello') == b'5:hello'

    def test_encode_list(self):
        assert encode([b'a', b'b']) == b'l1:a1:be'

    def test_encode_dict(self):
        result = encode({b'bar': b'spam', b'foo': 42})
        # 字典按键排序
        assert result == b'd3:bar4:spam3:fooi42ee'

    def test_decode_int(self):
        assert decode(b'i42e') == 42
        assert decode(b'i0e') == 0
        assert decode(b'i-3e') == -3

    def test_decode_string(self):
        assert decode(b'4:spam') == b'spam'

    def test_decode_list(self):
        assert decode(b'l4:spam4:eggse') == [b'spam', b'eggs']

    def test_decode_dict(self):
        result = decode(b'd3:bar4:spam3:fooi42ee')
        assert isinstance(result, dict)
        assert result[b'bar'] == b'spam'
        assert result[b'foo'] == 42

    def test_roundtrip(self):
        original = {
            b'announce': b'http://tracker.com',
            b'info': {
                b'name': b'test.torrent',
                b'piece length': 65536,
                b'pieces': b'x' * 40,
                b'length': 1024,
            }
        }
        encoded = encode(original)
        decoded = decode(encoded)
        assert decoded == original

    def test_decode_torrent_raises_on_invalid(self):
        with pytest.raises((ValueError, TypeError)):
            decode_torrent(b'not a torrent')


# ===== 磁力链接解析 =====

class TestMagnetParser:
    def test_parse_valid_magnet(self):
        uri = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567" \
              "&dn=test&tr=http://tracker.com"
        magnet = MagnetParser.parse(uri)
        assert magnet is not None
        assert magnet.display_name == 'test'
        assert len(magnet.trackers) == 1
        assert magnet.trackers[0] == 'http://tracker.com'

    def test_parse_invalid(self):
        assert MagnetParser.parse("") is None
        assert MagnetParser.parse("http://example.com") is None

    def test_create(self):
        info_hash = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        uri = MagnetParser.create(info_hash, name="test")
        assert uri.startswith("magnet:?xt=urn:btih:")
        assert "dn=test" in uri


# ===== 协议路由 =====

class TestProtocolRouter:
    def setup_method(self):
        self.router = ProtocolRouter()

    def test_route_http(self):
        driver = self.router.route("http://example.com/file.zip")
        assert driver is not None
        assert driver.__class__.__name__ == "HTTPDriver"

    def test_route_https(self):
        driver = self.router.route("https://example.com/file.zip")
        assert driver is not None

    def test_route_ftp(self):
        driver = self.router.route("ftp://fileserver.com/file.zip")
        assert driver is not None
        assert driver.__class__.__name__ == "FTPDriver"

    def test_route_m3u8(self):
        driver = self.router.route("https://example.com/stream.m3u8")
        assert driver is not None
        assert driver.__class__.__name__ == "M3U8Driver"

    def test_route_magnet(self):
        driver = self.router.route("magnet:?xt=urn:btih:abcd")
        assert driver is not None
        assert driver.__class__.__name__ == "TorrentDriver"

    def test_route_torrent(self):
        driver = self.router.route("file.torrent")
        assert driver is not None
        assert driver.__class__.__name__ == "TorrentDriver"

    def test_route_unsupported(self):
        driver = self.router.route("gopher://example.com/file")
        assert driver is None

    def test_list_supported(self):
        protocols = self.router.list_supported()
        assert len(protocols) >= 4
        assert "HTTPDriver" in protocols


# ===== 调度器 =====

class TestScheduler:
    @pytest.fixture
    def scheduler(self):
        return Scheduler(max_concurrent=3)

    @pytest.mark.asyncio
    async def test_add_and_cancel(self, scheduler):
        async def fake_executor(handle, task_id):
            await asyncio.sleep(10)
            return "done"

        scheduler.set_executor(fake_executor)
        task_id = scheduler.add("fake_url")
        assert task_id is not None
        assert len(task_id) == 8

        # 取消应该成功
        assert scheduler.cancel(task_id) is True

    def test_invalid_max_concurrent(self):
        with pytest.raises(ValueError):
            Scheduler(max_concurrent=0)

    def test_counts(self):
        s = Scheduler(max_concurrent=3)
        assert s.active_count() == 0
        assert s.queued_count() == 0

    def test_cancel_nonexistent(self):
        s = Scheduler(max_concurrent=3)
        assert s.cancel("nonexistent") is False


# ===== Core API =====

class TestCoreAPI:
    def test_init(self):
        downloader = DracoDownloader()
        assert downloader is not None

    def test_list_protocols(self):
        downloader = DracoDownloader()
        protocols = downloader.list_protocols()
        assert len(protocols) >= 4

    @pytest.mark.asyncio
    async def test_download_unsupported_protocol(self):
        downloader = DracoDownloader()
        result = await downloader.download_async(
            url="gopher://example.com/file",
            output_path=str(Path(tempfile.gettempdir()) / "test_output")
        )
        assert result.success is False
        assert not result.error is None

    def test_get_status(self):
        downloader = DracoDownloader()
        status = downloader.get_status()
        assert 'active' in status
        assert 'queued' in status
        assert 'protocols' in status
        assert len(status['protocols']) >= 4

    @pytest.mark.asyncio
    async def test_download_async_timeout(self):
        """测试通过调度器的超时机制"""
        downloader = DracoDownloader()
        # 使用一个不可能连接的超短超时
        result = await downloader.download_async(
            url="https://192.0.2.1/nonexistent",
            output_path=str(Path(tempfile.gettempdir()) / "timeout_test"),
            timeout=1
        )
        assert result.success is False


# ===== 进度管理 =====

class TestProgressManager:
    def test_update_and_get(self, tmp_path):
        from DracoDownloader.progress import ProgressManager
        pm = ProgressManager(storage_dir=tmp_path / ".progress")
        pm.update("test1", 50, 100, 1024)
        data = pm.get("test1")
        assert data is not None
        assert data.downloaded == 50
        assert data.total == 100
        assert data.progress == 50.0

    def test_delete(self, tmp_path):
        from DracoDownloader.progress import ProgressManager
        pm = ProgressManager(storage_dir=tmp_path / ".progress")
        pm.update("test1", 100, 200)
        pm.delete("test1")
        assert pm.get("test1") is None

    def test_list_active(self, tmp_path):
        from DracoDownloader.progress import ProgressManager
        pm = ProgressManager(storage_dir=tmp_path / ".progress")
        pm.update("task_a", 0, 100)
        pm.update("task_b", 50, 100)
        active = pm.list_active()
        assert "task_a" in active
        assert "task_b" in active
