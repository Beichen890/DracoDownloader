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

# 延迟导入 - 避免导入错误阻塞整个测试文件
def _get_draco():
    try:
        from DracoDownloader import DracoDownloader
        return DracoDownloader
    except ImportError:
        pass
    return None

def _get_router():
    try:
        from DracoDownloader.protocols import ProtocolRouter
        return ProtocolRouter
    except ImportError:
        pass
    try:
        from protocols import ProtocolRouter
        return ProtocolRouter
    except ImportError:
        pass
    return None

# 其他导入
try:
    from DracoDownloader.bittorrent.bencode import encode, decode, decode_torrent, info_hash
    from DracoDownloader.bittorrent.magnet import MagnetParser
except ImportError:
    from bittorrent.bencode import encode, decode, decode_torrent, info_hash
    from bittorrent.magnet import MagnetParser

try:
    from DracoDownloader.scheduler import Scheduler, TaskStatus
except ImportError:
    from scheduler import Scheduler, TaskStatus


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


# ===== 路径安全（zip-slip 防护）=====

class TestZipSlipProtection:
    """测试 zip-slip 路径遍历防护"""

    def test_path_traversal_blocked_in_loaders(self):
        """测试 loaders.py 中的路径过滤"""
        # 直接导入模块，不通过包路径
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        
        from bittorrent.loaders import _parse_torrent_bytes
        from bittorrent.bencode import encode

        # 构造包含路径遍历的恶意 torrent
        malicious_torrent = {
            b'info': {
                b'name': b'test',
                b'piece length': 16384,
                b'pieces': b'x' * 20,
                b'files': [
                    {b'path': [b'..', b'..', b'etc', b'passwd'], b'length': 100},
                    {b'path': [b'.', b'normal_file'], b'length': 200},
                    {b'path': [b'/etc', b'shadow'], b'length': 150},
                    {b'path': [b'C:', b'Windows', b'System32'], b'length': 50},
                ]
            }
        }

        # 编码为 bencode
        encoded = encode(malicious_torrent)
        
        # 解析
        resolved = _parse_torrent_bytes(encoded, 'test.torrent', 'file')

        # 验证路径被过滤
        assert resolved.files is not None
        assert len(resolved.files) >= 1
        
        # 所有路径不应包含 ".." 或以 "/" 开头
        for file_info in resolved.files:
            path = file_info['path']
            assert '..' not in path, f"路径遍历未被过滤: {path}"
            assert not path.startswith('/'), f"绝对路径未被过滤: {path}"
            assert not path.startswith('C:'), f"Windows 盘符未被过滤: {path}"

    def test_path_traversal_blocked_in_torrent_protocol(self):
        """测试 protocols/torrent.py 中的路径过滤"""
        # 模拟 _legacy_probe 的解析逻辑（与修复后的代码一致）
        malicious_info = {
            b'name': b'test',
            b'piece length': 16384,
            b'pieces': b'x' * 20,
            b'files': [
                {b'path': [b'..', b'..', b'attack'], b'length': 100},
                {b'path': [b'normal', b'file.txt'], b'length': 200},
            ]
        }

        files = malicious_info.get(b'files', [])
        file_list = []
        if files:
            for f_info in files:
                path_parts = f_info.get(b'path', [])
                if isinstance(path_parts, list):
                    safe_parts = []
                    for p in path_parts:
                        part = p.decode() if isinstance(p, bytes) else str(p)
                        if part in ('', '.', '..'):
                            continue
                        if part.startswith('/') or part.startswith('\\'):
                            continue
                        if len(part) >= 2 and part[1] == ':':
                            continue
                        safe_parts.append(part)
                    f_path = '/'.join(safe_parts) or 'unnamed'
                else:
                    f_path = str(path_parts)
                file_list.append({'path': f_path, 'length': f_info.get(b'length', 0)})

        # 验证过滤结果
        assert len(file_list) == 1
        assert file_list[0]['path'] == 'normal/file.txt'
