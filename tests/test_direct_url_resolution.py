"""
直链解析（core 层）端到端测试

验证 DracoDownloader._resolve_direct_url 与 download_async 的直链解析路径：
  1. 直链直接返回（不走探嗅）
  2. 非 HTTP 协议不探嗅（magnet/ftp）
  3. 页面 URL → 探嗅 → 返回 best 直链 + sniff_info
  4. 探嗅被反爬拦截 → anti_bot_error
  5. 探嗅无结果 → no_direct_url_error
  6. remember_page 被调用（Referer 注入）
  7. download_async 端到端：页面 URL → 自动解析 → 路由到 HTTPDriver

运行: python -m pytest tests/test_direct_url_resolution.py -v
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader import DracoDownloader, DownloadResult
from DracoDownloader.sniffer import SniffResult, SniffedResource, ResourceType
from DracoDownloader.http_client import AntiBotError
from DracoDownloader.errors import (
    DracoError, ERR_ANTI_BOT, ERR_SNIFF_FAILED, ERR_NO_DIRECT_URL,
    anti_bot_error, sniff_failed_error, no_direct_url_error,
)


# ===== _resolve_direct_url =====

class TestResolveDirectUrl:
    async def test_direct_link_passthrough(self):
        """直链直接返回，不触发探嗅"""
        async with DracoDownloader() as d:
            with patch.object(d, 'sniff', new=AsyncMock()) as mock_sniff:
                url, sniff_info = await d._resolve_direct_url(
                    "https://example.com/video.mp4"
                )
            assert url == "https://example.com/video.mp4"
            assert sniff_info is None
            mock_sniff.assert_not_called()  # 直链不应触发探嗅

    async def test_magnet_not_sniffed(self):
        """磁力链接不探嗅，原样返回"""
        async with DracoDownloader() as d:
            with patch.object(d, 'sniff', new=AsyncMock()) as mock_sniff:
                magnet = "magnet:?xt=urn:btih:abc123&dn=test"
                url, sniff_info = await d._resolve_direct_url(magnet)
            assert url == magnet
            assert sniff_info is None
            mock_sniff.assert_not_called()

    async def test_ftp_not_sniffed(self):
        """FTP 协议不探嗅"""
        async with DracoDownloader() as d:
            with patch.object(d, 'sniff', new=AsyncMock()) as mock_sniff:
                url, sniff_info = await d._resolve_direct_url("ftp://example.com/file.zip")
            assert url == "ftp://example.com/file.zip"
            assert sniff_info is None
            mock_sniff.assert_not_called()

    async def test_page_url_triggers_sniff(self):
        """页面 URL 触发探嗅，返回 best 直链"""
        async with DracoDownloader() as d:
            sniff_result = SniffResult(original_url="https://www.example.com/watch")
            sniff_result.direct_urls = [
                SniffedResource(
                    url="https://cdn.example.com/movie.mp4",
                    type=ResourceType.VIDEO,
                    source="video_tag",
                    confidence=0.95,
                    label="1080p",
                ),
                SniffedResource(
                    url="https://cdn.example.com/stream.m3u8",
                    type=ResourceType.M3U8,
                    source="source_tag",
                    confidence=0.9,
                ),
            ]
            with patch.object(d, 'sniff', new=AsyncMock(return_value=sniff_result)):
                url, sniff_info = await d._resolve_direct_url(
                    "https://www.example.com/watch"
                )
            # best 应优先 M3U8
            assert url == "https://cdn.example.com/stream.m3u8"
            assert sniff_info is not None
            assert sniff_info['original_url'] == "https://www.example.com/watch"
            assert sniff_info['direct_url'] == "https://cdn.example.com/stream.m3u8"
            assert sniff_info['type'] == "m3u8"
            assert sniff_info['source'] == "source_tag"
            assert sniff_info['confidence'] == 0.9
            assert len(sniff_info['all_candidates']) == 2

    async def test_remember_page_called(self):
        """探嗅成功后应调用 remember_page（为 Referer 注入做准备）"""
        async with DracoDownloader() as d:
            sniff_result = SniffResult(original_url="https://www.example.com/watch")
            sniff_result.direct_urls = [
                SniffedResource(
                    url="https://cdn.example.com/v.mp4",
                    type=ResourceType.VIDEO, source="video_tag", confidence=0.9,
                ),
            ]
            with patch.object(d, 'sniff', new=AsyncMock(return_value=sniff_result)):
                with patch.object(d._http_client, 'remember_page') as mock_remember:
                    await d._resolve_direct_url("https://www.example.com/watch")
            mock_remember.assert_called_once_with("https://www.example.com/watch")

    async def test_antibot_raises_anti_bot_error(self):
        """探嗅阶段被反爬拦截 → anti_bot_error"""
        async with DracoDownloader() as d:
            with patch.object(d, 'sniff', new=AsyncMock(
                side_effect=AntiBotError(status=403, url="https://www.example.com/watch")
            )):
                with pytest.raises(DracoError) as exc_info:
                    await d._resolve_direct_url("https://www.example.com/watch")
            assert exc_info.value.code == ERR_ANTI_BOT
            assert exc_info.value.retryable is True
            assert "403" in exc_info.value.message

    async def test_no_direct_url_raises(self):
        """探嗅无结果 → no_direct_url_error"""
        async with DracoDownloader() as d:
            empty_result = SniffResult(original_url="https://www.example.com/empty")
            with patch.object(d, 'sniff', new=AsyncMock(return_value=empty_result)):
                with pytest.raises(DracoError) as exc_info:
                    await d._resolve_direct_url("https://www.example.com/empty")
            assert exc_info.value.code == ERR_NO_DIRECT_URL
            assert "https://www.example.com/empty" in exc_info.value.message

    async def test_sniff_exception_wrapped(self):
        """探嗅阶段其他异常 → sniff_failed_error"""
        async with DracoDownloader() as d:
            with patch.object(d, 'sniff', new=AsyncMock(
                side_effect=RuntimeError("network down")
            )):
                with pytest.raises(DracoError) as exc_info:
                    await d._resolve_direct_url("https://www.example.com/x")
            assert exc_info.value.code == ERR_SNIFF_FAILED
            assert "network down" in exc_info.value.message

    async def test_sniff_info_best_none_raises(self):
        """best 为 None（direct_urls 非空但 best 计算 None）→ no_direct_url_error

        边界：best 在 direct_urls 非空时一般不为 None，但防御性测试覆盖。
        """
        async with DracoDownloader() as d:
            sniff_result = SniffResult(original_url="https://www.example.com/x")
            sniff_result.direct_urls = [
                SniffedResource(
                    url="https://cdn.example.com/v.mp4",
                    type=ResourceType.VIDEO, source="s", confidence=0.5,
                ),
            ]
            with patch.object(d, 'sniff', new=AsyncMock(return_value=sniff_result)):
                with patch.object(SniffResult, 'best', new=None):
                    with pytest.raises(DracoError) as exc_info:
                        await d._resolve_direct_url("https://www.example.com/x")
            assert exc_info.value.code == ERR_NO_DIRECT_URL


# ===== download_async 端到端（探嗅集成）=====

class TestDownloadAsyncSniffIntegration:
    async def test_direct_link_download_no_sniff(self, tmp_path):
        """直链下载不触发探嗅"""
        out = str(tmp_path / "out.bin")
        async with DracoDownloader() as d:
            with patch.object(d, '_resolve_direct_url',
                              new=AsyncMock(return_value=("https://x/v.mp4", None))) as mock_resolve:
                with patch.object(d, '_resolve_mirror_url',
                                  new=AsyncMock(return_value=("https://x/v.mp4", None))):
                    # driver.probe 返回小文件走单线程路径
                    driver = d.router.route("https://x/v.mp4")
                    with patch.object(driver, 'probe', new=AsyncMock(
                        return_value={'size': 100, 'supports_range': False,
                                      'filename': 'v.mp4', 'content_type': '', 'url': 'https://x/v.mp4'}
                    )):
                        with patch.object(driver, 'download', new=AsyncMock()):
                            result = await d.download_async("https://x/v.mp4", out)
            mock_resolve.assert_called_once()

    async def test_page_url_download_triggers_sniff(self, tmp_path):
        """页面 URL 下载自动触发探嗅，并用解析出的直链路由"""
        out = str(tmp_path / "out.mp4")
        async with DracoDownloader() as d:
            sniff_info = {'direct_url': 'https://cdn.x.com/movie.mp4',
                          'type': 'video', 'original_url': 'https://www.x.com/watch'}
            with patch.object(d, '_resolve_direct_url',
                              new=AsyncMock(return_value=("https://cdn.x.com/movie.mp4", sniff_info))):
                with patch.object(d, '_resolve_mirror_url',
                                  new=AsyncMock(return_value=("https://cdn.x.com/movie.mp4", None))):
                    driver = d.router.route("https://cdn.x.com/movie.mp4")
                    captured_url = {}

                    async def fake_probe(url):
                        captured_url['url'] = url
                        return {'size': 0, 'supports_range': False,
                                'filename': 'movie.mp4', 'content_type': '', 'url': url}

                    with patch.object(driver, 'probe', new=fake_probe):
                        with patch.object(driver, 'download', new=AsyncMock()):
                            result = await d.download_async(
                                "https://www.x.com/watch", out
                            )
            # 验证 driver 收到的是解析后的直链，不是原始页面 URL
            assert captured_url['url'] == "https://cdn.x.com/movie.mp4"
            # 验证结果带 sniffed 信息
            assert result.sniffed == sniff_info

    async def test_sniff_failure_returns_failed_result(self, tmp_path):
        """探嗅失败 → 返回失败结果（不抛异常）"""
        out = str(tmp_path / "out.mp4")
        async with DracoDownloader() as d:
            with patch.object(d, '_resolve_direct_url',
                              new=AsyncMock(side_effect=no_direct_url_error("https://x/page"))):
                result = await d.download_async("https://x/page", out)
            assert result.success is False
            assert "未探嗅到可下载直链" in result.error

    async def test_antibot_failure_returns_failed_result(self, tmp_path):
        """探嗅被反爬拦截 → 返回失败结果"""
        out = str(tmp_path / "out.mp4")
        async with DracoDownloader() as d:
            with patch.object(d, '_resolve_direct_url',
                              new=AsyncMock(side_effect=anti_bot_error(403, "https://x/page", "blocked"))):
                result = await d.download_async("https://x/page", out)
            assert result.success is False
            assert "403" in result.error
            assert "反爬" in result.error


# ===== sniff() 公开接口 =====

class TestSniffPublicApi:
    async def test_sniff_starts_http_client(self):
        """sniff() 在 HttpClient 未启动时应自动启动"""
        d = DracoDownloader()
        try:
            assert d._http_client._session is None
            fake_result = SniffResult(original_url="https://x/p")
            with patch.object(d._sniffer, 'sniff',
                              new=AsyncMock(return_value=fake_result)):
                await d.sniff("https://x/p")
            assert d._http_client._session is not None
        finally:
            await d.close()

    async def test_sniff_passes_headers(self):
        """sniff() 应转发用户 headers"""
        async with DracoDownloader() as d:
            captured = {}
            fake_result = SniffResult(original_url="https://x/p")

            async def fake_sniff(url, headers=None):
                captured['url'] = url
                captured['headers'] = headers
                return fake_result

            with patch.object(d._sniffer, 'sniff', new=fake_sniff):
                await d.sniff("https://x/p", headers={"Referer": "http://ref"})
            assert captured['url'] == "https://x/p"
            assert captured['headers'] == {"Referer": "http://ref"}
