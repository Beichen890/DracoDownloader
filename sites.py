"""
站点专用探嗅规则

针对 GitHub、Bilibili 等无法用通用 HTML/正则可靠解析的站点，
通过其官方 API 将页面 URL 解析为可下载直链。

设计：
- 每个规则继承 SniffRule，重写 match() + extract_async()
- 优先于通用规则命中（注册时插入到规则列表头部）
- 失败时静默返回空列表，不阻断通用规则兜底
- 站点 API 调用复用 HttpClient（带 UA/cookie/proxy）
"""

import re
import time
import urllib.parse
from hashlib import md5
from functools import reduce
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse, urljoin

from .logger import get_logger
from .sniffer import SniffRule, SniffedResource, ResourceType, classify_url

log = get_logger('sites')


# =========================================================================
# GitHub 规则
# =========================================================================

# GitHub 相关域名（出现这些 host 说明已经是直链，交给通用下载器即可）
_GITHUB_FILE_HOSTS = {
    "raw.githubusercontent.com",
    "raw.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "gist.githubusercontent.com",
}

# 公共加速代理站（前缀拼接式：{proxy}/{original_url}）
# 用于在 GitHub 直链不可达时提供镜像候选
_GITHUB_PROXIES = [
    "https://gh-proxy.com",
    "https://ghfast.top",
    "https://gh.ddlc.top",
]

# 路径模式
_RE_RELEASE_TAG = re.compile(
    r"^/([^/]+)/([^/]+)/releases/tag/([^/]+)$", re.IGNORECASE
)
_RE_REPO_ROOT = re.compile(r"^/([^/]+)/([^/]+)/?$")
_RE_BLOB = re.compile(
    r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", re.IGNORECASE
)
_RE_RELEASES_INDEX = re.compile(r"^/([^/]+)/([^/]+)/releases/?$")


