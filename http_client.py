"""
反反爬 HTTP 客户端 - 统一的会话管理 + UA 轮换 + Cookie 持久 + Proxy 激活

解决 Draco 当前反爬能力缺失的问题：
1. UA 默认化：所有请求自动带真实浏览器 UA（不再是 aiohttp 默认的 Python/xxx）
2. Proxy 激活：DownloadHandle.proxy 之前是死字段，现在真正传给 aiohttp
3. Cookie/Session 复用：跨请求保持 cookie（应对登录态/反爬 cookie 校验）
4. 智能重试：403/429 按 Retry-After 退避，区分反爬错误与网络错误
5. Referer 自动注入：下载分片时自动带页面 URL 作为 Referer（防盗链）

设计：HttpClient 作为协议层的共享基础设施，由 HTTPDriver/M3U8Driver/Sniffer 共用。
不引入 curl_cffi/niquests 等重依赖（保持核心零额外依赖），
但预留 backend 接口，未来可平滑切换到 TLS 指纹后端。
"""

import asyncio
import random
import time
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

from .logger import get_logger
from .errors import DracoError, make_error, ERR_HTTP_STATUS

log = get_logger('http_client')


# === 浏览器 UA 池 ===
# 真实主流浏览器 UA，按桌面/移动分组，请求时随机轮换
# （非完整列表，足够应对简单 UA 校验类反爬）

