"""
BilibiliAuth 登录态模块测试

覆盖:
  - generate_qrcode: 成功 / API 错误
  - poll_login: 成功(cookie 提取) / 二维码过期 / 超时
  - login_async: 完整流程（mock 回调）
  - check_login: 已登录 / 游客 / API 错误
  - 持久化: save/load 往返 / load_and_inject 缺 SESSDATA / logout 删除文件
  - HttpClient.set_cookies / get_cookies 注入读取

运行: python -m pytest tests/test_auth.py -v
"""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from DracoDownloader.auth import BilibiliAuth
from DracoDownloader.http_client import HttpClient


# ===== 辅助：构造 mock HttpClient =====

def make_mock_http(fetch_text_side_effects=None,
                   cookies_return=None) -> MagicMock:
    """构造一个 mock HttpClient，fetch_text 按序列返回预设响应

    Args:
        fetch_text_side_effects: [(text, content_type, final_url), ...]
            每次调用 fetch_text 弹出一个；None 表示用默认
        cookies_return: get_cookies 返回值（模拟 jar 中的 cookie）
    """
    http = MagicMock()
    http.fetch_text = AsyncMock()
    if fetch_text_side_effects is not None:
        # AsyncMock 的 side_effect 为列表时，每次调用取下一项作为返回值
        http.fetch_text.side_effect = list(fetch_text_side_effects)
    http.get_cookies = MagicMock(return_value=cookies_return or {})
    http.set_cookies = MagicMock()
    http._cookie_jar = None
    return http


def qr_generate_response(url="https://bili.example/qr?key=K",
                         qrcode_key="K") -> str:
    return json.dumps({
        "code": 0, "message": "ok",
        "data": {"url": url, "qrcode_key": qrcode_key},
    })


def poll_response(code=0, message="", crossdomain_url="") -> str:
    data = {"code": code, "message": message or "ok"}
    if crossdomain_url:
        data["url"] = crossdomain_url
    return json.dumps({"code": 0, "data": data})


def nav_response(is_login=True, uname="测试用户", mid=12345) -> str:
    return json.dumps({
        "code": 0,
        "data": {
            "isLogin": is_login,
            "uname": uname if is_login else "",
            "mid": mid if is_login else 0,
            "vipStatus": 1 if is_login else 0,
        },
    })


# ===== generate_qrcode =====

