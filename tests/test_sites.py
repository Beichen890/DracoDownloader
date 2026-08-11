"""
站点专用探嗅规则测试（GitHub / Bilibili）

运行: python -m pytest tests/test_sites.py -v
"""

import json
import asyncio
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader.sites import (
    _GitHubRule, _BilibiliRule, _is_github_page_url,
    _with_proxy_candidates, _GITHUB_PROXIES,
)
from DracoDownloader.sniffer import ResourceType, SniffedResource


# =========================================================================
# GitHub 规则
# =========================================================================

class TestGitHubUrlRecognition:
    def test_release_tag_page(self):
        assert _is_github_page_url(
            "https://github.com/BurntSushi/ripgrep/releases/tag/14.1.1"
        )

    def test_repo_root(self):
        assert _is_github_page_url("https://github.com/aio-libs/aiohttp")
        assert _is_github_page_url("https://github.com/aio-libs/aiohttp/")

    def test_blob_page(self):
        assert _is_github_page_url(
            "https://github.com/aio-libs/aiohttp/blob/master/README.rst"
        )

    def test_releases_index(self):
        assert _is_github_page_url(
            "https://github.com/aio-libs/aiohttp/releases"
        )

    def test_raw_direct_link_not_page(self):
        assert not _is_github_page_url(
            "https://raw.githubusercontent.com/aio-libs/aiohttp/master/README.rst"
        )

    def test_archive_direct_link_not_page(self):
        assert not _is_github_page_url(
            "https://github.com/aio-libs/aiohttp/archive/refs/heads/master.zip"
        )

    def test_release_download_direct_link_not_page(self):
        assert not _is_github_page_url(
            "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep.tar.gz"
        )

    def test_non_github_host(self):
        assert not _is_github_page_url("https://gitlab.com/foo/bar")
        assert not _is_github_page_url("https://pypi.org/project/requests/")


class TestGitHubRuleMatch:
    def setup_method(self):
        self.rule = _GitHubRule()

    def test_match_release_tag(self):
        assert self.rule.match(
            "https://github.com/o/r/releases/tag/v1.0", "", ""
        )

    def test_match_repo_root(self):
        assert self.rule.match("https://github.com/o/r", "", "")

    def test_match_blob(self):
        assert self.rule.match(
            "https://github.com/o/r/blob/main/file.py", "", ""
        )

    def test_no_match_raw(self):
        assert not self.rule.match(
            "https://raw.githubusercontent.com/o/r/main/file.py", "", ""
        )

    def test_no_match_non_github(self):
        assert not self.rule.match("https://example.com", "", "")