def _is_github_page_url(url: str) -> bool:
    """是否是 GitHub 页面 URL（非直链、可解析的）"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != "github.com":
        return False
    if host in _GITHUB_FILE_HOSTS:
        return False
    path = parsed.path.lower()
    # 已是直链形式
    if "/archive/" in path or "/releases/download/" in path or "/raw/" in path:
        return False
    return bool(_RE_RELEASE_TAG.match(parsed.path)
                or _RE_REPO_ROOT.match(parsed.path)
                or _RE_BLOB.match(parsed.path)
                or _RE_RELEASES_INDEX.match(parsed.path))


def _with_proxy_candidates(direct_url: str) -> List[SniffedResource]:
    """为 GitHub 直链生成加速站候选列表

    返回：[原直链(最高置信度), 代理1, 代理2, ...]
    """
    resources = [SniffedResource(
        url=direct_url,
        type=classify_url(direct_url),
        source="github:direct",
        confidence=0.95,
    )]
    for proxy in _GITHUB_PROXIES:
        resources.append(SniffedResource(
            url=f"{proxy.rstrip('/')}/{direct_url}",
            type=resources[0].type,
            source=f"github:proxy:{urlparse(proxy).hostname}",
            confidence=0.7,
        ))
    return resources


class _GitHubRule(SniffRule):
    """GitHub 站点规则

    将以下页面 URL 解析为直链：
    - release tag 页 → 调 API 获取 release assets 直链
    - 仓库主页 → 生成 archive（master.zip）直链
    - blob 页 → 转 raw.githubusercontent.com 直链
    - releases 索引页 → 取最新 release 的 assets

    所有直链附加公共加速站候选，供下载器在原链不可达时切换。
    """
    name = "github"

    def match(self, url: str, content_type: str, text: str) -> bool:
        return _is_github_page_url(url)

    async def extract_async(self, url: str, text: str,
                            http_client=None) -> List[SniffedResource]:
        results: List[SniffedResource] = []
        parsed = urlparse(url)

        # 1. release tag 页 → API 获取 assets
        m = _RE_RELEASE_TAG.match(parsed.path)
        if m:
            owner, repo, tag = m.group(1), m.group(2), m.group(3)
            results = await self._fetch_release_assets(
                owner, repo, tag, http_client
            )
            if results:
                return results
            # API 失败时回退到页面正则解析（通用规则已处理，这里返回空让其兜底）
            return []

        # 2. releases 索引页 → 取最新 release
        m = _RE_RELEASES_INDEX.match(parsed.path)
        if m:
            owner, repo = m.group(1), m.group(2)
            latest = await self._fetch_latest_release_tag(
                owner, repo, http_client
            )
            if latest:
                return await self._fetch_release_assets(
                    owner, repo, latest, http_client
                )
            return []

        # 3. blob 页 → raw 直链
        m = _RE_BLOB.match(parsed.path)
        if m:
            owner, repo, branch, filepath = (
                m.group(1), m.group(2), m.group(3), m.group(4)
            )
            raw_url = (
                f"https://raw.githubusercontent.com/"
                f"{owner}/{repo}/{branch}/{filepath}"
            )
            return _with_proxy_candidates(raw_url)

        # 4. 仓库主页 → archive 直链
        m = _RE_REPO_ROOT.match(parsed.path)
        if m:
            owner, repo = m.group(1), m.group(2)
            archive_url = (
                f"https://github.com/{owner}/{repo}/"
                f"archive/refs/heads/master.zip"
            )
            resources = _with_proxy_candidates(archive_url)
            for r in resources:
                r.label = f"{owner}/{repo} master.zip"
            return resources

        return []

    async def _fetch_release_assets(self, owner: str, repo: str,
                                    tag: str, http_client
                                    ) -> List[SniffedResource]:
        """调 GitHub API 获取指定 release 的 assets 直链"""
        api_url = (
            f"https://api.github.com/repos/{owner}/{repo}/"
            f"releases/tags/{urllib.parse.quote(tag)}"
        )
        try:
            data = await self._api_get(api_url, http_client)
        except Exception as e:
            log.debug(f"GitHub release API 失败 {owner}/{repo}@{tag}: {e}")
            return []

        assets = data.get("assets") or []
        results: List[SniffedResource] = []
        for asset in assets:
            dl_url = asset.get("browser_download_url")
            if not dl_url:
                continue
            name = asset.get("name", "")
            size = asset.get("size", 0)
            candidates = _with_proxy_candidates(dl_url)
            for r in candidates:
                r.label = name
                r.extra["size"] = size
                r.extra["release_tag"] = tag
            results.extend(candidates)
        return results

    async def _fetch_latest_release_tag(self, owner: str, repo: str,
                                        http_client) -> Optional[str]:
        """获取仓库最新 release 的 tag 名"""
        api_url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        )
        try:
            data = await self._api_get(api_url, http_client)
        except Exception as e:
            log.debug(f"GitHub latest release API 失败 {owner}/{repo}: {e}")
            return None
        return data.get("tag_name")

    async def _api_get(self, url: str, http_client) -> Dict[str, Any]:
        """调 JSON API 并返回解析后的 dict"""
        if http_client is None:
            raise RuntimeError("无 HttpClient，无法调 GitHub API")
        text, ct, _ = await http_client.fetch_text(
            url, headers={"Accept": "application/vnd.github+json"},
            timeout=15.0, clean_headers=True,
        )
        import json
        return json.loads(text)


# =========================================================================
# Bilibili 规则
# =========================================================================

# WBI 签名用混淆表（公开算法，用于对 playurl 请求参数签名）
_WBI_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 视频页路径：/video/BVxxxx 或 /video/avxxxx
_RE_BILI_VIDEO = re.compile(r"/video/(BV[a-zA-Z0-9]+|av\d+)", re.IGNORECASE)

# 默认请求画质（80=1080P，64=720P，32=480P，16=360P）
_DEFAULT_QUALITY = 80
# fnval=16 请求 DASH 格式（音视频分离，可分别下载）
_FNVAL_DASH = 16

# 画质代号 → 标签
_QUALITY_LABELS = {
    127: "8K 超高清", 126: "杜比视界", 125: "HDR 真彩", 120: "4K 超清",
    116: "1080P60", 112: "1080P+ 码率", 80: "1080P 高清",
    74: "720P60", 64: "720P 高清", 32: "480P 清晰", 16: "360P 流畅",
}


class _BilibiliRule(SniffRule):
    """Bilibili 视频规则

    B 站视频页有强反爬（412），HTML 解析不可行。本规则通过官方 API：
    1. view API：BV/av → cid + 标题 + 分P 列表
    2. playurl API（WBI 签名）：cid → DASH 流（音视频分离）
    3. 选最高画质视频流 + 最高音质音频流，返回直链

    B 站直链要求带 Referer，已写入 extra.headers 供下载器注入。
    """
    name = "bilibili"

    def __init__(self):
        self._mixin_key = ""

    def match(self, url: str, content_type: str, text: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if host != "bilibili.com" and not host.endswith(".bilibili.com"):
            return False
        return bool(_RE_BILI_VIDEO.search(urlparse(url).path))

    async def extract_async(self, url: str, text: str,
                            http_client=None) -> List[SniffedResource]:
        if http_client is None:
            raise RuntimeError("无 HttpClient，无法调 Bilibili API")

        parsed = urlparse(url)
        m = _RE_BILI_VIDEO.search(parsed.path)
        if not m:
            return []
        video_id = m.group(1)

        # 解析分P参数 (?p=1 或 ?p=1-3 或 ?p=1,3)
        page_no = self._parse_page_param(parsed.query)

        # 1. 获取 WBI keys（playurl 签名用）
        await self._fetch_wbi_keys(http_client)

        # 2. view API → cid + 标题
        view_data = await self._fetch_view(video_id, http_client)
        if not view_data:
            return []

        title = str(view_data.get("title", "")).strip() or "bilibili_video"
        pages = list(view_data.get("pages") or [])
        if not pages:
            return []

        # 选目标分P
        if page_no is None:
            target_pages = pages  # 全部分P
        else:
            target_pages = [p for p in pages
                            if self._page_number(p) in page_no]
            if not target_pages:
                target_pages = [pages[0]]

        results: List[SniffedResource] = []
        referer = f"https://www.bilibili.com/video/{video_id}"
        download_headers = {"Referer": referer}

        for page in target_pages:
            cid = int(page.get("cid", 0))
            if cid <= 0:
                continue
            page_part = str(page.get("part", "")).strip()
            page_num = self._page_number(page)

            # 3. playurl API（WBI 签名）→ DASH 流
            play_data = await self._fetch_playurl(
                video_id, cid, http_client
            )
            if not play_data:
                continue

            dash = play_data.get("dash") or {}
            video_streams = dash.get("video") or []
            audio_streams = dash.get("audio") or []

            video_url = self._select_video_stream(
                video_streams,
                play_data.get("accept_quality") or [],
            )
            audio_url = self._select_audio_stream(audio_streams)

            if not video_url:
                continue

            label_parts = [f"P{page_num}"]
            if page_part:
                label_parts.append(page_part)
            # 取视频流画质标签
            v_stream = self._find_stream_by_url(video_streams, video_url)
            if v_stream:
                qid = v_stream.get("id")
                qlabel = _QUALITY_LABELS.get(qid, "")
                if qlabel:
                    label_parts.append(qlabel)
            label = " ".join(label_parts)

            resource = SniffedResource(
                url=video_url,
                type=ResourceType.VIDEO,
                source="bilibili:video",
                confidence=0.95,
                label=label,
                extra={
                    "page": page_num,
                    "cid": cid,
                    "title": title,
                    "part": page_part,
                    "headers": download_headers,
                    "video_quality": v_stream.get("id") if v_stream else None,
                },
            )
            if audio_url:
                resource.extra["audio_url"] = audio_url
                a_stream = self._find_stream_by_url(audio_streams, audio_url)
                if a_stream:
                    resource.extra["audio_quality"] = a_stream.get("id")
            results.append(resource)

        return results

    # --- API 调用 ---

    async def _fetch_wbi_keys(self, http_client) -> None:
        """获取 WBI 签名密钥（nav API 返回 img_url + sub_url）"""
        if self._mixin_key:
            return
        try:
            import json
            text, _, _ = await http_client.fetch_text(
                "https://api.bilibili.com/x/web-interface/nav",
                timeout=10.0, clean_headers=True,
            )
            data = json.loads(text).get("data") or {}
            wbi = data.get("wbi_img") or {}
            img_url = str(wbi.get("img_url") or "")
            sub_url = str(wbi.get("sub_url") or "")
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""
            if img_key and sub_key:
                orig = img_key + sub_key
                self._mixin_key = reduce(
                    lambda s, i: s + orig[i], _WBI_ENC_TAB, ""
                )[:32]
        except Exception as e:
            log.debug(f"Bilibili WBI keys 获取失败: {e}")

    async def _fetch_view(self, video_id: str,
                          http_client) -> Optional[Dict[str, Any]]:
        """view API：BV/av → 视频元信息（cid、标题、分P）"""
        try:
            import json
            if video_id.lower().startswith("av"):
                api = (
                    f"https://api.bilibili.com/x/web-interface/view"
                    f"?avid={video_id[2:]}"
                )
            else:
                api = (
                    f"https://api.bilibili.com/x/web-interface/view"
                    f"?bvid={video_id}"
                )
            text, _, _ = await http_client.fetch_text(
                api, timeout=10.0, clean_headers=True,
            )
            payload = json.loads(text)
            if payload.get("code") not in (None, 0):
                log.debug(f"Bilibili view API 返回错误: {payload.get('message')}")
                return None
            return payload.get("data") or {}
        except Exception as e:
            log.debug(f"Bilibili view API 异常: {e}")
            return None

    async def _fetch_playurl(self, video_id: str, cid: int,
                             http_client) -> Optional[Dict[str, Any]]:
        """playurl API（WBI 签名）→ DASH 流信息"""
        try:
            import json
            params = {
                "cid": cid,
                "qn": _DEFAULT_QUALITY,
                "fnval": _FNVAL_DASH,
                "fourk": 1,
            }
            if video_id.lower().startswith("av"):
                params["avid"] = video_id[2:]
            else:
                params["bvid"] = video_id
            params = self._sign_params(params)
            query = urllib.parse.urlencode(params)
            api = (
                f"https://api.bilibili.com/x/player/wbi/playurl?{query}"
            )
            text, _, _ = await http_client.fetch_text(
                api, timeout=10.0, clean_headers=True,
            )
            payload = json.loads(text)
            if payload.get("code") not in (None, 0):
                log.debug(
                    f"Bilibili playurl API 返回错误: {payload.get('message')}"
                )
                return None
            return payload.get("data") or {}
        except Exception as e:
            log.debug(f"Bilibili playurl API 异常: {e}")
            return None

    # --- WBI 签名 ---

    def _sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """对请求参数做 WBI 签名（追加 wts + w_rid）"""
        if not self._mixin_key:
            return params
        params["wts"] = round(time.time())
        params = dict(sorted(params.items()))
        # 过滤特殊字符（B 站签名要求）
        params = {
            k: "".join(c for c in str(v) if c not in "!'()*")
            for k, v in params.items()
        }
        query = urllib.parse.urlencode(params)
        params["w_rid"] = md5(
            (query + self._mixin_key).encode()
        ).hexdigest()
        return params

    # --- 流选择 ---

    def _select_video_stream(self, streams: List[Dict],
                             accept_quality: List[int]) -> str:
        """选最高可用画质的视频流"""
        if not streams:
            return ""
        # 优先匹配默认画质，其次取 accept_quality 中的最高
        target = _DEFAULT_QUALITY
        if accept_quality and target not in accept_quality:
            target = max(accept_quality)
        for s in streams:
            if s.get("id") == target:
                url = self._stream_url(s)
                if url:
                    return url
        # 兜底：取第一个有 URL 的
        for s in streams:
            url = self._stream_url(s)
            if url:
                return url
        return ""

    def _select_audio_stream(self, streams: List[Dict]) -> str:
        """选最高音质的音频流（按 id 降序）"""
        if not streams:
            return ""
        sorted_streams = sorted(
            streams, key=lambda s: s.get("id", 0), reverse=True
        )
        for s in sorted_streams:
            url = self._stream_url(s)
            if url:
                return url
        return ""

    def _stream_url(self, stream: Dict) -> str:
        """从流对象提取直链（baseUrl 优先，其次 backupUrl）"""
        url = stream.get("baseUrl") or ""
        if url:
            return url
        for item in (stream.get("backupUrl") or []):
            if item:
                return item
        return ""

    def _find_stream_by_url(self, streams: List[Dict],
                            url: str) -> Optional[Dict]:
        """根据 URL 反查流对象"""
        for s in streams:
            if self._stream_url(s) == url:
                return s
        return None

    # --- 分P 解析 ---

    def _parse_page_param(self, query: str) -> Optional[set]:
        """解析 ?p= 参数（支持 1, 1-3, 1,3,5）"""
        from urllib.parse import parse_qs
        p = parse_qs(query).get("p", [""])[0].strip()
        if not p:
            return None
        pages = set()
        for part in p.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    start, end = int(start), int(end)
                    if start > end:
                        start, end = end, start
                    pages.update(range(start, end + 1))
                except ValueError:
                    continue
            else:
                try:
                    pages.add(int(part))
                except ValueError:
                    continue
        return pages or None

    def _page_number(self, page: Dict) -> int:
        """分P 对象 → 页码（page 字段，默认 1）"""
        try:
            return int(page.get("page", 1))
        except (TypeError, ValueError):
            return 1


# =========================================================================
# 导出
# =========================================================================

def get_builtin_site_rules() -> List[SniffRule]:
    """返回内置站点专用规则（按优先级，GitHub 在前）"""
    return [_GitHubRule(), _BilibiliRule()]


__all__ = [
    "get_builtin_site_rules",
    "_GitHubRule",
    "_BilibiliRule",
]
