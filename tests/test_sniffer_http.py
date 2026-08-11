"""
资源探嗅 & 反反爬 HttpClient 测试

覆盖:
  - sniffer: classify_url / is_direct_link / SniffResult.best /
             _MediaExtractor(HTML) / _JSONRule / _RegexRule / ResourceSniffer
  - http_client: UA 池 / AntiBotError / HttpClient 头构建 / Referer 注入 /
                 Retry-After 解析 / session 生命周期

运行: python -m pytest tests/test_sniffer_http.py -v
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader.sniffer import (
    ResourceSniffer, SniffResult, SniffedResource, ResourceType,
    SniffRule, classify_url, is_direct_link,
    _MediaExtractor, _HTMLRule, _JSONRule, _RegexRule,
)
from DracoDownloader.http_client import (
    HttpClient, AntiBotError,
    ANTI_BOT_STATUS_CODES, RETRYABLE_STATUS_CODES,
    random_user_agent, build_request_headers,
)
from DracoDownloader.errors import (
    ERR_ANTI_BOT, ERR_HTTP_STATUS, make_error,
    anti_bot_error, sniff_failed_error, no_direct_url_error,
)


# ===== classify_url / is_direct_link =====

class TestClassifyUrl:
    def test_m3u8(self):
        assert classify_url("https://example.com/stream.m3u8") == ResourceType.M3U8
        assert classify_url("https://example.com/stream.m3u") == ResourceType.M3U8

    def test_video(self):
        assert classify_url("https://example.com/video.mp4") == ResourceType.VIDEO
        assert classify_url("https://example.com/video.webm") == ResourceType.VIDEO
        assert classify_url("https://example.com/video.mkv") == ResourceType.VIDEO

    def test_audio(self):
        assert classify_url("https://example.com/audio.mp3") == ResourceType.AUDIO
        assert classify_url("https://example.com/audio.flac") == ResourceType.AUDIO

    def test_archive(self):
        assert classify_url("https://example.com/file.zip") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/file.7z") == ResourceType.ARCHIVE

    def test_torrent(self):
        assert classify_url("https://example.com/file.torrent") == ResourceType.TORRENT
        assert classify_url("magnet:?xt=urn:btih:abc123") == ResourceType.TORRENT

    def test_wheel_and_package_formats(self):
        """包管理产物后缀（实测发现 .whl 曾缺失）"""
        assert classify_url("https://files.pythonhosted.org/x/requests-2.34.2-py3-none-any.whl") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/pkg.deb") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/pkg.rpm") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/app.apk") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/lib.jar") == ResourceType.ARCHIVE

    def test_installer_and_image_formats(self):
        assert classify_url("https://example.com/app.exe") == ResourceType.BINARY
        assert classify_url("https://example.com/app.dmg") == ResourceType.BINARY
        assert classify_url("https://example.com/disk.iso") == ResourceType.BINARY
        assert classify_url("https://example.com/app.AppImage") == ResourceType.BINARY

    def test_compression_variants(self):
        assert classify_url("https://example.com/x.tgz") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/x.txz") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/x.tar.zst") == ResourceType.ARCHIVE
        assert classify_url("https://example.com/x.tbz2") == ResourceType.ARCHIVE

    def test_unknown(self):
        assert classify_url("https://example.com/page.html") == ResourceType.UNKNOWN
        assert classify_url("https://example.com/") == ResourceType.UNKNOWN

    def test_url_with_query(self):
        assert classify_url("https://example.com/video.mp4?token=abc&exp=123") == ResourceType.VIDEO

    def test_case_insensitive(self):
        assert classify_url("https://example.com/VIDEO.MP4") == ResourceType.VIDEO


class TestIsDirectLink:
    def test_direct(self):
        assert is_direct_link("https://example.com/video.mp4") is True
        assert is_direct_link("magnet:?xt=urn:btih:abc") is True

    def test_not_direct(self):
        assert is_direct_link("https://example.com/watch?v=123") is False
        assert is_direct_link("https://example.com/page.html") is False


# ===== SniffResult.best =====

class TestSniffResultBest:
    def test_empty(self):
        result = SniffResult(original_url="http://x")
        assert result.best is None

    def test_m3u8_priority_over_video(self):
        result = SniffResult(original_url="http://x")
        result.direct_urls = [
            SniffedResource(url="http://x/v.mp4", type=ResourceType.VIDEO, confidence=1.0),
            SniffedResource(url="http://x/s.m3u8", type=ResourceType.M3U8, confidence=0.5),
        ]
        assert result.best.type == ResourceType.M3U8

    def test_higher_confidence_same_type(self):
        result = SniffResult(original_url="http://x")
        result.direct_urls = [
            SniffedResource(url="http://x/v1.mp4", type=ResourceType.VIDEO, confidence=0.5),
            SniffedResource(url="http://x/v2.mp4", type=ResourceType.VIDEO, confidence=0.9),
        ]
        assert result.best.url == "http://x/v2.mp4"


# ===== _MediaExtractor (HTML) =====

class TestMediaExtractor:
    def test_video_tag_src(self):
        html = '<video src="https://example.com/movie.mp4"></video>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        urls = [r.url for r in p.results]
        assert "https://example.com/movie.mp4" in urls

    def test_source_tag_in_video(self):
        html = '<video><source src="https://example.com/hls.m3u8" type="application/x-mpegURL"></video>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert any(r.url == "https://example.com/hls.m3u8" for r in p.results)

    def test_a_tag_media_href(self):
        html = '<a href="/files/doc.pdf">Download</a>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        # 相对路径应被 urljoin 为绝对路径
        assert any(r.url == "https://example.com/files/doc.pdf" for r in p.results)

    def test_a_tag_non_media_ignored(self):
        html = '<a href="/about">About</a>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert len(p.results) == 0

    def test_meta_og_video(self):
        html = '<meta property="og:video" content="https://example.com/clip.mp4">'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert any(r.url == "https://example.com/clip.mp4" for r in p.results)

    def test_meta_og_image_low_confidence(self):
        html = '<meta property="og:image" content="https://example.com/cover.jpg">'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        imgs = [r for r in p.results if r.type == ResourceType.IMAGE]
        assert len(imgs) == 1
        assert imgs[0].confidence < 0.5

    def test_iframe_extracted(self):
        html = '<iframe src="https://example.com/player"></iframe>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert any("player" in r.url for r in p.results)

    def test_title_extraction(self):
        html = '<html><head><title>My Video Page</title></head><body></body></html>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert p.title == "My Video Page"

    def test_dedup(self):
        html = ('<video src="https://example.com/movie.mp4"></video>'
                '<a href="https://example.com/movie.mp4">link</a>')
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        urls = [r.url for r in p.results]
        assert urls.count("https://example.com/movie.mp4") == 1

    def test_json_ld_content_url(self):
        html = '''<script type="application/ld+json">
        {"@type":"VideoObject","contentUrl":"https://example.com/v.mp4"}
        </script>'''
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert any(r.url == "https://example.com/v.mp4" for r in p.results)

    def test_source_label_with_res(self):
        html = '<video><source src="https://example.com/v.mp4" res="1080" label="HD"></video>'
        p = _MediaExtractor("https://example.com/page")
        p.feed(html)
        assert len(p.results) == 1
        assert "1080p" in p.results[0].label


# ===== _HTMLRule / _JSONRule / _RegexRule =====

class TestSniffRules:
    def test_html_rule_match(self):
        rule = _HTMLRule()
        assert rule.match("http://x", "text/html", "<!doctype html><html>") is True
        assert rule.match("http://x", "application/json", "{}") is False

    def test_html_rule_extract(self):
        rule = _HTMLRule()
        html = '<video src="https://example.com/v.mp4"></video>'
        results = rule.extract("https://example.com/p", html)
        assert len(results) == 1
        assert results[0].url == "https://example.com/v.mp4"

    def test_json_rule_match(self):
        rule = _JSONRule()
        assert rule.match("http://x", "application/json", '{"a":1}') is True
        assert rule.match("http://x", "text/html", "<html>") is False

    def test_json_rule_extract_nested(self):
        rule = _JSONRule()
        text = json.dumps({
            "data": {
                "list": [
                    {"url": "https://example.com/a.mp4"},
                    {"url": "https://example.com/b.m3u8"},
                ]
            }
        })
        results = rule.extract("https://example.com/api", text)
        urls = {r.url for r in results}
        assert "https://example.com/a.mp4" in urls
        assert "https://example.com/b.m3u8" in urls

    def test_json_rule_invalid_json(self):
        rule = _JSONRule()
        assert rule.extract("http://x", "not json{") == []

    def test_regex_rule_always_matches(self):
        rule = _RegexRule()
        assert rule.match("http://x", "text/plain", "anything") is True

    def test_regex_rule_quoted_url(self):
        rule = _RegexRule()
        text = 'var url = "https://example.com/secret.m3u8?token=abc";'
        results = rule.extract("https://example.com/p", text)
        assert any("secret.m3u8" in r.url for r in results)

    def test_regex_rule_data_attr(self):
        rule = _RegexRule()
        text = '<div data-src="https://example.com/clip.mp4"></div>'
        results = rule.extract("https://example.com/p", text)
        assert any("clip.mp4" in r.url for r in results)

    def test_regex_rule_relative_path(self):
        """正则应匹配相对路径（如 GitHub 的 /owner/repo/archive/master.zip）"""
        rule = _RegexRule()
        text = '"zipballUrl":"/owner/repo/archive/refs/heads/master.zip"'
        results = rule.extract("https://github.com/owner/repo", text)
        assert len(results) == 1
        assert results[0].url == "https://github.com/owner/repo/archive/refs/heads/master.zip"
        assert results[0].type == ResourceType.ARCHIVE

    def test_regex_rule_relative_path_with_query(self):
        rule = _RegexRule()
        text = '"url":"/files/video.mp4?token=abc"'
        results = rule.extract("https://example.com/page", text)
        assert len(results) == 1
        assert results[0].url == "https://example.com/files/video.mp4?token=abc"

    def test_regex_rule_wheel_extension(self):
        """正则应匹配 .whl 后缀（实测发现曾缺失）"""
        rule = _RegexRule()
        text = '"url":"https://files.pythonhosted.org/x/requests-2.34.2-py3-none-any.whl"'
        results = rule.extract("https://example.com/p", text)
        assert len(results) == 1
        assert results[0].type == ResourceType.ARCHIVE


# ===== ResourceSniffer =====

class TestResourceSniffer:
    async def test_direct_link_no_fetch(self):
        """直链不应触发网络请求"""
        sniffer = ResourceSniffer()
        result = await sniffer.sniff("https://example.com/video.mp4")
        assert result.is_direct is True
        assert len(result.direct_urls) == 1
        assert result.direct_urls[0].url == "https://example.com/video.mp4"
        assert result.direct_urls[0].confidence == 1.0

    async def test_direct_link_cached(self):
        sniffer = ResourceSniffer()
        r1 = await sniffer.sniff("https://example.com/v.mp4")
        r2 = await sniffer.sniff("https://example.com/v.mp4")
        assert r1 is r2  # 缓存命中，同一对象

    async def test_sniff_html_page(self):
        sniffer = ResourceSniffer()
        html = '<html><head><title>Test</title></head><body>' \
               '<video src="https://cdn.example.com/movie.mp4"></video>' \
               '</body></html>'
        with patch.object(sniffer, '_fetch_page', new=AsyncMock(
            return_value=(html, "text/html", "https://example.com/watch")
        )):
            result = await sniffer.sniff("https://example.com/watch")
        assert result.sniffed is True
        assert result.is_direct is False
        assert result.page_title == "Test"
        assert len(result.direct_urls) == 1
        assert result.direct_urls[0].url == "https://cdn.example.com/movie.mp4"
        assert result.best.url == "https://cdn.example.com/movie.mp4"

    async def test_sniff_no_resources(self):
        sniffer = ResourceSniffer()
        with patch.object(sniffer, '_fetch_page', new=AsyncMock(
            return_value=("<html><body>no media</body></html>", "text/html", "http://x")
        )):
            result = await sniffer.sniff("https://example.com/empty")
        assert result.sniffed is True
        assert len(result.direct_urls) == 0
        assert result.best is None

    async def test_sniff_fetch_error(self):
        sniffer = ResourceSniffer()
        with patch.object(sniffer, '_fetch_page', new=AsyncMock(
            side_effect=RuntimeError("network down")
        )):
            result = await sniffer.sniff("https://example.com/broken")
        # sniffed=True 表示已尝试探嗅（站点规则 + 页面拉取都失败）
        assert result.sniffed is True
        assert result.error is not None
        assert "network down" in result.error
        assert len(result.direct_urls) == 0

    async def test_sniff_m3u8_preferred_over_mp4(self):
        sniffer = ResourceSniffer()
        html = ('<video src="https://x.com/v.mp4"></video>'
                '<a href="https://x.com/s.m3u8">stream</a>')
        with patch.object(sniffer, '_fetch_page', new=AsyncMock(
            return_value=(html, "text/html", "https://x.com/p")
        )):
            result = await sniffer.sniff("https://x.com/p")
        assert result.best.type == ResourceType.M3U8

    def test_register_custom_rule(self):
        sniffer = ResourceSniffer()

        class MyRule(SniffRule):
            name = "custom"
            def match(self, url, content_type, text):
                return False
            def extract(self, url, text):
                return []

        sniffer.register_rule(MyRule(), priority=0)
        assert sniffer.list_rules()[0] == "custom"

    def test_clear_cache(self):
        sniffer = ResourceSniffer()
        sniffer._cache["http://x"] = SniffResult(original_url="http://x")
        sniffer.clear_cache()
        assert len(sniffer._cache) == 0


# ===== random_user_agent / build_request_headers =====

class TestUserAgent:
    def test_random_ua_desktop(self):
        ua = random_user_agent(mobile=False)
        assert isinstance(ua, str)
        assert "Mozilla" in ua

    def test_random_ua_mobile(self):
        ua = random_user_agent(mobile=True)
        assert "Mobile" in ua or "iPhone" in ua or "Android" in ua

    def test_build_request_headers_default(self):
        h = build_request_headers()
        assert "User-Agent" in h
        assert "Accept" in h
        assert "Accept-Language" in h

    def test_build_request_headers_with_extra(self):
        h = build_request_headers(extra={"X-Custom": "1"}, referer="http://ref")
        assert h["X-Custom"] == "1"
        assert h["Referer"] == "http://ref"


# ===== AntiBotError =====

class TestAntiBotError:
    def test_rate_limited(self):
        e = AntiBotError(status=429, url="http://x", retry_after=30.0)
        assert e.is_rate_limited is True
        assert e.is_forbidden is False
        assert e.needs_auth is False

    def test_forbidden(self):
        e = AntiBotError(status=403, url="http://x")
        assert e.is_forbidden is True
        assert e.is_rate_limited is False

    def test_needs_auth(self):
        e = AntiBotError(status=401, url="http://x")
        assert e.needs_auth is True

    def test_to_dict(self):
        e = AntiBotError(status=429, url="http://x", retry_after=10.0,
                         message="rate limited", context={"attempts": 2})
        d = e.to_dict()
        assert d["status"] == 429
        assert d["url"] == "http://x"
        assert d["retry_after"] == 10.0
        assert d["context"]["attempts"] == 2

    def test_is_exception(self):
        e = AntiBotError(status=403, url="http://x")
        assert isinstance(e, Exception)
        with pytest.raises(AntiBotError):
            raise e

    def test_status_codes_sets(self):
        assert 403 in ANTI_BOT_STATUS_CODES
        assert 412 in ANTI_BOT_STATUS_CODES  # B 站反爬码
        assert 429 in ANTI_BOT_STATUS_CODES
        assert 412 in RETRYABLE_STATUS_CODES  # 412 可重试
        assert 429 in RETRYABLE_STATUS_CODES


# ===== HttpClient =====

class TestHttpClient:
    def test_build_headers_fixed_ua(self):
        client = HttpClient(user_agent="MyBot/1.0", rotate_ua=False)
        h = client._build_headers()
        assert h["User-Agent"] == "MyBot/1.0"

    def test_build_headers_rotating_ua(self):
        client = HttpClient(rotate_ua=True)
        h1 = client._build_headers()
        h2 = client._build_headers()
        assert "User-Agent" in h1
        # 轮换可能相同（随机），但都应是合法 UA
        assert "Mozilla" in h1["User-Agent"]
        assert "Mozilla" in h2["User-Agent"]

    def test_build_headers_with_extra(self):
        client = HttpClient(user_agent="Bot/1.0")
        h = client._build_headers(extra={"Referer": "http://ref"})
        assert h["Referer"] == "http://ref"
        assert h["User-Agent"] == "Bot/1.0"

    def test_remember_page_referer(self):
        client = HttpClient()
        assert client._get_referer("https://cdn.example.com/v.mp4") is None
        client.remember_page("https://www.example.com/watch")
        # 同 host 的页面 URL 应作为 Referer
        assert client._get_referer("https://www.example.com/other") == "https://www.example.com/watch"
        # 不同 host 无 Referer
        assert client._get_referer("https://cdn.example.com/v.mp4") is None

    def test_remember_page_invalid_url(self):
        client = HttpClient()
        client.remember_page("not a url")  # 不应抛异常
        assert client._get_referer("http://x") is None

    async def test_start_close_lifecycle(self):
        client = HttpClient()
        assert client._session is None
        await client.start()
        assert client._session is not None
        assert not client._session.closed
        await client.close()
        assert client._session is None

    async def test_start_idempotent(self):
        client = HttpClient()
        await client.start()
        s1 = client._session
        await client.start()  # 再次启动不应重建
        assert client._session is s1

    async def test_close_idempotent(self):
        client = HttpClient()
        await client.start()
        await client.close()
        await client.close()  # 再次关闭不应抛异常

    async def test_session_property_not_started(self):
        client = HttpClient()
        with pytest.raises(RuntimeError):
            _ = client.session

    async def test_context_manager(self):
        async with HttpClient() as client:
            assert client._session is not None
            assert not client._session.closed
        assert client._session is None

    def test_brotli_detection_removes_br_when_unavailable(self):
        """未安装 brotli 时应从 Accept-Encoding 移除 br，避免收到无法解压的响应"""
        import sys
        # 模拟 brotli 和 brotlicffi 都不可用
        old_brotli = sys.modules.get('brotli')
        old_brotlicffi = sys.modules.get('brotlicffi')
        sys.modules['brotli'] = None  # 触发 ImportError
        sys.modules['brotlicffi'] = None
        try:
            client = HttpClient()
            ae = client._default_headers.get('Accept-Encoding', '')
            assert 'br' not in ae, f"br 应被移除，实际: {ae}"
            assert 'gzip' in ae  # gzip 应保留
        finally:
            if old_brotli is not None:
                sys.modules['brotli'] = old_brotli
            else:
                sys.modules.pop('brotli', None)
            if old_brotlicffi is not None:
                sys.modules['brotlicffi'] = old_brotlicffi
            else:
                sys.modules.pop('brotlicffi', None)

    def test_brotli_available_keeps_br(self):
        """已安装 brotli 时应保留 br（当前测试环境已装 brotli）"""
        client = HttpClient()
        ae = client._default_headers.get('Accept-Encoding', '')
        # 当前测试环境装了 brotli，应保留 br
        assert 'br' in ae, f"br 应保留（brotli 已安装），实际: {ae}"

    def test_parse_retry_after_numeric(self):
        client = HttpClient()
        resp = MagicMock()
        resp.headers = {"Retry-After": "120"}
        assert client._parse_retry_after(resp) == 120.0

    def test_parse_retry_after_missing(self):
        client = HttpClient()
        resp = MagicMock()
        resp.headers = {}
        assert client._parse_retry_after(resp) is None

    def test_parse_retry_after_invalid(self):
        client = HttpClient()
        resp = MagicMock()
        resp.headers = {"Retry-After": "not-a-date-or-number!!"}
        assert client._parse_retry_after(resp) is None

    def test_cookies_property(self):
        client = HttpClient()
        # cookie_jar 为空时应返回空 dict
        assert client.cookies == {}


# ===== HttpClient.request 反爬重试逻辑 =====

class TestHttpClientRequest:
    async def test_request_success(self):
        client = HttpClient(max_retries=0)
        await client.start()
        try:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.headers = {}
            mock_resp.release = MagicMock()
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=None)
            with patch.object(client._session, 'request',
                              new=AsyncMock(return_value=mock_resp)):
                resp = await client.request('GET', 'http://example.com')
                assert resp.status == 200
        finally:
            await client.close()

    async def test_request_403_raises_antibot(self):
        client = HttpClient(max_retries=0)
        await client.start()
        try:
            mock_resp = MagicMock()
            mock_resp.status = 403
            mock_resp.headers = {}
            mock_resp.release = MagicMock()
            with patch.object(client._session, 'request',
                              new=AsyncMock(return_value=mock_resp)):
                with pytest.raises(AntiBotError) as exc_info:
                    await client.request('GET', 'http://example.com')
                assert exc_info.value.status == 403
                assert exc_info.value.is_forbidden is True
        finally:
            await client.close()

    async def test_request_429_retries_then_raises(self):
        client = HttpClient(max_retries=2)
        await client.start()
        try:
            mock_resp = MagicMock()
            mock_resp.status = 429
            mock_resp.headers = {"Retry-After": "0"}  # 立即重试
            mock_resp.release = MagicMock()
            with patch.object(client._session, 'request',
                              new=AsyncMock(return_value=mock_resp)):
                with patch('asyncio.sleep', new=AsyncMock()):
                    with pytest.raises(AntiBotError) as exc_info:
                        await client.request('GET', 'http://example.com')
                    assert exc_info.value.status == 429
                    # 应重试 max_retries 次
                    assert client._session.request.call_count == 3  # 1 + 2 retries
        finally:
            await client.close()

    async def test_request_404_raises_draco_error(self):
        client = HttpClient(max_retries=0)
        await client.start()
        try:
            mock_resp = MagicMock()
            mock_resp.status = 404
            mock_resp.headers = {}
            mock_resp.release = MagicMock()
            with patch.object(client._session, 'request',
                              new=AsyncMock(return_value=mock_resp)):
                from DracoDownloader.errors import DracoError
                with pytest.raises(DracoError) as exc_info:
                    await client.request('GET', 'http://example.com')
                assert exc_info.value.code == ERR_HTTP_STATUS
        finally:
            await client.close()


# ===== 错误工厂函数 =====

class TestErrorFactories:
    def test_anti_bot_error(self):
        e = anti_bot_error(403, "http://x", hint="blocked")
        assert e.code == ERR_ANTI_BOT
        assert e.retryable is True
        assert "403" in e.message
        assert "blocked" in e.message

    def test_sniff_failed_error(self):
        e = sniff_failed_error("timeout")
        assert e.code == "draco.sniff_failed"
        assert "timeout" in e.message

    def test_no_direct_url_error(self):
        e = no_direct_url_error("http://x/page")
        assert e.code == "draco.no_direct_url"
        assert "http://x/page" in e.message