class TestGitHubRuleExtract:
    def setup_method(self):
        self.rule = _GitHubRule()

    async def test_blob_to_raw(self):
        """blob 页应转为 raw.githubusercontent.com 直链"""
        url = "https://github.com/aio-libs/aiohttp/blob/master/README.rst"
        results = await self.rule.extract_async(url, "", http_client=None)
        assert len(results) == 4  # 原直链 + 3 个代理
        assert results[0].url == (
            "https://raw.githubusercontent.com/aio-libs/aiohttp/master/README.rst"
        )
        assert results[0].source == "github:direct"
        assert results[0].confidence == 0.95
        # 代理候选
        assert results[1].source.startswith("github:proxy:")
        assert results[1].url.startswith("https://gh-proxy.com/")

    async def test_repo_root_to_archive(self):
        """仓库主页应生成 archive 直链"""
        url = "https://github.com/aio-libs/aiohttp"
        results = await self.rule.extract_async(url, "", http_client=None)
        assert len(results) == 4
        assert results[0].url == (
            "https://github.com/aio-libs/aiohttp/archive/refs/heads/master.zip"
        )
        assert results[0].type == ResourceType.ARCHIVE
        assert "master.zip" in results[0].label

    async def test_release_tag_via_api(self):
        """release tag 页应调 API 获取 assets"""
        api_response = {
            "tag_name": "v1.0",
            "assets": [
                {
                    "name": "app-linux.tar.gz",
                    "size": 1024000,
                    "browser_download_url": "https://github.com/o/r/releases/download/v1.0/app-linux.tar.gz",
                },
                {
                    "name": "app-win.zip",
                    "size": 2048000,
                    "browser_download_url": "https://github.com/o/r/releases/download/v1.0/app-win.zip",
                },
            ],
        }
        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(
            return_value=(json.dumps(api_response), "application/json", "url")
        )

        url = "https://github.com/o/r/releases/tag/v1.0"
        results = await self.rule.extract_async(url, "", http_client=mock_http)

        # 2 assets × 4（原+3代理）= 8
        assert len(results) == 8
        assert results[0].url == (
            "https://github.com/o/r/releases/download/v1.0/app-linux.tar.gz"
        )
        assert results[0].label == "app-linux.tar.gz"
        assert results[0].extra["size"] == 1024000
        assert results[0].extra["release_tag"] == "v1.0"
        assert results[0].type == ResourceType.ARCHIVE

    async def test_release_tag_api_failure_returns_empty(self):
        """API 失败应返回空列表，让通用规则兜底"""
        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(side_effect=RuntimeError("network"))

        url = "https://github.com/o/r/releases/tag/v1.0"
        results = await self.rule.extract_async(url, "", http_client=mock_http)
        assert results == []

    async def test_releases_index_gets_latest(self):
        """releases 索引页应先取最新 release tag，再取 assets"""
        latest_response = {"tag_name": "v2.0"}
        release_response = {
            "tag_name": "v2.0",
            "assets": [{
                "name": "app.tar.gz",
                "size": 500,
                "browser_download_url": "https://github.com/o/r/releases/download/v2.0/app.tar.gz",
            }],
        }
        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(
            side_effect=[
                (json.dumps(latest_response), "application/json", "url"),
                (json.dumps(release_response), "application/json", "url"),
            ]
        )

        url = "https://github.com/o/r/releases"
        results = await self.rule.extract_async(url, "", http_client=mock_http)
        assert len(results) == 4  # 1 asset × 4
        assert results[0].url == (
            "https://github.com/o/r/releases/download/v2.0/app.tar.gz"
        )

    async def test_no_http_client_raises(self):
        """release tag 解析无 HttpClient 应返回空（API 调用失败）"""
        url = "https://github.com/o/r/releases/tag/v1.0"
        results = await self.rule.extract_async(url, "", http_client=None)
        assert results == []


class TestProxyCandidates:
    def test_proxy_candidates_count(self):
        results = _with_proxy_candidates(
            "https://github.com/o/r/releases/download/v1.0/app.zip"
        )
        assert len(results) == 1 + len(_GITHUB_PROXIES)

    def test_first_is_direct_highest_confidence(self):
        results = _with_proxy_candidates("https://github.com/o/r/archive/main.zip")
        assert results[0].source == "github:direct"
        assert results[0].confidence == 0.95
        for r in results[1:]:
            assert r.confidence == 0.7
            assert r.source.startswith("github:proxy:")

    def test_proxy_url_format(self):
        results = _with_proxy_candidates("https://github.com/o/r/x.zip")
        for i, proxy in enumerate(_GITHUB_PROXIES):
            assert results[i + 1].url == f"{proxy.rstrip('/')}/https://github.com/o/r/x.zip"


# =========================================================================
# Bilibili 规则
# =========================================================================

class TestBilibiliRuleMatch:
    def setup_method(self):
        self.rule = _BilibiliRule()

    def test_match_bv(self):
        assert self.rule.match(
            "https://www.bilibili.com/video/BV1GJ411x7h7", "", ""
        )

    def test_match_av(self):
        assert self.rule.match(
            "https://www.bilibili.com/video/av170001", "", ""
        )

    def test_match_no_www(self):
        assert self.rule.match(
            "https://bilibili.com/video/BV1GJ411x7h7", "", ""
        )

    def test_no_match_non_video_page(self):
        assert not self.rule.match(
            "https://www.bilibili.com/bangumi/play/ep123", "", ""
        )

    def test_no_match_non_bilibili(self):
        assert not self.rule.match(
            "https://www.youtube.com/watch?v=abc", "", ""
        )


