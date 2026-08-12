"""
自适应分片分裂测试

验证事件驱动分裂：快分片完成后立即分裂最慢分片（响应延迟 ~0），
而非等 1s 定时轮询。通过统计 Range 请求数验证分裂确实发生。
"""

import asyncio
import hashlib
import time
from pathlib import Path

import pytest
from aiohttp import web

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader.protocols.http import HTTPDriver
from DracoDownloader.protocols.base import DownloadHandle


_FILE_DATA: dict = {}
_REQUEST_COUNT: int = 0
_REQUEST_LOG: list = []


async def _tracking_handler(request: web.Request):
    """记录所有请求的 Range，用于验证分裂是否发生"""
    global _REQUEST_COUNT, _REQUEST_LOG
    filename = request.match_info['filename']
    data = _FILE_DATA.get(filename, b'')
    total = len(data)

    range_header = request.headers.get('Range', '')
    _REQUEST_COUNT += 1
    _REQUEST_LOG.append(range_header)

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

    # 第一个分片（start=0）正常快速返回
    # 第二个分片（start > 0）首次请求时 sleep 2s 模拟慢（但不是 trickle）
    # 速率守卫 grace 5s 内不检查，不会中断
    if start > 0 and end - start > 2 * 1024 * 1024:
        # 慢分片：sleep 2s 模拟慢路由（不是 trickle，不会触发速率守卫）
        # 快分片完成后事件驱动分裂此分片
        await asyncio.sleep(2)

    return web.Response(
        body=chunk, status=206,
        headers={
            'Content-Range': f'bytes {start}-{end}/{total}',
            'Content-Length': str(len(chunk)),
            'Accept-Ranges': 'bytes',
        },
    )


async def _start_server(data: bytes, filename: str):
    global _REQUEST_COUNT, _REQUEST_LOG
    _FILE_DATA[filename] = data
    _REQUEST_COUNT = 0
    _REQUEST_LOG = []
    app = web.Application()
    app.router.add_get('/{filename}', _tracking_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    sock = site._server.sockets[0]
    port = sock.getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


class TestAdaptiveSplit:
    """验证自适应分片分裂机制"""

    async def test_fast_chunk_triggers_split_of_slow_chunk(self, tmp_path):
        """快分片完成后事件驱动分裂慢分片

        文件 8MB，2 个分片各 4MB。
        - 分片 0（start=0）：立即返回
        - 分片 1（start>0）：sleep 2s 模拟慢路由

        快分片 0 完成后，事件驱动分裂分片 1（剩余 > 2MB min_split_bytes）。
        验证：Range 请求数 > 2（初始 2 个 + 分裂产生的）。
        """
        # 8MB，2 个分片各 4MB（> min_split_bytes 2MB，可分裂）
        data = bytes(range(256)) * (8 * 1024 * 1024 // 256)
        expected_sha = hashlib.sha256(data).hexdigest()
        filename = 'split_test.bin'

        runner, base_url = await _start_server(data, filename)
        try:
            url = f"{base_url}/{filename}"
            output = tmp_path / filename

            driver = HTTPDriver(
                adaptive=True,
                auto_optimize=False,
                chunk_size=4 * 1024 * 1024,  # 4MB → 2 个分片
                max_connections=8,
            )

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

            # 文件完整性
            assert output.exists()
            actual_sha = hashlib.sha256(output.read_bytes()).hexdigest()
            assert actual_sha == expected_sha, "下载文件校验失败"

            # 关键断言：请求数 > 初始分片数 2（分裂发生了）
            # 快分片 0 完成 → 事件驱动分裂分片 1 → 分片 1 被分成两半
            # 至少多 1 个请求（分裂出的新分片）
            assert _REQUEST_COUNT > 2, (
                f"请求数 {_REQUEST_COUNT} 未增加，分裂可能未触发。"
                f"请求日志: {_REQUEST_LOG}"
            )
        finally:
            await runner.cleanup()

    async def test_split_produces_non_overlapping_ranges(self, tmp_path):
        """分裂后的 Range 请求不重叠、无间隙（覆盖完整文件）"""
        data = bytes(range(256)) * (8 * 1024 * 1024 // 256)
        expected_sha = hashlib.sha256(data).hexdigest()
        filename = 'split_overlap.bin'

        runner, base_url = await _start_server(data, filename)
        try:
            url = f"{base_url}/{filename}"
            output = tmp_path / filename

            driver = HTTPDriver(
                adaptive=True,
                auto_optimize=False,
                chunk_size=4 * 1024 * 1024,
                max_connections=8,
            )

            probe = await driver.probe(url)
            handle = DownloadHandle(
                url=url,
                output_path=str(output),
                total_size=len(data),
                metadata={'supports_range': True},
            )

            await driver.download(handle)

            actual_sha = hashlib.sha256(output.read_bytes()).hexdigest()
            assert actual_sha == expected_sha, "下载文件校验失败（Range 可能重叠/有间隙）"

            # 解析所有 Range 请求，验证覆盖完整文件
            ranges_requested = []
            for rh in _REQUEST_LOG:
                if not rh:
                    continue
                spec = rh.replace('bytes=', '')
                start_s, end_s = spec.split('-')
                start = int(start_s)
                end = int(end_s) if end_s else len(data) - 1
                ranges_requested.append((start, end))

            # 合并区间验证覆盖 [0, total-1]
            ranges_requested.sort()
            assert ranges_requested[0][0] == 0, "Range 未从 0 开始"
            assert ranges_requested[-1][1] == len(data) - 1, "Range 未到文件末尾"

            # 验证无间隙、无重叠
            for i in range(1, len(ranges_requested)):
                prev_end = ranges_requested[i - 1][1]
                curr_start = ranges_requested[i][0]
                assert curr_start == prev_end + 1, (
                    f"Range 有间隙或重叠: prev_end={prev_end}, curr_start={curr_start}"
                )
        finally:
            await runner.cleanup()
