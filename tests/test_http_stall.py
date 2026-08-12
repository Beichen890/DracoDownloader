"""
HTTP 分片下载 stall / trickle 超时测试

验证两种"长尾分片卡死"场景：
1. 完全 stall：服务器长时间无响应（sock_read 超时抓）
2. 慢速 trickle：服务器持续发送少量数据（速率守卫抓）

第二种是真实场景：CDN 某条路由变慢，服务器以 9 KB/s 持续吐字节，
sock_read 永不触发（每次 read 都在 15s 内返回），但整体速度极低，
导致 19 分片中 18 个秒完、最后 1 个卡 250s+。
"""

import asyncio
import hashlib
import time
from pathlib import Path

import pytest
import aiohttp
from aiohttp import web

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader.protocols.http import HTTPDriver
from DracoDownloader.protocols.base import DownloadHandle


# === 测试用慢速服务器（共享状态） ===

_FILE_DATA: dict = {}
_RANGE_REQUEST_COUNT: dict = {}


async def _slow_stream_handler(request: web.Request):
    """模拟慢速服务器：首次请求某 range 时 stall，后续请求正常

    用全局 dict 记录每个 range 被请求的次数，首次 stall 20s
    （>sock_read 15s 触发超时重连），第二次正常返回。
    """
    filename = request.match_info['filename']
    data = _FILE_DATA.get(filename, b'')
    total = len(data)

    range_header = request.headers.get('Range', '')
    if not range_header:
        return web.Response(body=data, headers={
            'Content-Length': str(total),
            'Accept-Ranges': 'bytes',
        })

    try:
        spec = range_header.replace('bytes=', '')
        start_s, end_s = spec.split('-')
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    except (ValueError, IndexError):
        return web.Response(status=400)

    chunk = data[start:end + 1]

    range_key = f"{filename}:{start}-{end}"
    _RANGE_REQUEST_COUNT[range_key] = _RANGE_REQUEST_COUNT.get(range_key, 0) + 1
    if _RANGE_REQUEST_COUNT[range_key] == 1:
        await asyncio.sleep(20)  # > sock_read(15s)，触发完全 stall 超时

    return web.Response(
        body=chunk, status=206,
        headers={
            'Content-Range': f'bytes {start}-{end}/{total}',
            'Content-Length': str(len(chunk)),
            'Accept-Ranges': 'bytes',
        },
    )