_DESKTOP_UAS = [
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Edge (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Firefox (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Safari (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

_MOBILE_UAS = [
    # Chrome (Android)
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    # Safari (iPhone)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
    "Mobile/15E148 Safari/604.1",
]


def random_user_agent(mobile: bool = False) -> str:
    """随机返回一个真实浏览器 UA"""
    pool = _MOBILE_UAS if mobile else _DESKTOP_UAS
    return random.choice(pool)


# === 默认请求头 ===
# 模拟真实浏览器的基础头集合，除 UA 外还包括 Accept/Accept-Language 等

_DEFAULT_BROWSER_HEADERS: Dict[str, str] = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
              'image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}


# === 反爬错误分类 ===

# 需要特殊处理的 HTTP 状态码（反爬相关）
# 412 = Precondition Failed（B 站/部分站点用此码反爬，需补 Cookie/UA）
ANTI_BOT_STATUS_CODES = {401, 403, 412, 429}
# 可重试状态码（带退避）：429 限流、412 反爬、503 服务暂不可用
RETRYABLE_STATUS_CODES = {412, 429, 503}


@dataclass
class AntiBotError(Exception):
    """反爬错误

    与普通 HTTP 状态错误区分，供 Agent 程序化处理：
    - 403 → 可能需要换 UA / 加 cookie / 换代理
    - 412 → 反爬前置校验失败（B 站等），需补 Cookie/Referer/UA
    - 429 → 需要按 Retry-After 退避
    - 401 → 需要登录态
    """
    status: int
    url: str
    retry_after: Optional[float] = None  # 秒（来自 Retry-After 头）
    message: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        super().__init__(self.message or f"HTTP {self.status} (anti-bot) {self.url}")
        if not self.message:
            self.message = f"HTTP {self.status} (anti-bot) {self.url}"

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429

    @property
    def is_forbidden(self) -> bool:
        return self.status == 403

    @property
    def needs_auth(self) -> bool:
        return self.status == 401

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'url': self.url,
            'retry_after': self.retry_after,
            'message': self.message,
            'context': self.context,
        }


# === HttpClient ===

class HttpClient:
    """反反爬 HTTP 客户端

    特性：
    1. 持久 aiohttp.ClientSession（cookie jar 跨请求复用）
    2. 自动 UA 轮换（每次请求随机 UA，或固定指定）
    3. Proxy 真正生效（传给 aiohttp session）
    4. 智能重试：429/503 按 Retry-After 退避；网络错误指数退避
    5. 反爬错误分类：抛 AntiBotError 而非 RuntimeError，Agent 可分支处理
    6. Referer 自动注入：防盗链站点需要

    用法：
        client = HttpClient(proxy='socks5://127.0.0.1:1080',
                            user_agent='custom', rotate_ua=True)
        await client.start()
        text, ctype, final_url = await client.fetch_text(url)
        await client.close()

    或作为 async context manager：
        async with HttpClient() as client:
            ...
    """

    def __init__(self,
                 proxy: Optional[str] = None,
                 user_agent: Optional[str] = None,
                 rotate_ua: bool = True,
                 mobile_ua: bool = False,
                 default_headers: Optional[Dict[str, str]] = None,
                 timeout: float = 30.0,
                 max_retries: int = 3,
                 cookie_jar: Optional[aiohttp.CookieJar] = None,
                 trust_env: bool = True):
        """
        Args:
            proxy: 代理 URL（http://或 socks5://，socks5 需 aiohttp-socks）
            user_agent: 固定 UA（None 时按 rotate_ua 策略选）
            rotate_ua: 是否每次请求轮换 UA
            mobile_ua: 使用移动端 UA 池
            default_headers: 默认请求头（合并到每个请求）
            timeout: 默认超时
            max_retries: 反爬/网络错误最大重试次数
            cookie_jar: 自定义 cookie jar（None=内存 jar）
            trust_env: 是否读取环境变量代理（HTTP_PROXY/HTTPS_PROXY）
        """
        self._proxy = proxy
        self._fixed_ua = user_agent
        self._rotate_ua = rotate_ua and user_agent is None
        self._mobile = mobile_ua
        self._default_headers = {**_DEFAULT_BROWSER_HEADERS, **(default_headers or {})}
        # 运行时检测 brotli 支持：aiohttp 不内置 br 解压，需 brotli/brotlicffi 包。
        # 未安装时移除 Accept-Encoding 中的 br，避免收到无法解压的响应导致 HTML 损坏。
        try:
            import brotli  # noqa: F401
        except ImportError:
            try:
                import brotlicffi  # noqa: F401
            except ImportError:
                ae = self._default_headers.get('Accept-Encoding', '')
                self._default_headers['Accept-Encoding'] = \
                    ', '.join(p.strip() for p in ae.split(',') if 'br' not in p)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        # cookie jar 延迟创建：aiohttp>=3.10 的 CookieJar.__init__ 需要运行中的事件循环，
        # 在 __init__ 中创建会导致无事件循环时构造失败。延迟到 start() 中创建。
        self._cookie_jar = cookie_jar
        self._trust_env = trust_env

        self._session: Optional[aiohttp.ClientSession] = None
        # 记录已访问的页面 URL，后续分片请求自动带 Referer
        self._page_urls: Dict[str, str] = {}  # host → 最后访问的页面 URL

    async def start(self):
        """启动客户端（创建持久 session）"""
        if self._session and not self._session.closed:
            return
        # 延迟创建 cookie jar（需要运行中的事件循环）
        if self._cookie_jar is None:
            self._cookie_jar = aiohttp.CookieJar(unsafe=True)
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            cookie_jar=self._cookie_jar,
            trust_env=self._trust_env,
            headers=self._build_headers(),
        )
        log.debug(f"HttpClient 启动 (proxy={self._proxy}, "
                  f"rotate_ua={self._rotate_ua})")

    async def close(self):
        """关闭客户端"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HttpClient 未启动，先调用 start() 或用 async with")
        return self._session

    def _build_headers(self, extra: Optional[Dict[str, str]] = None,
                       clean: bool = False) -> Dict[str, str]:
        """构建请求头（默认头 + UA + 用户额外头）

        Args:
            extra: 用户额外请求头（覆盖默认头）
            clean: True 时只返回 UA + extra，不带浏览器伪装头
                   （用于调 REST API，避免 Sec-Fetch-* 等头触发 403）
        """
        if clean:
            headers = {}
            if self._fixed_ua:
                headers['User-Agent'] = self._fixed_ua
            elif self._rotate_ua:
                headers['User-Agent'] = random_user_agent(self._mobile)
            if extra:
                headers.update(extra)
            return headers

        headers = dict(self._default_headers)
        if self._fixed_ua:
            headers['User-Agent'] = self._fixed_ua
        elif self._rotate_ua:
            headers['User-Agent'] = random_user_agent(self._mobile)
        if extra:
            headers.update(extra)
        return headers

    def _get_referer(self, url: str) -> Optional[str]:
        """获取该 URL 对应 host 的 Referer（页面 URL）"""
        try:
            host = urlparse(url).netloc
            return self._page_urls.get(host)
        except Exception:
            return None

    def remember_page(self, page_url: str):
        """记录页面 URL，后续对该 host 的分片请求自动带 Referer"""
        try:
            host = urlparse(page_url).netloc
            self._page_urls[host] = page_url
        except Exception:
            pass

    def set_cookies(self, host: str, cookies: Dict[str, str]):
        """向 cookie jar 注入 cookie（用于登录态注入）

        注入后，对该 host 及其子域的后续请求（含 fetch_text/fetch_bytes）
        会自动带这些 cookie，与 clean_headers 无关（cookie 由 jar 层管理）。

        cookie 的 domain 设为 .{host}，使子域也生效（如注入到 bilibili.com
        后，api.bilibili.com / passport.bilibili.com 也能命中）。

        Args:
            host: 目标 host（如 "bilibili.com"）；可带 scheme，仅取 netloc
            cookies: cookie dict，如 {"SESSDATA": "xxx", "bili_jct": "yyy"}
        """
        from http.cookies import SimpleCookie
        from yarl import URL
        if not cookies:
            return
        # 取 netloc
        if "://" in host:
            host = urlparse(host).netloc or host
        host = host.lower()
        url = URL(f"https://{host}/")
        if self._cookie_jar is None:
            self._cookie_jar = aiohttp.CookieJar(unsafe=True)
        # 用 SimpleCookie 构造，显式设置 domain=.host，
        # 让主域和所有子域（api./www./passport. 等）都命中
        morsels = SimpleCookie()
        for name, value in cookies.items():
            morsels[name] = value
            morsels[name]["domain"] = f".{host}" if "." in host else host
        self._cookie_jar.update_cookies(morsels, url)
        log.debug(f"HttpClient 注入 {len(cookies)} 个 cookie 到 {host}")

    def get_cookies(self, host: str) -> Dict[str, str]:
        """读取当前 jar 中对指定 host 生效的 cookie

        Returns:
            cookie dict（name→value），无则空 dict
        """
        from yarl import URL
        if "://" in host:
            host = urlparse(host).netloc or host
        host = host.lower()
        if self._cookie_jar is None:
            return {}
        url = URL(f"https://{host}/")
        result: Dict[str, str] = {}
        try:
            for cookie in self._cookie_jar.filter_cookies(url).values():
                result[cookie.key] = cookie.value
        except Exception:
            return {}
        return result

    async def request(self, method: str, url: str,
                      headers: Optional[Dict[str, str]] = None,
                      params: Optional[Dict] = None,
                      data: Any = None,
                      json_data: Any = None,
                      allow_redirects: bool = True,
                      timeout: Optional[float] = None,
                      max_retries: Optional[int] = None,
                      clean_headers: bool = False,
                      **kwargs) -> aiohttp.ClientResponse:
        """发起请求（带智能重试 + 反爬错误分类）

        Args:
            clean_headers: True 时不带浏览器伪装头（用于调 REST API）

        Returns:
            aiohttp.ClientResponse（需调用方管理上下文，或用 fetch_text/fetch_bytes）

        Raises:
            AntiBotError: 反爬触发（403/429/401）
            aiohttp.ClientError: 网络错误（重试耗尽后抛出）
        """
        retries = max_retries if max_retries is not None else self._max_retries
        last_exc: Optional[Exception] = None

        for attempt in range(retries + 1):
            # 每次重试可能换 UA（轮换）
            req_headers = self._build_headers(headers, clean=clean_headers)
            # 自动带 Referer（防盗链）— clean 模式下不自动加
            if not clean_headers:
                referer = self._get_referer(url)
                if referer and 'Referer' not in req_headers:
                    req_headers['Referer'] = referer

            req_timeout = aiohttp.ClientTimeout(
                total=timeout or self._timeout.total
            ) if timeout else self._timeout

            try:
                resp = await self.session.request(
                    method, url,
                    headers=req_headers,
                    params=params,
                    data=data,
                    json=json_data,
                    allow_redirects=allow_redirects,
                    timeout=req_timeout,
                    proxy=self._proxy,
                    **kwargs,
                )

                # 检查反爬状态码
                if resp.status in ANTI_BOT_STATUS_CODES:
                    retry_after = self._parse_retry_after(resp)
                    if (resp.status in RETRYABLE_STATUS_CODES
                            and attempt < retries):
                        # 429/503：按 Retry-After 退避后重试
                        wait = retry_after if retry_after else (2 ** attempt)
                        log.warning(
                            f"HTTP {resp.status} {url}，"
                            f"{wait:.1f}s 后重试 ({attempt+1}/{retries})"
                        )
                        resp.release()
                        await asyncio.sleep(wait)
                        continue
                    # 不可重试或重试耗尽 → 抛 AntiBotError
                    resp.release()
                    raise AntiBotError(
                        status=resp.status, url=url,
                        retry_after=retry_after,
                        context={'attempts': attempt + 1}
                    )

                if resp.status >= 400:
                    # 其他 4xx/5xx：不视为反爬，按普通 HTTP 错误抛
                    resp.release()
                    raise make_error(
                        ERR_HTTP_STATUS, status=resp.status, url=url
                    )

                return resp

            except AntiBotError:
                raise
            except DracoError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                last_exc = e
                if attempt < retries:
                    wait = 2 ** attempt  # 指数退避：1, 2, 4...
                    log.debug(f"网络错误 {url}: {e}，{wait}s 后重试 "
                              f"({attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                raise

        # 不应到达
        raise last_exc or RuntimeError(f"请求失败 {url}")

    async def fetch_text(self, url: str,
                         headers: Optional[Dict[str, str]] = None,
                         timeout: Optional[float] = None,
                         max_size: int = 5 * 1024 * 1024,
                         clean_headers: bool = False) -> Tuple[str, str, str]:
        """拉取文本内容

        Args:
            clean_headers: True 时不带浏览器伪装头（用于调 REST API）

        Returns:
            (text, content_type, final_url)
        """
        async with await self.request('GET', url, headers=headers,
                                      timeout=timeout,
                                      clean_headers=clean_headers) as resp:
            content_type = resp.headers.get('content-type', '')
            raw = await resp.content.read(max_size)
            charset = resp.charset or 'utf-8'
            try:
                text = raw.decode(charset, errors='replace')
            except (LookupError, UnicodeDecodeError):
                text = raw.decode('utf-8', errors='replace')
            return text, content_type, str(resp.url)

    async def fetch_bytes(self, url: str,
                          headers: Optional[Dict[str, str]] = None,
                          timeout: Optional[float] = None,
                          clean_headers: bool = False) -> Tuple[bytes, str, str]:
        """拉取二进制内容

        Args:
            clean_headers: True 时不带浏览器伪装头（用于调 REST API）

        Returns:
            (data, content_type, final_url)
        """
        async with await self.request('GET', url, headers=headers,
                                      timeout=timeout,
                                      clean_headers=clean_headers) as resp:
            content_type = resp.headers.get('content-type', '')
            data = await resp.read()
            return data, content_type, str(resp.url)

    async def head(self, url: str,
                   headers: Optional[Dict[str, str]] = None,
                   timeout: Optional[float] = None) -> Dict[str, str]:
        """HEAD 请求，返回响应头字典"""
        async with await self.request('HEAD', url, headers=headers,
                                      timeout=timeout) as resp:
            return dict(resp.headers)

    def _parse_retry_after(self, resp: aiohttp.ClientResponse) -> Optional[float]:
        """解析 Retry-After 头（秒）

        支持两种格式：
        - 数字：表示秒数
        - HTTP 日期：表示绝对时间，转换为剩余秒数
        """
        val = resp.headers.get('Retry-After')
        if not val:
            return None
        # 1. 纯数字 → 秒
        try:
            return float(val)
        except ValueError:
            pass
        # 2. HTTP 日期格式
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            dt = parsedate_to_datetime(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            remaining = (dt - now).total_seconds()
            return max(0.0, remaining)
        except (TypeError, ValueError):
            return None

    @property
    def cookies(self) -> Dict[str, str]:
        """当前 cookie jar 中的所有 cookie（用于调试）"""
        result = {}
        if self._cookie_jar is None:
            return result
        for cookie in self._cookie_jar:
            result[cookie.key] = cookie.value
        return result


# === 模块级便捷函数 ===

def build_request_headers(extra: Optional[Dict[str, str]] = None,
                          user_agent: Optional[str] = None,
                          referer: Optional[str] = None) -> Dict[str, str]:
    """构建请求头（无 client 时用此函数）

    供无法使用持久 session 的场景（如 BT peer 连接前的 tracker 请求）使用。
    """
    headers = dict(_DEFAULT_BROWSER_HEADERS)
    headers['User-Agent'] = user_agent or random_user_agent()
    if referer:
        headers['Referer'] = referer
    if extra:
        headers.update(extra)
    return headers


__all__ = [
    'HttpClient',
    'AntiBotError',
    'ANTI_BOT_STATUS_CODES',
    'RETRYABLE_STATUS_CODES',
    'random_user_agent',
    'build_request_headers',
]
