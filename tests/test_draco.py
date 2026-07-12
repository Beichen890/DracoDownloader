"""
DracoDownloader 集成测试

测试覆盖:
  - Bencode 编解码
  - 磁力链接解析
  - 协议路由
  - 调度器
  - Core API
  - 进度管理

运行: python -m pytest DracoDownloader/tests/test_draco.py -v
"""

import asyncio
import sys
import tempfile
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader import DracoDownloader
from DracoDownloader.protocols import ProtocolRouter
from DracoDownloader.scheduler import Scheduler
from DracoDownloader.bittorrent.bencode import encode, decode
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
        # bencode 解码器将 bytes key 转为 str key
        assert result['bar'] == b'spam'
        assert result['foo'] == 42

    def test_roundtrip(self):
        original = {
            'announce': b'http://tracker.com',
            'info': {
                'name': b'test.torrent',
                'piece length': 65536,
                'pieces': b'x' * 40,
                'length': 1024,
            }
        }
        encoded = encode(original)
        decoded = decode(encoded)
        # 确认结构和值一致（解码器将 str key 编码后回读为 str key）
        assert decoded['announce'] == b'http://tracker.com'
        assert decoded['info']['name'] == b'test.torrent'
        assert decoded['info']['piece length'] == 65536
        assert decoded['info']['length'] == 1024


# ===== 磁力链接解析 =====

class TestMagnetParser:
    def test_parse_valid_magnet(self):
        uri = ("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
               "&dn=test&tr=http://tracker.com")
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
        assert self.router.route("http://example.com/file.zip") is not None

    def test_route_https(self):
        assert self.router.route("https://example.com/file.zip") is not None

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
        assert self.router.route("gopher://example.com/file") is None

    def test_list_supported(self):
        protocols = self.router.list_supported()
        assert len(protocols) >= 4
        assert "HTTPDriver" in protocols


# ===== 调度器 =====

class TestScheduler:
    def test_create(self):
        s = Scheduler(max_concurrent=3)
        assert s.active_count() == 0
        assert s.queued_count() == 0

    def test_invalid_max_concurrent(self):
        with pytest.raises(ValueError):
            Scheduler(max_concurrent=0)

    def test_cancel_nonexistent(self):
        s = Scheduler(max_concurrent=3)
        assert s.cancel("nonexistent") is False

    @pytest.mark.asyncio
    async def test_add_and_cancel(self):
        s = Scheduler(max_concurrent=3)
        async def fake_executor(handle, task_id):
            await asyncio.sleep(10)
            return "done"
        s.set_executor(fake_executor)
        task_id = s.add("fake_handle")
        assert task_id is not None
        assert len(task_id) == 8
        assert s.cancel(task_id) is True


# ===== Core API =====

class TestCoreAPI:
    def test_init(self):
        d = DracoDownloader()
        assert d is not None

    def test_list_protocols(self):
        d = DracoDownloader()
        assert len(d.list_protocols()) >= 4

    def test_get_status(self):
        d = DracoDownloader()
        status = d.get_status()
        assert 'active' in status
        assert 'protocols' in status

    @pytest.mark.asyncio
    async def test_download_unsupported_protocol(self):
        d = DracoDownloader()
        result = await d.download_async(
            url="gopher://example.com/file",
            output_path=str(Path(tempfile.gettempdir()) / "test_output")
        )
        assert result.success is False
        assert result.error is not None


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


# ===== CLI 工具 =====

class TestCLI:
    def test_verify_file(self):
        from DracoDownloader.cli import verify_file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as f:
            f.write(b"hello world")
            tmpname = f.name
        try:
            h = verify_file(tmpname, "sha256")
            assert len(h) == 64
            h_md5 = verify_file(tmpname, "md5")
            assert len(h_md5) == 32
        finally:
            os.unlink(tmpname)
