"""
站点登录态模块 - 扫码登录 + Cookie 持久化

目前实现 Bilibili 扫码登录：
1. 调 passport API 生成二维码 → 用户用手机 App 扫码
2. 轮询登录状态 → 成功后从响应提取 cookie
3. Cookie 持久化到本地 JSON 文件，下次启动自动加载
4. 注入到共享 HttpClient 的 cookie jar，后续 API 请求自动携带

设计：
- 登录态与 HttpClient 解耦：BilibiliAuth 负责拿到 cookie，
  通过 HttpClient.set_cookies() 注入；HttpClient 不感知具体站点
- 持久化路径默认 ~/.draco/auth/bilibili.json，可自定义
- 零额外依赖：二维码内容（URL）返回给调用方自行渲染，
  不引入 qrcode 库
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable

from .logger import get_logger
from .http_client import HttpClient

log = get_logger('auth')


# 默认 cookie 持久化路径
def _default_storage_path() -> Path:
    return Path.home() / ".draco" / "auth" / "bilibili.json"


# Bilibili passport API
_QR_GENERATE_API = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
_QR_POLL_API = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
)
_NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# 扫码轮询状态码
_QR_CODE_SUCCESS = 0       # 登录成功
_QR_CODE_WAITING = 86039   # 未扫码
_QR_CODE_SCANNED = 86090   # 已扫码，等待确认
_QR_CODE_EXPIRED = 86038   # 二维码已过期

# 轮询间隔（秒）
_POLL_INTERVAL = 2.0
# 默认二维码有效期（秒）— B 站二维码约 180s
_DEFAULT_QR_TIMEOUT = 180


class BilibiliAuth:
    """Bilibili 扫码登录与 Cookie 持久化

    用法（完整登录流程）::

        auth = BilibiliAuth(http_client)
        result = await auth.login_async(on_qrcode=lambda url: print(url))
        if result["success"]:
            print(f"登录成功: {result['username']}")

    用法（启动时自动加载已保存的 cookie）::

        auth = BilibiliAuth(http_client)
        loaded = auth.load_and_inject()
        if loaded:
            status = await auth.check_login()
            print(status)

    登录成功后，cookie 已注入 HttpClient，后续 Bilibili API 请求
    （view/playurl/nav 等）会自动携带，无需规则层改动。
    """

    def __init__(self, http_client: HttpClient,
                 storage_path: Optional[Path] = None):
        """
        Args:
            http_client: 共享 HttpClient（cookie 注入目标）
            storage_path: cookie 持久化路径，默认 ~/.draco/auth/bilibili.json
        """
        self._http = http_client
        self._storage_path = storage_path or _default_storage_path()

    # ------------------------------------------------------------------
    # 扫码登录
    # ------------------------------------------------------------------

    async def generate_qrcode(self) -> Dict[str, Any]:
        """生成登录二维码

        Returns:
            {"url": str, "qrcode_key": str}
            - url: 二维码内容，用手机 B 站 App 扫码
            - qrcode_key: 轮询登录状态用的 key

        Raises:
            RuntimeError: API 调用失败
        """
        text, _, _ = await self._http.fetch_text(
            _QR_GENERATE_API, timeout=10.0, clean_headers=True,
        )
        payload = json.loads(text)
        if payload.get("code") not in (None, 0):
            raise RuntimeError(
                f"生成二维码失败: {payload.get('message')}"
            )
        data = payload.get("data") or {}
        url = data.get("url", "")
        qrcode_key = data.get("qrcode_key", "")
        if not url or not qrcode_key:
            raise RuntimeError("二维码 API 返回数据不完整")
        log.debug("Bilibili 二维码已生成")
        return {"url": url, "qrcode_key": qrcode_key}

    async def poll_login(self, qrcode_key: str,
                         timeout: float = _DEFAULT_QR_TIMEOUT,
                         on_status: Optional[Callable[[int, str], Awaitable[None]]] = None
                         ) -> Dict[str, Any]:
        """轮询登录状态，直到成功/过期/超时

        Args:
            qrcode_key: generate_qrcode 返回的 key
            timeout: 最长等待时间（秒），默认 180
            on_status: 状态回调（code, message），用于 UI 展示进度

        Returns:
            {"success": bool, "code": int, "message": str, "cookies": dict}
            成功时 cookies 含 SESSDATA / bili_jct / DedeUserID 等
        """
        deadline = time.time() + timeout
        last_code = None

        while time.time() < deadline:
            params = {"qrcode_key": qrcode_key}
            from urllib.parse import urlencode
            api = f"{_QR_POLL_API}?{urlencode(params)}"
            text, _, resp_url = await self._http.fetch_text(
                api, timeout=10.0, clean_headers=True,
            )
            payload = json.loads(text)
            if payload.get("code") not in (None, 0):
                raise RuntimeError(
                    f"轮询接口错误: {payload.get('message')}"
                )
            data = payload.get("data") or {}
            code = int(data.get("code", -1))
            message = str(data.get("message", ""))

            # 状态变化时回调
            if code != last_code:
                last_code = code
                if on_status:
                    try:
                        await on_status(code, message)
                    except Exception:
                        pass

            if code == _QR_CODE_SUCCESS:
                # 登录成功，从 poll 响应的 Set-Cookie 头提取 cookie
                # aiohttp cookie jar 已自动捕获，这里从 jar 读取
                cookies = self._extract_login_cookies(resp_url)
                if not cookies:
                    # 兜底：尝试从 data.url 的 crossDomain 参数提取
                    cookies = self._parse_crossdomain_cookies(
                        str(data.get("url") or "")
                    )
                if not cookies:
                    return {
                        "success": False, "code": code,
                        "message": "登录成功但未捕获到 cookie",
                        "cookies": {},
                    }
                log.info(f"Bilibili 登录成功，捕获 {len(cookies)} 个 cookie")
                return {
                    "success": True, "code": code,
                    "message": message, "cookies": cookies,
                }
            elif code == _QR_CODE_EXPIRED:
                return {
                    "success": False, "code": code,
                    "message": "二维码已过期，请重新生成",
                    "cookies": {},
                }
            # 86039(未扫码) / 86090(已扫码等待确认) → 继续轮询
            await asyncio.sleep(_POLL_INTERVAL)

        return {
            "success": False, "code": -1,
            "message": "登录超时",
            "cookies": {},
        }

    async def login_async(self,
                          on_qrcode: Optional[Callable[[str], Awaitable[None]]] = None,
                          on_status: Optional[Callable[[int, str], Awaitable[None]]] = None,
                          timeout: float = _DEFAULT_QR_TIMEOUT
                          ) -> Dict[str, Any]:
        """完整扫码登录流程

        Args:
            on_qrcode: 二维码生成回调，参数为二维码 URL（需调用方渲染展示）
            on_status: 状态变化回调（code, message）
            timeout: 二维码有效期（秒）

        Returns:
            {"success": bool, "message": str, "cookies": dict,
             "username": str (登录成功时)}
        """
        qr = await self.generate_qrcode()
        if on_qrcode:
            try:
                await on_qrcode(qr["url"])
            except Exception:
                pass

        result = await self.poll_login(
            qr["qrcode_key"], timeout=timeout, on_status=on_status,
        )
        if not result["success"]:
            return result

        cookies = result["cookies"]
        # 注入到 HttpClient
        self._http.set_cookies("bilibili.com", cookies)

        # 持久化
        self.save_cookies(cookies)

        # 查询用户名
        username = ""
        try:
            status = await self.check_login()
            username = status.get("username", "")
        except Exception:
            pass

        return {
            "success": True,
            "message": result["message"],
            "cookies": cookies,
            "username": username,
        }

    # ------------------------------------------------------------------
    # 登录态校验
    # ------------------------------------------------------------------

    async def check_login(self) -> Dict[str, Any]:
        """检查当前 HttpClient 中的 Bilibili cookie 是否有效

        调 nav API，返回用户信息即视为已登录。

        Returns:
            {"is_logged_in": bool, "username": str, "uid": int,
             "vip_status": int, "message": str}
        """
        text, _, _ = await self._http.fetch_text(
            _NAV_API, timeout=10.0, clean_headers=True,
        )
        payload = json.loads(text)
        code = payload.get("code")
        if code not in (None, 0):
            return {
                "is_logged_in": False, "username": "", "uid": 0,
                "vip_status": 0,
                "message": f"nav API: {payload.get('message')}",
            }
        data = payload.get("data") or {}
        is_guest = bool(data.get("isLogin") is False)
        return {
            "is_logged_in": not is_guest,
            "username": str(data.get("uname") or ""),
            "uid": int(data.get("mid") or 0),
            "vip_status": int(data.get("vipStatus") or 0),
            "message": "ok" if not is_guest else "未登录（游客）",
        }

    # ------------------------------------------------------------------
    # Cookie 持久化
    # ------------------------------------------------------------------

    def save_cookies(self, cookies: Dict[str, str]) -> Path:
        """持久化 cookie 到本地 JSON 文件

        Args:
            cookies: cookie dict

        Returns:
            实际写入的文件路径
        """
        path = self._storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cookies": cookies,
            "saved_at": time.time(),
            "host": "bilibili.com",
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        log.debug(f"Bilibili cookie 已保存到 {path}")
        return path

    def load_cookies(self) -> Dict[str, str]:
        """从本地 JSON 文件加载 cookie

        Returns:
            cookie dict，文件不存在或损坏时返回空 dict
        """
        path = self._storage_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cookies = data.get("cookies") or {}
            if isinstance(cookies, dict):
                return cookies
        except (json.JSONDecodeError, OSError) as e:
            log.debug(f"加载 Bilibili cookie 失败: {e}")
        return {}

    def load_and_inject(self) -> bool:
        """加载已保存的 cookie 并注入 HttpClient

        启动时调用，恢复登录态。

        Returns:
            True 表示成功加载并注入了 cookie
        """
        cookies = self.load_cookies()
        if not cookies:
            return False
        # 基本有效性检查：SESSDATA 是 B 站登录态核心 cookie
        if "SESSDATA" not in cookies:
            log.debug("已保存的 Bilibili cookie 缺少 SESSDATA，跳过注入")
            return False
        self._http.set_cookies("bilibili.com", cookies)
        log.info(f"已加载 Bilibili 登录态（{len(cookies)} 个 cookie）")
        return True

    def logout(self):
        """清除登录态（删除持久化文件 + 清空 jar 中 bilibili cookie）"""
        try:
            if self._storage_path.exists():
                self._storage_path.unlink()
                log.debug(f"已删除 {self._storage_path}")
        except OSError as e:
            log.warning(f"删除 cookie 文件失败: {e}")
        # 清空 jar 中的 bilibili cookie：用过期值覆盖
        # aiohttp CookieJar 没有直接的 clear(host) 接口，
        # 这里用 max-age=0 的方式让 jar 丢弃
        expired = {k: "" for k in self._http.get_cookies("bilibili.com")}
        if expired:
            from yarl import URL
            if self._http._cookie_jar is not None:
                # 用空值覆盖并设置过期
                for name in expired:
                    self._http._cookie_jar.update_cookies(
                        {name: ""}, URL("https://www.bilibili.com/")
                    )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _extract_login_cookies(self, resp_url: str) -> Dict[str, str]:
        """从 poll 成功响应后，读取 HttpClient jar 中的 bilibili cookie

        poll 成功时 B 站通过 Set-Cookie 下发 SESSDATA/bili_jct 等，
        aiohttp cookie jar 会自动捕获。
        """
        return self._http.get_cookies("bilibili.com")

    def _parse_crossdomain_cookies(self, crossdomain_url: str) -> Dict[str, str]:
        """从 poll 返回的 crossDomain URL 中解析 cookie（兜底）

        B 站有时把 SESSDATA 等放在 data.url 的查询参数里：
        https://passport.biligame.com/crossDomain?DedeUserID=xxx&SESSDATA=yyy&...
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(crossdomain_url)
        params = parse_qs(parsed.query)
        cookies = {}
        for key in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
                    "sid", "gourl"):
            if key in params and params[key]:
                cookies[key] = params[key][0]
        return cookies


__all__ = ["BilibiliAuth"]