async def _trickle_handler(request: web.Request):
    """模拟 trickle 服务器：首次请求慢速 dribble，后续正常

    真实场景：CDN 路由变慢，服务器以 ~8 KB/s 持续发送少量数据。
    sock_read 永不触发（每次 read 在 15s 内返回），但整体速度极低。
    速率守卫在窗口期检测到速度 < 阈值后抛 TimeoutError，触发重连。
    第二次请求模拟"换了一条好路由"，正常速度返回。

    计数按 filename + "分片起点向下取整 1MB"：worker 重试时 start 偏移
    会因已下载字节变化，但属于同一分片，不应再次 trickle。
    """
    filename = request.match_info['filename']
    data = _FILE_DATA.get(filename, b'')
    total = len(data)

    range_header = request.headers.get('Range', '')
    if not range_header:
        return web.Response(body=data, headers={
            'Content-Length': str(total),
            'Accept-Ranges': 'bytes',
        })

    try:
        spec = range_header.replace('bytes=', '')
        start_s, end_s = spec.split('-')
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    except (ValueError, IndexError):
        return web.Response(status=400)

    chunk = data[start:end + 1]

    # 按分片起点（1MB 对齐）计数，避免 worker 部分下载后重试又触发 trickle
    shard_start = (start // (1024 * 1024)) * (1024 * 1024)
    range_key = f"{filename}:{shard_start}"
    _RANGE_REQUEST_COUNT[range_key] = _RANGE_REQUEST_COUNT.get(range_key, 0) + 1
    is_first = _RANGE_REQUEST_COUNT[range_key] == 1

    response = web.StreamResponse(
        status=206,
        headers={
            'Content-Range': f'bytes {start}-{end}/{total}',
            'Content-Length': str(len(chunk)),
            'Accept-Ranges': 'bytes',
        },
    )
    await response.prepare(request)

    if is_first:
        # trickle：每 0.5s 发 4KB → 8 KB/s = 64 Kbps（远低于阈值 200 Kbps）
        # sock_read=15s 不会触发（每次 write 间隔 0.5s），只有速率守卫能抓
        try:
            for i in range(0, len(chunk), 4096):
                await response.write(chunk[i:i + 4096])
                await asyncio.sleep(0.5)
        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
            # 客户端因速率守卫触发断开，正常退出
            return response
    else:
        # 后续请求：正常速度一次性返回
        await response.write(chunk)

    await response.write_eof()
    return response


async def _start_server(data: bytes, filename: str, handler):
    """启动测试服务器"""
    _FILE_DATA[filename] = data
    _RANGE_REQUEST_COUNT.clear()
    app = web.Application()
    app.router.add_get('/{filename}', handler)  # add_get 自动处理 HEAD
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    sock = site._server.sockets[0]
    port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    return runner, base_url


# === 测试 ===

class TestStallTimeout:
    """验证 sock_read 超时防止长尾分片完全卡死"""

    async def test_slow_chunk_reconnects_within_timeout(self, tmp_path):
        """完全 stall：sock_read 超时后重连，不会卡满 total timeout

        文件 2MB，2 个分片。其中一个分片首次请求 stall 20s（>sock_read 15s），
        应在 ~15s 后超时重连，第二次请求正常返回。
        """
        data = bytes(range(256)) * (2 * 1024 * 1024 // 256)
        expected_sha = hashlib.sha256(data).hexdigest()
        filename = 'stall_test.bin'

        runner, base_url = await _start_server(data, filename, _slow_stream_handler)
        try:
            url = f"{base_url}/{filename}"
            output = tmp_path / filename

            driver = HTTPDriver(
                adaptive=False,  # 走合并路径，测 _download_chunk
                auto_optimize=False,
                chunk_size=1024 * 1024,  # 1MB → 2 个分片
                max_connections=4,
            )

            t0 = time.time()
            probe = await driver.probe(url)
            assert probe['size'] == len(data)
            assert probe['supports_range'] is True

            handle = DownloadHandle(
                url=url,
                output_path=str(output),
                total_size=len(data),
                metadata={'supports_range': True},
            )

            await driver.download(handle)
            elapsed = time.time() - t0

            assert output.exists()
            actual_sha = hashlib.sha256(output.read_bytes()).hexdigest()
            assert actual_sha == expected_sha, "下载文件校验失败"

            # stall 20s + sock_read 15s 超时 + 重连下载 ~1s ≈ 16-20s
            # 给 90s 余量（避免 CI 环境抖动），但必须 << 300s
            assert elapsed < 90, (
                f"下载耗时 {elapsed:.1f}s 过长，sock_read 超时可能未生效"
                f"（应 < 90s，total timeout 300s）"
            )
        finally:
            await runner.cleanup()

    async def test_adaptive_path_slow_chunk(self, tmp_path):
        """自适应路径：完全 stall 后被孤儿接管"""
        data = bytes(range(256)) * (2 * 1024 * 1024 // 256)
        expected_sha = hashlib.sha256(data).hexdigest()
        filename = 'stall_adaptive.bin'

        runner, base_url = await _start_server(data, filename, _slow_stream_handler)
        try:
            url = f"{base_url}/{filename}"
            output = tmp_path / filename

            driver = HTTPDriver(
                adaptive=True,
                auto_optimize=False,
                chunk_size=1024 * 1024,
                max_connections=4,
            )

            t0 = time.time()
            probe = await driver.probe(url)
            handle = DownloadHandle(
                url=url,
                output_path=str(output),
                total_size=len(data),
                metadata={'supports_range': True},
            )

            await driver.download(handle)
            elapsed = time.time() - t0

            actual_sha = hashlib.sha256(output.read_bytes()).hexdigest()
            assert actual_sha == expected_sha, "自适应下载文件校验失败"
            assert elapsed < 90, f"自适应下载耗时 {elapsed:.1f}s 过长"
        finally:
            await runner.cleanup()


class TestTrickleDetection:
    """验证速率守卫捕获慢速 trickle（sock_read 抓不到的真实场景）"""

    async def test_trickle_merge_path(self, tmp_path):
        """合并路径：trickle 分片被速率守卫中断后重连

        文件 2MB，2 个分片。一个分片首次请求以 8 KB/s trickle（sock_read 不触发），
        速率守卫在 ~15s（grace 5s + window 10s）内检测到速度过低，
        抛 TimeoutError 触发重连，第二次正常返回。
        总耗时应 << 60s（如果没速率守卫，trickle 要 250s+）。
        """
        data = bytes(range(256)) * (2 * 1024 * 1024 // 256)
        expected_sha = hashlib.sha256(data).hexdigest()
        filename = 'trickle_merge.bin'

        runner, base_url = await _start_server(data, filename, _trickle_handler)
        try:
            url = f"{base_url}/{filename}"
            output = tmp_path / filename

            driver = HTTPDriver(
                adaptive=False,  # 走合并路径
                auto_optimize=False,
                chunk_size=1024 * 1024,  # 1MB → 2 个分片
                max_connections=4,
            )

            t0 = time.time()
            probe = await driver.probe(url)
            handle = DownloadHandle(
                url=url,
                output_path=str(output),
                total_size=len(data),
                metadata={'supports_range': True},
            )

            await driver.download(handle)
            elapsed = time.time() - t0

            actual_sha = hashlib.sha256(output.read_bytes()).hexdigest()
            assert actual_sha == expected_sha, "下载文件校验失败"

            # trickle 1MB / 8KBps = 128s（如果不中断）
            # 速率守卫应在 grace(5s) + window(10s) = 15s 左右触发
            # + 重连 + 正常下载 ~1s ≈ 16-20s
            # 给 60s 余量，必须远小于 128s
            assert elapsed < 60, (
                f"下载耗时 {elapsed:.1f}s 过长，速率守卫可能未生效"
                f"（trickle 不中断需要 128s+）"
            )
        finally:
            await runner.cleanup()

    async def test_trickle_adaptive_path(self, tmp_path):
        """自适应路径：trickle 分片被速率守卫中断后分裂/重连"""
        data = bytes(range(256)) * (2 * 1024 * 1024 // 256)
        expected_sha = hashlib.sha256(data).hexdigest()
        filename = 'trickle_adaptive.bin'

        runner, base_url = await _start_server(data, filename, _trickle_handler)
        try:
            url = f"{base_url}/{filename}"
            output = tmp_path / filename

            driver = HTTPDriver(
                adaptive=True,
                auto_optimize=False,
                chunk_size=1024 * 1024,
                max_connections=4,
            )

            t0 = time.time()
            probe = await driver.probe(url)
            handle = DownloadHandle(
                url=url,
                output_path=str(output),
                total_size=len(data),
                metadata={'supports_range': True},
            )

            await driver.download(handle)
            elapsed = time.time() - t0

            actual_sha = hashlib.sha256(output.read_bytes()).hexdigest()
            assert actual_sha == expected_sha, "自适应下载文件校验失败"
            assert elapsed < 60, f"自适应下载耗时 {elapsed:.1f}s 过长"
        finally:
            await runner.cleanup()