class TestGenerateQrcode:

    async def test_success(self):
        http = make_mock_http([
            (qr_generate_response(), "application/json", "https://passport.bilibili.com/"),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        qr = await auth.generate_qrcode()
        assert qr["url"] == "https://bili.example/qr?key=K"
        assert qr["qrcode_key"] == "K"

    async def test_api_error_raises(self):
        http = make_mock_http([
            (json.dumps({"code": -101, "message": "访问过于频繁"}), "", ""),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        with pytest.raises(RuntimeError, match="生成二维码失败"):
            await auth.generate_qrcode()

    async def test_incomplete_data_raises(self):
        # 缺 qrcode_key
        http = make_mock_http([
            (json.dumps({"code": 0, "data": {"url": "https://x"}}), "", ""),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        with pytest.raises(RuntimeError, match="数据不完整"):
            await auth.generate_qrcode()


# ===== poll_login =====

class TestPollLogin:

    async def test_success_with_jar_cookies(self):
        # poll 返回 code=0，cookie 从 jar 读取
        http = make_mock_http(
            fetch_text_side_effects=[(poll_response(code=0), "", "https://passport.bilibili.com/")],
            cookies_return={"SESSDATA": "abc", "bili_jct": "def", "DedeUserID": "123"},
        )
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        result = await auth.poll_login("K", timeout=5)
        assert result["success"] is True
        assert result["code"] == 0
        assert result["cookies"]["SESSDATA"] == "abc"
        assert result["cookies"]["DedeUserID"] == "123"

    async def test_success_with_crossdomain_fallback(self):
        # jar 无 cookie，从 data.url 的 crossDomain 参数兜底
        crossdomain = ("https://passport.biligame.com/crossDomain"
                       "?DedeUserID=123&SESSDATA=xyz&bili_jct=jct1")
        http = make_mock_http(
            fetch_text_side_effects=[(poll_response(code=0, crossdomain_url=crossdomain), "", "")],
            cookies_return={},  # jar 空
        )
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        result = await auth.poll_login("K", timeout=5)
        assert result["success"] is True
        assert result["cookies"]["SESSDATA"] == "xyz"
        assert result["cookies"]["DedeUserID"] == "123"

    async def test_qrcode_expired(self):
        http = make_mock_http([
            (poll_response(code=86038, message="二维码已过期"), "", ""),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        result = await auth.poll_login("K", timeout=5)
        assert result["success"] is False
        assert result["code"] == 86038
        assert "过期" in result["message"]

    async def test_success_but_no_cookie(self):
        # code=0 但 jar 和 crossdomain 都无 cookie
        http = make_mock_http(
            fetch_text_side_effects=[(poll_response(code=0), "", "")],
            cookies_return={},
        )
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        result = await auth.poll_login("K", timeout=5)
        assert result["success"] is False
        assert "未捕获" in result["message"]

    async def test_timeout(self):
        # 一直返回未扫码(86039)，直到超时
        http = make_mock_http()
        http.fetch_text.side_effect = [
            (poll_response(code=86039, message="未扫码"), "", "")
        ] * 100  # 足够多次
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        result = await auth.poll_login("K", timeout=1)
        assert result["success"] is False
        assert result["code"] == -1
        assert "超时" in result["message"]

    async def test_status_callback_invoked(self):
        http = make_mock_http(
            fetch_text_side_effects=[
                (poll_response(code=86039, message="未扫码"), "", ""),
                (poll_response(code=86090, message="已扫码"), "", ""),
                (poll_response(code=0), "", ""),
            ],
            cookies_return={"SESSDATA": "x"},
        )
        auth = BilibiliAuth(http, storage_path=Path("/tmp/test_bili_auth.json"))
        statuses = []

        async def on_status(code, msg):
            statuses.append((code, msg))

        result = await auth.poll_login("K", timeout=10, on_status=on_status)
        assert result["success"] is True
        # 至少回调了 86039 / 86090 / 0 三种状态
        codes = [s[0] for s in statuses]
        assert 86039 in codes
        assert 86090 in codes
        assert 0 in codes


# ===== login_async（完整流程） =====

class TestLoginAsync:

    async def test_full_flow_success(self, tmp_path):
        storage = tmp_path / "bili.json"
        http = make_mock_http(
            fetch_text_side_effects=[
                # generate_qrcode
                (qr_generate_response(), "", ""),
                # poll_login → 成功
                (poll_response(code=0), "", ""),
                # check_login（nav）
                (nav_response(is_login=True, uname="用户A"), "", ""),
            ],
            cookies_return={"SESSDATA": "sess", "bili_jct": "jct"},
        )
        auth = BilibiliAuth(http, storage_path=storage)

        qr_urls = []

        async def on_qr(url):
            qr_urls.append(url)

        result = await auth.login_async(on_qrcode=on_qr, timeout=10)
        assert result["success"] is True
        assert result["username"] == "用户A"
        assert result["cookies"]["SESSDATA"] == "sess"
        assert len(qr_urls) == 1
        # cookie 注入到 http
        http.set_cookies.assert_called_with("bilibili.com", result["cookies"])
        # 持久化文件已写
        assert storage.exists()
        saved = json.loads(storage.read_text())
        assert saved["cookies"]["SESSDATA"] == "sess"

    async def test_full_flow_expired(self, tmp_path):
        storage = tmp_path / "bili.json"
        http = make_mock_http(
            fetch_text_side_effects=[
                (qr_generate_response(), "", ""),
                (poll_response(code=86038, message="二维码已过期"), "", ""),
            ],
        )
        auth = BilibiliAuth(http, storage_path=storage)
        result = await auth.login_async(timeout=10)
        assert result["success"] is False
        # 失败不写文件
        assert not storage.exists()


# ===== check_login =====

class TestCheckLogin:

    async def test_logged_in(self):
        http = make_mock_http([
            (nav_response(is_login=True, uname="UP主", mid=999, ), "", ""),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/x.json"))
        status = await auth.check_login()
        assert status["is_logged_in"] is True
        assert status["username"] == "UP主"
        assert status["uid"] == 999
        assert status["vip_status"] == 1

    async def test_guest(self):
        http = make_mock_http([
            (nav_response(is_login=False), "", ""),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/x.json"))
        status = await auth.check_login()
        assert status["is_logged_in"] is False
        assert status["username"] == ""
        assert status["uid"] == 0

    async def test_api_error(self):
        http = make_mock_http([
            (json.dumps({"code": -101, "message": "账号未登录"}), "", ""),
        ])
        auth = BilibiliAuth(http, storage_path=Path("/tmp/x.json"))
        status = await auth.check_login()
        assert status["is_logged_in"] is False
        assert "未登录" in status["message"]


# ===== 持久化 =====

class TestPersistence:

    def test_save_load_roundtrip(self, tmp_path):
        storage = tmp_path / "bili.json"
        http = make_mock_http()
        auth = BilibiliAuth(http, storage_path=storage)
        cookies = {"SESSDATA": "s1", "bili_jct": "j1", "DedeUserID": "u1"}
        auth.save_cookies(cookies)
        assert storage.exists()
        loaded = auth.load_cookies()
        assert loaded == cookies

    def test_load_nonexistent_returns_empty(self, tmp_path):
        storage = tmp_path / "noexist.json"
        http = make_mock_http()
        auth = BilibiliAuth(http, storage_path=storage)
        assert auth.load_cookies() == {}

    def test_load_corrupt_returns_empty(self, tmp_path):
        storage = tmp_path / "corrupt.json"
        storage.write_text("not json{", encoding="utf-8")
        http = make_mock_http()
        auth = BilibiliAuth(http, storage_path=storage)
        assert auth.load_cookies() == {}

    def test_load_and_inject_with_sessdata(self, tmp_path):
        storage = tmp_path / "bili.json"
        http = make_mock_http()
        auth = BilibiliAuth(http, storage_path=storage)
        auth.save_cookies({"SESSDATA": "s", "bili_jct": "j"})
        ok = auth.load_and_inject()
        assert ok is True
        http.set_cookies.assert_called_with(
            "bilibili.com", {"SESSDATA": "s", "bili_jct": "j"}
        )

    def test_load_and_inject_missing_sessdata_skipped(self, tmp_path):
        storage = tmp_path / "bili.json"
        http = make_mock_http()
        auth = BilibiliAuth(http, storage_path=storage)
        # 只有 bili_jct，缺 SESSDATA
        auth.save_cookies({"bili_jct": "j"})
        ok = auth.load_and_inject()
        assert ok is False
        http.set_cookies.assert_not_called()

    def test_load_and_inject_empty(self, tmp_path):
        storage = tmp_path / "empty.json"
        http = make_mock_http()
        auth = BilibiliAuth(http, storage_path=storage)
        ok = auth.load_and_inject()
        assert ok is False

    def test_logout_deletes_file(self, tmp_path):
        storage = tmp_path / "bili.json"
        http = make_mock_http()
        http.get_cookies = MagicMock(return_value={})
        auth = BilibiliAuth(http, storage_path=storage)
        auth.save_cookies({"SESSDATA": "s"})
        assert storage.exists()
        auth.logout()
        assert not storage.exists()


# ===== HttpClient.set_cookies / get_cookies =====

class TestHttpClientCookies:

    async def test_set_and_get_cookies(self):
        """真实 HttpClient：注入 cookie 后能读回"""
        async with HttpClient() as c:
            c.set_cookies("bilibili.com", {"SESSDATA": "real_test", "bili_jct": "real_jct"})
            got = c.get_cookies("bilibili.com")
            assert got.get("SESSDATA") == "real_test"
            assert got.get("bili_jct") == "real_jct"

    async def test_set_cookies_with_full_url(self):
        """带 scheme 的 host 也能正确解析出 netloc 并注入"""
        async with HttpClient() as c:
            c.set_cookies("https://www.bilibili.com/video/xxx",
                          {"SESSDATA": "v2"})
            # 注入到 www.bilibili.com（domain=.www.bilibili.com），
            # 查询 www.bilibili.com 应命中
            got = c.get_cookies("www.bilibili.com")
            assert got.get("SESSDATA") == "v2"

    async def test_get_cookies_empty(self):
        async with HttpClient() as c:
            assert c.get_cookies("nonexistent.com") == {}

    async def test_set_cookies_empty_noop(self):
        async with HttpClient() as c:
            c.set_cookies("bilibili.com", {})
            # 不报错，且 jar 仍空
            assert c.get_cookies("bilibili.com") == {}

    async def test_cookies_shared_to_subdomain(self):
        """注入到 bilibili.com 的 cookie 对 api.bilibili.com 也生效"""
        async with HttpClient() as c:
            c.set_cookies("bilibili.com", {"SESSDATA": "sub_test"})
            # 子域应能命中（domain=.bilibili.com）
            got = c.get_cookies("api.bilibili.com")
            assert got.get("SESSDATA") == "sub_test"
