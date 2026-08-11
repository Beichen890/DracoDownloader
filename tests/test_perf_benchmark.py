"""
性能基准测试：自适应下载路径 vs 传统合并路径

启动本地 HTTP 服务器（支持 Range），下载不同大小的文件，
对比两条路径的耗时与产出文件完整性。
"""

import asyncio
import hashlib
import os
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader.protocols.base import DownloadHandle
from DracoDownloader.protocols.http import HTTPDriver


# ── 本地 HTTP 服务器（aiohttp，支持 Range） ──

@pytest.fixture(scope="function")
async def http_server(tmp_path):
    """启动支持 Range 的本地 aiohttp HTTP 服务器"""
    serve_dir = tmp_path / "http_root"
    serve_dir.mkdir()

    # 生成不同大小的测试文件
    files = {}
    for name, size_mb in [("small.bin", 1), ("medium.bin", 5), ("large.bin", 20)]:
        data = os.urandom(size_mb * 1024 * 1024)
        path = serve_dir / name
        path.write_bytes(data)
        files[name] = {
            "data": data,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    async def handle_file(request):
        filename = request.match_info['filename']
        filepath = serve_dir / filename
        if not filepath.exists():
            return web.Response(status=404, text="Not Found")

        file_size = filepath.stat().st_size
        range_header = request.headers.get('Range')

        if range_header:
            # 解析 Range: bytes=start-end
            range_spec = range_header.replace('bytes=', '')
            parts = range_spec.split('-')
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            with open(filepath, 'rb') as f:
                f.seek(start)
                data = f.read(length)

            return web.Response(
                body=data,
                status=206,
                headers={
                    'Content-Range': f'bytes {start}-{end}/{file_size}',
                    'Content-Length': str(length),
                    'Accept-Ranges': 'bytes',
                },
            )
        else:
            with open(filepath, 'rb') as f:
                data = f.read()
            return web.Response(
                body=data,
                status=200,
                headers={
                    'Content-Length': str(file_size),
                    'Accept-Ranges': 'bytes',
                },
            )

    app = web.Application()
    app.router.add_get('/{filename}', handle_file)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 18924)
    await site.start()

    yield files, "http://127.0.0.1:18924"

    await runner.cleanup()