class TestBilibiliWbiSign:
    def setup_method(self):
        self.rule = _BilibiliRule()
        self.rule._mixin_key = "abc1234567890abcdef1234567890abcd"  # 32 字符

    def test_sign_adds_wts_and_wrid(self):
        params = {"bvid": "BV1xxx", "cid": 123, "qn": 80, "fnval": 16, "fourk": 1}
        signed = self.rule._sign_params(params)
        assert "wts" in signed
        assert "w_rid" in signed
        assert len(signed["w_rid"]) == 32  # md5 hex

    def test_sign_without_mixin_key_passthrough(self):
        rule = _BilibiliRule()
        assert rule._mixin_key == ""
        params = {"bvid": "BV1xxx", "cid": 123}
        signed = rule._sign_params(params)
        assert "wts" not in signed
        assert "w_rid" not in signed
        assert signed == params

    def test_sign_filters_special_chars(self):
        self.rule._mixin_key = "a" * 32
        params = {"fnval": "16'()"}
        signed = self.rule._sign_params(params)
        # '()* 应被过滤
        assert "'" not in str(signed["fnval"])
        assert "(" not in str(signed["fnval"])


class TestBilibiliPageParam:
    def setup_method(self):
        self.rule = _BilibiliRule()

    def test_single_page(self):
        assert self.rule._parse_page_param("p=1") == {1}

    def test_multiple_pages(self):
        assert self.rule._parse_page_param("p=1,3,5") == {1, 3, 5}

    def test_page_range(self):
        assert self.rule._parse_page_param("p=1-3") == {1, 2, 3}

    def test_page_mixed(self):
        assert self.rule._parse_page_param("p=1-3,5") == {1, 2, 3, 5}

    def test_no_param(self):
        assert self.rule._parse_page_param("") is None

    def test_invalid_param(self):
        assert self.rule._parse_page_param("p=abc") is None


class TestBilibiliStreamSelection:
    def setup_method(self):
        self.rule = _BilibiliRule()
        self.video_streams = [
            {"id": 32, "baseUrl": "https://x.com/480.mp4"},
            {"id": 80, "baseUrl": "https://x.com/1080.mp4"},
            {"id": 64, "baseUrl": "https://x.com/720.mp4"},
        ]
        self.audio_streams = [
            {"id": 30216, "baseUrl": "https://x.com/64k.m4a"},
            {"id": 30280, "baseUrl": "https://x.com/192k.m4a"},
            {"id": 30232, "baseUrl": "https://x.com/132k.m4a"},
        ]

    def test_select_video_prefer_default_quality(self):
        url = self.rule._select_video_stream(self.video_streams, [80, 64, 32])
        assert url == "https://x.com/1080.mp4"  # 默认 80

    def test_select_video_fallback_to_highest_acceptable(self):
        url = self.rule._select_video_stream(self.video_streams, [64, 32])
        # 80 不在 accept_quality，取最高 64
        assert url == "https://x.com/720.mp4"

    def test_select_video_empty_streams(self):
        assert self.rule._select_video_stream([], []) == ""

    def test_select_audio_highest_quality(self):
        url = self.rule._select_audio_stream(self.audio_streams)
        # 30280 (192K) 是最高
        assert url == "https://x.com/192k.m4a"

    def test_stream_url_from_backup(self):
        stream = {"backupUrl": ["https://x.com/backup.mp4"]}
        assert self.rule._stream_url(stream) == "https://x.com/backup.mp4"

    def test_stream_url_empty(self):
        assert self.rule._stream_url({}) == ""

    def test_find_stream_by_url(self):
        s = self.rule._find_stream_by_url(self.video_streams, "https://x.com/1080.mp4")
        assert s is not None
        assert s["id"] == 80

    def test_find_stream_by_url_not_found(self):
        assert self.rule._find_stream_by_url(self.video_streams, "https://x.com/nope") is None