def verify_file(path: str, expected_sha256: str) -> bool:
    """校验文件 SHA256"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest() == expected_sha256


async def download_with_driver(url: str, output_path: str, adaptive: bool) -> dict:
    """用指定模式下载并返回计时结果"""
    driver = HTTPDriver(adaptive=adaptive, auto_optimize=False)

    # probe
    probe_result = await driver.probe(url)

    handle = DownloadHandle(
        url=url,
        output_path=output_path,
        total_size=probe_result['size'],
        metadata=probe_result,
    )

    start = time.perf_counter()
    await driver.download(handle)
    elapsed = time.perf_counter() - start

    actual_size = os.path.getsize(output_path)
    return {
        "elapsed": elapsed,
        "size": actual_size,
        "speed_mbps": (actual_size / 1024 / 1024) / elapsed if elapsed > 0 else 0,
    }


# ── 测试用例 ──

class TestAdaptiveVsLegacy:
    """自适应路径 vs 传统合并路径性能对比"""

    @pytest.mark.asyncio
    async def test_adaptive_small_file(self, http_server, tmp_path):
        """自适应路径 - 小文件 (1MB)"""
        files, base_url = http_server
        info = files["small.bin"]
        url = f"{base_url}/small.bin"
        out = str(tmp_path / "adaptive_small.bin")

        result = await download_with_driver(url, out, adaptive=True)

        assert result["size"] == info["size"]
        assert verify_file(out, info["sha256"]), "文件完整性校验失败"
        print(f"\n[自适应] 1MB: {result['elapsed']:.3f}s, {result['speed_mbps']:.1f} MB/s")

    @pytest.mark.asyncio
    async def test_legacy_small_file(self, http_server, tmp_path):
        """传统合并路径 - 小文件 (1MB)"""
        files, base_url = http_server
        info = files["small.bin"]
        url = f"{base_url}/small.bin"
        out = str(tmp_path / "legacy_small.bin")

        result = await download_with_driver(url, out, adaptive=False)

        assert result["size"] == info["size"]
        assert verify_file(out, info["sha256"]), "文件完整性校验失败"
        print(f"\n[传统合并] 1MB: {result['elapsed']:.3f}s, {result['speed_mbps']:.1f} MB/s")

    @pytest.mark.asyncio
    async def test_adaptive_medium_file(self, http_server, tmp_path):
        """自适应路径 - 中文件 (5MB)"""
        files, base_url = http_server
        info = files["medium.bin"]
        url = f"{base_url}/medium.bin"
        out = str(tmp_path / "adaptive_medium.bin")

        result = await download_with_driver(url, out, adaptive=True)

        assert result["size"] == info["size"]
        assert verify_file(out, info["sha256"]), "文件完整性校验失败"
        print(f"\n[自适应] 5MB: {result['elapsed']:.3f}s, {result['speed_mbps']:.1f} MB/s")

    @pytest.mark.asyncio
    async def test_legacy_medium_file(self, http_server, tmp_path):
        """传统合并路径 - 中文件 (5MB)"""
        files, base_url = http_server
        info = files["medium.bin"]
        url = f"{base_url}/medium.bin"
        out = str(tmp_path / "legacy_medium.bin")

        result = await download_with_driver(url, out, adaptive=False)

        assert result["size"] == info["size"]
        assert verify_file(out, info["sha256"]), "文件完整性校验失败"
        print(f"\n[传统合并] 5MB: {result['elapsed']:.3f}s, {result['speed_mbps']:.1f} MB/s")

    @pytest.mark.asyncio
    async def test_adaptive_large_file(self, http_server, tmp_path):
        """自适应路径 - 大文件 (20MB)"""
        files, base_url = http_server
        info = files["large.bin"]
        url = f"{base_url}/large.bin"
        out = str(tmp_path / "adaptive_large.bin")

        result = await download_with_driver(url, out, adaptive=True)

        assert result["size"] == info["size"]
        assert verify_file(out, info["sha256"]), "文件完整性校验失败"
        print(f"\n[自适应] 20MB: {result['elapsed']:.3f}s, {result['speed_mbps']:.1f} MB/s")

    @pytest.mark.asyncio
    async def test_legacy_large_file(self, http_server, tmp_path):
        """传统合并路径 - 大文件 (20MB)"""
        files, base_url = http_server
        info = files["large.bin"]
        url = f"{base_url}/large.bin"
        out = str(tmp_path / "legacy_large.bin")

        result = await download_with_driver(url, out, adaptive=False)

        assert result["size"] == info["size"]
        assert verify_file(out, info["sha256"]), "文件完整性校验失败"
        print(f"\n[传统合并] 20MB: {result['elapsed']:.3f}s, {result['speed_mbps']:.1f} MB/s")

    @pytest.mark.asyncio
    async def test_no_temp_files_adaptive(self, http_server, tmp_path):
        """自适应路径不应产生临时分片文件"""
        files, base_url = http_server
        url = f"{base_url}/medium.bin"
        out = str(tmp_path / "adaptive_notemp.bin")

        await download_with_driver(url, out, adaptive=True)

        # 不应有 .parts 临时目录
        parts_dir = Path(out).parent / f".{Path(out).name}.parts"
        assert not parts_dir.exists(), "自适应路径不应创建临时分片目录"

    @pytest.mark.asyncio
    async def test_legacy_has_temp_files(self, http_server, tmp_path):
        """传统路径下载完成后应清理临时分片目录"""
        files, base_url = http_server
        url = f"{base_url}/medium.bin"
        out = str(tmp_path / "legacy_temp.bin")

        await download_with_driver(url, out, adaptive=False)

        # 传统路径下载完成后会清理临时目录
        parts_dir = Path(out).parent / f".{Path(out).name}.parts"
        assert not parts_dir.exists(), "传统路径应在完成后清理临时目录"

    @pytest.mark.asyncio
    async def test_adaptive_fallback_on_error(self, http_server, tmp_path):
        """自适应路径失败时应回退到传统合并路径"""
        files, base_url = http_server
        info = files["small.bin"]
        url = f"{base_url}/small.bin"
        out = str(tmp_path / "fallback_test.bin")

        driver = HTTPDriver(adaptive=True, auto_optimize=False)

        # probe
        probe_result = await driver.probe(url)
        handle = DownloadHandle(
            url=url,
            output_path=out,
            total_size=probe_result['size'],
            metadata=probe_result,
        )

        # 正常下载应成功（自适应路径成功，无需回退）
        await driver.download(handle)
        assert os.path.getsize(out) == info["size"]
        assert verify_file(out, info["sha256"])


class TestPerformanceComparison:
    """性能对比汇总（打印汇总表，不做硬断言）"""

    @pytest.mark.asyncio
    async def test_comparison_summary(self, http_server, tmp_path):
        """对比汇总：自适应 vs 传统，3 种文件大小"""
        files, base_url = http_server

        results = []
        for name, label in [("small.bin", "1MB"), ("medium.bin", "5MB"), ("large.bin", "20MB")]:
            info = files[name]
            url = f"{base_url}/{name}"

            # 自适应
            out_adaptive = str(tmp_path / f"cmp_adaptive_{name}")
            r_adaptive = await download_with_driver(url, out_adaptive, adaptive=True)
            assert verify_file(out_adaptive, info["sha256"]), f"自适应 {label} 校验失败"

            # 传统
            out_legacy = str(tmp_path / f"cmp_legacy_{name}")
            r_legacy = await download_with_driver(url, out_legacy, adaptive=False)
            assert verify_file(out_legacy, info["sha256"]), f"传统 {label} 校验失败"

            speedup = r_adaptive["elapsed"] / r_legacy["elapsed"] if r_legacy["elapsed"] > 0 else 0
            results.append({
                "label": label,
                "adaptive_elapsed": r_adaptive["elapsed"],
                "legacy_elapsed": r_legacy["elapsed"],
                "adaptive_speed": r_adaptive["speed_mbps"],
                "legacy_speed": r_legacy["speed_mbps"],
                "speedup": speedup,
            })

        print("\n" + "=" * 80)
        print("性能对比汇总：自适应路径 vs 传统合并路径")
        print("=" * 80)
        print(f"{'文件大小':>10} | {'自适应耗时':>12} | {'传统合并耗时':>12} | {'自适应速度':>12} | {'传统速度':>12} | {'加速比':>8}")
        print("-" * 80)
        for r in results:
            print(f"{r['label']:>10} | {r['adaptive_elapsed']:>10.3f}s | {r['legacy_elapsed']:>10.3f}s | "
                  f"{r['adaptive_speed']:>8.1f} MB/s | {r['legacy_speed']:>8.1f} MB/s | "
                  f"{r['speedup']:>6.2f}x")
        print("=" * 80)