class TestBilibiliRuleExtract:
    async def test_full_extract_flow(self):
        """完整探嗅流程：view API + playurl API → DASH 流"""
        rule = _BilibiliRule()
        rule._mixin_key = "a" * 32  # 跳过 WBI keys 获取

        view_response = json.dumps({
            "code": 0,
            "data": {
                "title": "测试视频",
                "pages": [
                    {"cid": 111, "part": "第一集", "page": 1},
                    {"cid": 222, "part": "第二集", "page": 2},
                ],
            },
        })
        play_response_1 = json.dumps({
            "code": 0,
            "data": {
                "accept_quality": [80, 64, 32],
                "dash": {
                    "video": [
                        {"id": 80, "baseUrl": "https://upos-sz.com/p1_1080.mp4"},
                        {"id": 64, "baseUrl": "https://upos-sz.com/p1_720.mp4"},
                    ],
                    "audio": [
                        {"id": 30280, "baseUrl": "https://upos-sz.com/p1_192k.m4a"},
                    ],
                },
            },
        })
        play_response_2 = json.dumps({
            "code": 0,
            "data": {
                "accept_quality": [80, 64, 32],
                "dash": {
                    "video": [
                        {"id": 80, "baseUrl": "https://upos-sz.com/p2_1080.mp4"},
                    ],
                    "audio": [
                        {"id": 30280, "baseUrl": "https://upos-sz.com/p2_192k.m4a"},
                    ],
                },
            },
        })

        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(side_effect=[
            (view_response, "application/json", "url"),
            (play_response_1, "application/json", "url"),
            (play_response_2, "application/json", "url"),
        ])

        # 不带 p 参数 → 全部分P
        url = "https://www.bilibili.com/video/BV1testxxx"
        results = await rule.extract_async(url, "", http_client=mock_http)

        assert len(results) == 2  # 2 个分P
        # 第一集
        assert results[0].url == "https://upos-sz.com/p1_1080.mp4"
        assert results[0].type == ResourceType.VIDEO
        assert results[0].source == "bilibili:video"
        assert "P1" in results[0].label
        assert "第一集" in results[0].label
        assert "1080P" in results[0].label
        assert results[0].extra["cid"] == 111
        assert results[0].extra["title"] == "测试视频"
        assert results[0].extra["audio_url"] == "https://upos-sz.com/p1_192k.m4a"
        assert results[0].extra["headers"]["Referer"] == "https://www.bilibili.com/video/BV1testxxx"
        # 第二集
        assert results[1].url == "https://upos-sz.com/p2_1080.mp4"
        assert "P2" in results[1].label

    async def test_extract_with_page_param(self):
        """带 ?p=2 参数只取第二集"""
        rule = _BilibiliRule()
        rule._mixin_key = "a" * 32

        view_response = json.dumps({
            "code": 0,
            "data": {
                "title": "测试",
                "pages": [
                    {"cid": 111, "part": "P1", "page": 1},
                    {"cid": 222, "part": "P2", "page": 2},
                ],
            },
        })
        play_response = json.dumps({
            "code": 0,
            "data": {
                "accept_quality": [80],
                "dash": {
                    "video": [{"id": 80, "baseUrl": "https://x.com/p2.mp4"}],
                    "audio": [{"id": 30280, "baseUrl": "https://x.com/p2.m4a"}],
                },
            },
        })

        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(side_effect=[
            (view_response, "application/json", "url"),
            (play_response, "application/json", "url"),
        ])

        url = "https://www.bilibili.com/video/BV1testxxx?p=2"
        results = await rule.extract_async(url, "", http_client=mock_http)

        assert len(results) == 1
        assert results[0].extra["cid"] == 222
        assert "P2" in results[0].label

    async def test_view_api_error_returns_empty(self):
        """view API 返回错误码应返回空"""
        rule = _BilibiliRule()
        rule._mixin_key = "a" * 32

        view_response = json.dumps({"code": -404, "message": "视频不存在"})

        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(side_effect=[
            (view_response, "application/json", "url"),
        ])

        url = "https://www.bilibili.com/video/BV1badxxx"
        results = await rule.extract_async(url, "", http_client=mock_http)
        assert results == []

    async def test_av_id_format(self):
        """av 开头的视频 ID 应正确传 avid 参数"""
        rule = _BilibiliRule()
        rule._mixin_key = "a" * 32

        view_response = json.dumps({
            "code": 0,
            "data": {
                "title": "av 测试",
                "pages": [{"cid": 999, "part": "", "page": 1}],
            },
        })
        play_response = json.dumps({
            "code": 0,
            "data": {
                "accept_quality": [80],
                "dash": {
                    "video": [{"id": 80, "baseUrl": "https://x.com/av.mp4"}],
                    "audio": [],
                },
            },
        })

        mock_http = MagicMock()
        mock_http.fetch_text = AsyncMock(side_effect=[
            (view_response, "application/json", "url"),
            (play_response, "application/json", "url"),
        ])

        url = "https://www.bilibili.com/video/av170001"
        results = await rule.extract_async(url, "", http_client=mock_http)

        assert len(results) == 1
        assert results[0].url == "https://x.com/av.mp4"
        # 验证 view API 调用用的是 avid
        view_call_url = mock_http.fetch_text.call_args_list[0].args[0]
        assert "avid=170001" in view_call_url
