"""
资源探嗅模块 - 将任意 URL 解析为可下载的直链

当用户给出的是网页 URL（而非直链）时，本模块负责：
1. 拉取页面内容（HTML / JSON / 纯文本）
2. 用正则 + 启发式从中提取媒体资源直链（m3u8/mp4/mp3/zip/...）
3. 返回结构化的探嗅结果（含直链、类型、来源标签、置信度）

设计原则：
- 零外部依赖：仅用 stdlib re + aiohttp（已在依赖中），不引入 bs4/lxml
  （HTML 解析用 stdlib html.parser，足够提取 <video>/<source>/<a> 等）
- 可插拔：Sniffer 由多个 SniffRule 组成，每个规则负责一类站点/模式，
  第三方可注册自定义规则
- 幂等可缓存：相同 URL 探嗅结果可缓存，避免重复请求
- 与反爬模块协作：探嗅请求经 HttpClient 发送，自动带 UA/cookie/proxy

典型流程：
    sniffer = ResourceSniffer()
    result = await sniffer.sniff(page_url)
    if result.direct_urls:
        # 用第一个直链走正常下载
        await downloader.download_async(result.direct_urls[0].url, ...)
    else:
        # 真无可下载资源
"""

import asyncio
import re
import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from enum import Enum

from .logger import get_logger

log = get_logger('sniffer')


# === 媒体类型识别 ===

class ResourceType(Enum):
    """探嗅到的资源类型"""
    M3U8 = "m3u8"           # HLS 播放列表
    VIDEO = "video"          # mp4/webm/mkv/avi 等视频
    AUDIO = "audio"          # mp3/aac/flac/ogg 等音频
    ARCHIVE = "archive"      # zip/tar/gz/rar/7z 等压缩包
    IMAGE = "image"          # png/jpg/gif/webp
    DOCUMENT = "document"    # pdf/doc/xlsx
    BINARY = "binary"        # 其他二进制
    TORRENT = "torrent"      # .torrent
    UNKNOWN = "unknown"


# 直链文件后缀 → 资源类型映射（用于快速识别直链）
# 覆盖主流分发格式：媒体/压缩包/安装包/镜像/包管理产物
_DIRECT_LINK_EXTENSIONS: Dict[str, ResourceType] = {
    # HLS
    '.m3u8': ResourceType.M3U8,
    '.m3u': ResourceType.M3U8,
    # 视频
    '.mp4': ResourceType.VIDEO,
    '.webm': ResourceType.VIDEO,
    '.mkv': ResourceType.VIDEO,
    '.avi': ResourceType.VIDEO,
    '.mov': ResourceType.VIDEO,
    '.flv': ResourceType.VIDEO,
    '.wmv': ResourceType.VIDEO,
    '.m4v': ResourceType.VIDEO,
    '.ts': ResourceType.VIDEO,
    '.mpg': ResourceType.VIDEO,
    '.mpeg': ResourceType.VIDEO,
    '.3gp': ResourceType.VIDEO,
    # 音频
    '.mp3': ResourceType.AUDIO,
    '.aac': ResourceType.AUDIO,
    '.flac': ResourceType.AUDIO,
    '.ogg': ResourceType.AUDIO,
    '.wav': ResourceType.AUDIO,
    '.m4a': ResourceType.AUDIO,
    '.opus': ResourceType.AUDIO,
    '.wma': ResourceType.AUDIO,
    # 压缩包
    '.zip': ResourceType.ARCHIVE,
    '.tar': ResourceType.ARCHIVE,
    '.gz': ResourceType.ARCHIVE,
    '.tgz': ResourceType.ARCHIVE,
    '.bz2': ResourceType.ARCHIVE,
    '.tbz2': ResourceType.ARCHIVE,
    '.xz': ResourceType.ARCHIVE,
    '.txz': ResourceType.ARCHIVE,
    '.zst': ResourceType.ARCHIVE,
    '.rar': ResourceType.ARCHIVE,
    '.7z': ResourceType.ARCHIVE,
    '.lz': ResourceType.ARCHIVE,
    '.lzma': ResourceType.ARCHIVE,
    # 安装包 / 包管理产物
    '.whl': ResourceType.ARCHIVE,    # Python wheel
    '.egg': ResourceType.ARCHIVE,    # Python egg
    '.deb': ResourceType.ARCHIVE,    # Debian 包
    '.rpm': ResourceType.ARCHIVE,    # RPM 包
    '.apk': ResourceType.ARCHIVE,    # Android 包
    '.jar': ResourceType.ARCHIVE,    # Java jar
    '.war': ResourceType.ARCHIVE,    # Java war
    '.exe': ResourceType.BINARY,     # Windows 可执行
    '.msi': ResourceType.BINARY,     # Windows 安装包
    '.dmg': ResourceType.BINARY,     # macOS 安装包
    '.pkg': ResourceType.BINARY,     # macOS pkg
    '.appimage': ResourceType.BINARY, # Linux AppImage
    # 镜像
    '.iso': ResourceType.BINARY,
    '.img': ResourceType.BINARY,
    # 图片
    '.png': ResourceType.IMAGE,
    '.jpg': ResourceType.IMAGE,
    '.jpeg': ResourceType.IMAGE,
    '.gif': ResourceType.IMAGE,
    '.webp': ResourceType.IMAGE,
    '.bmp': ResourceType.IMAGE,
    '.svg': ResourceType.IMAGE,
    '.ico': ResourceType.IMAGE,
    '.tiff': ResourceType.IMAGE,
    '.tif': ResourceType.IMAGE,
    # 文档
    '.pdf': ResourceType.DOCUMENT,
    '.doc': ResourceType.DOCUMENT,
    '.docx': ResourceType.DOCUMENT,
    '.xls': ResourceType.DOCUMENT,
    '.xlsx': ResourceType.DOCUMENT,
    '.ppt': ResourceType.DOCUMENT,
    '.pptx': ResourceType.DOCUMENT,
    '.epub': ResourceType.DOCUMENT,
    '.txt': ResourceType.DOCUMENT,
    '.csv': ResourceType.DOCUMENT,
    # BT
    '.torrent': ResourceType.TORRENT,
}


def classify_url(url: str) -> ResourceType:
    """根据 URL 后缀快速判断资源类型（直链识别）"""
    path = urlparse(url).path.lower()
    for ext, rtype in _DIRECT_LINK_EXTENSIONS.items():
        if path.endswith(ext):
            return rtype
    # 磁力链接
    if url.startswith('magnet:'):
        return ResourceType.TORRENT
    return ResourceType.UNKNOWN


def is_direct_link(url: str) -> bool:
    """判断 URL 是否是可直接下载的资源（非页面）"""
    return classify_url(url) != ResourceType.UNKNOWN


# === 探嗅结果数据结构 ===

@dataclass
class SniffedResource:
    """探嗅到的单个资源"""
    url: str                            # 直链 URL（已 urljoin 为绝对路径）
    type: ResourceType                  # 资源类型
    label: str = ""                     # 人类可读标签（如 "1080p"、"第3集"）
    source: str = ""                    # 来源（"video_tag" / "source_tag" / "regex" / "json_ld"）
    confidence: float = 1.0             # 置信度 0-1（标签属性 > 正则匹配）
    extra: Dict[str, Any] = field(default_factory=dict)  # 额外信息（如 resolution）


@dataclass
class SniffResult:
    """探嗅结果"""
    original_url: str                           # 原始页面 URL
    direct_urls: List[SniffedResource] = field(default_factory=list)  # 解析出的直链
    page_title: str = ""                        # 页面标题（便于 Agent 展示）
    is_direct: bool = False                     # 原始 URL 本身就是直链
    sniffed: bool = False                       # 是否执行了页面解析
    error: Optional[str] = None                 # 探嗅失败原因

    @property
    def best(self) -> Optional[SniffedResource]:
        """置信度最高的资源"""
        if not self.direct_urls:
            return None
        # 优先 M3U8（流媒体）> 视频 > 其他；同类型按置信度降序
        type_priority = {
            ResourceType.M3U8: 0,
            ResourceType.VIDEO: 1,
            ResourceType.AUDIO: 2,
            ResourceType.ARCHIVE: 3,
            ResourceType.DOCUMENT: 4,
            ResourceType.IMAGE: 5,
            ResourceType.TORRENT: 6,
            ResourceType.BINARY: 7,
            ResourceType.UNKNOWN: 8,
        }
        return max(self.direct_urls,
                   key=lambda r: (-type_priority.get(r.type, 9), r.confidence))


# === HTML 解析器：提取 <video>/<source>/<a>/<meta> 中的媒体 URL ===

class _MediaExtractor(HTMLParser):
    """从 HTML 中提取媒体资源 URL

    提取目标：
    - <video src="..."> 和 <video><source src="..."></video>
    - <audio src="..."> 和 <audio><source src="..."></audio>
    - <a href="..."> 中后缀为媒体文件的链接
    - <meta property="og:video" content="...">  Open Graph
    - <meta property="og:image" content="...">
    - <iframe src="..."> 嵌入的播放器（提取其 src 中的 m3u8）
    - JSON-LD <script type="application/ld+json"> 中的 contentUrl
    """

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.results: List[SniffedResource] = []
        self.title = ""
        self._in_title = False
        self._in_ld_json = False
        self._ld_json_buffer = ""
        self._current_video_attrs: Dict[str, str] = {}
        self._in_video = False
        self._in_audio = False

    def _abs(self, url: str) -> str:
        if not url:
            return url
        return urljoin(self.base_url, url)

    def _add(self, url: str, rtype: ResourceType, source: str,
             label: str = "", confidence: float = 1.0,
             extra: Optional[Dict] = None):
        if not url:
            return
        url = self._abs(url)
        # 去重
        for existing in self.results:
            if existing.url == url:
                # 已有，保留更高置信度的
                if confidence > existing.confidence:
                    existing.confidence = confidence
                return
        self.results.append(SniffedResource(
            url=url, type=rtype, source=source,
            label=label, confidence=confidence,
            extra=extra or {}
        ))

    def handle_starttag(self, tag: str, attrs):
        a = dict(attrs)

        if tag == 'title':
            self._in_title = True
            return

        if tag == 'video':
            self._in_video = True
            self._current_video_attrs = a
            src = a.get('src', '')
            if src:
                self._add(src, ResourceType.VIDEO, 'video_tag',
                          confidence=0.95)
            poster = a.get('poster', '')
            if poster:
                self._add(poster, ResourceType.IMAGE, 'video_poster',
                          confidence=0.3)

        elif tag == 'audio':
            self._in_audio = True
            src = a.get('src', '')
            if src:
                self._add(src, ResourceType.AUDIO, 'audio_tag',
                          confidence=0.95)

        elif tag == 'source':
            src = a.get('src', '')
            if src:
                # 父标签决定类型
                if self._in_video:
                    rtype = classify_url(src)
                    if rtype == ResourceType.UNKNOWN:
                        rtype = ResourceType.VIDEO
                elif self._in_audio:
                    rtype = classify_url(src)
                    if rtype == ResourceType.UNKNOWN:
                        rtype = ResourceType.AUDIO
                else:
                    rtype = classify_url(src)
                label_parts = []
                if a.get('res'):
                    label_parts.append(f"{a['res']}p")
                if a.get('label'):
                    label_parts.append(a['label'])
                if a.get('type'):
                    label_parts.append(a['type'])
                self._add(src, rtype, 'source_tag',
                          label=' '.join(label_parts),
                          confidence=0.9,
                          extra={'resolution': a.get('res'),
                                 'mime': a.get('type')})

        elif tag == 'a':
            href = a.get('href', '')
            if href:
                rtype = classify_url(href)
                if rtype != ResourceType.UNKNOWN:
                    label = a.get('download', '') or ''
                    self._add(href, rtype, 'a_tag',
                              label=label, confidence=0.8)

        elif tag == 'meta':
            prop = a.get('property', '') or a.get('name', '')
            content = a.get('content', '')
            if not prop or not content:
                return
            if prop in ('og:video', 'og:video:url', 'og:video:secure_url'):
                self._add(content, ResourceType.VIDEO, 'og_video',
                          confidence=0.85)
            elif prop in ('og:audio', 'og:audio:url'):
                self._add(content, ResourceType.AUDIO, 'og_audio',
                          confidence=0.85)
            elif prop == 'og:image':
                self._add(content, ResourceType.IMAGE, 'og_image',
                          confidence=0.4)

        elif tag == 'iframe':
            src = a.get('src', '')
            if src:
                # iframe 通常是播放器，URL 内可能含 m3u8 参数
                self._add(src, ResourceType.UNKNOWN, 'iframe',
                          confidence=0.3,
                          extra={'needs_secondary_sniff': True})

        elif tag == 'script':
            if a.get('type') == 'application/ld+json':
                self._in_ld_json = True
                self._ld_json_buffer = ""

    def handle_endtag(self, tag: str):
        if tag == 'title':
            self._in_title = False
        elif tag == 'video':
            self._in_video = False
            self._current_video_attrs = {}
        elif tag == 'audio':
            self._in_audio = False
        elif tag == 'script' and self._in_ld_json:
            self._in_ld_json = False
            self._parse_ld_json(self._ld_json_buffer)
            self._ld_json_buffer = ""

    def handle_data(self, data: str):
        if self._in_title:
            self.title += data
        if self._in_ld_json:
            self._ld_json_buffer += data

    def _parse_ld_json(self, text: str):
        """解析 JSON-LD，提取 contentUrl/embedUrl"""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        # JSON-LD 可能是单个对象或列表
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            # 常见字段：contentUrl, embedUrl, url
            for field_name, rtype, conf in [
                ('contentUrl', None, 0.9),
                ('embedUrl', None, 0.6),
                ('url', None, 0.4),
            ]:
                val = item.get(field_name)
                if isinstance(val, str) and val:
                    inferred = rtype or classify_url(val)
                    self._add(val, inferred or ResourceType.UNKNOWN,
                              f'json_ld:{field_name}', confidence=conf)


# === 正则规则：从 JS 变量 / 文本中提取 URL ===

# 从 _DIRECT_LINK_EXTENSIONS 动态构建后缀 alternation（长后缀优先，避免 .tar.gz 被 .gz 提前截断）
# 注意：去掉前导点，正则中统一用 \. 前缀
_EXT_ALTS = '|'.join(re.escape(ext.lstrip('.')) for ext in sorted(
    _DIRECT_LINK_EXTENSIONS.keys(), key=len, reverse=True
))

# 匹配引号包裹的 URL（含路径），用于从 JS 变量、data-* 属性、纯文本中提取
# 支持三种形式：绝对 URL(http://或//)、相对路径(/开头)，以已知后缀结尾
_QUOTED_URL_RE = re.compile(
    r'''["'](((?:https?:)?//[^\s"'<>\)]+|/[^\s"'<>\)]+)\.(?:''' + _EXT_ALTS + r''')(?:\?[^\s"'<>\)]*)?)["']''',
    re.IGNORECASE
)

# 匹配 data-src / data-url 等 data 属性中的媒体 URL
# 支持绝对 URL 和相对路径
_DATA_ATTR_RE = re.compile(
    r'''data-(?:src|url|video|audio|source)=["']((?:https?:)?//[^\s"'<>\)]+|/[^\s"'<>\)]+)["']''',
    re.IGNORECASE
)


# === 探嗅规则接口 ===

class SniffRule:
    """探嗅规则基类

    子类实现 match() 和 extract()，由 ResourceSniffer 调度。
    需要异步网络请求（如调站点 API）的规则可重写 extract_async()。
    """

    name: str = "base"

    def match(self, url: str, content_type: str, text: str) -> bool:
        """是否适用此规则"""
        return False

    def extract(self, url: str, text: str) -> List[SniffedResource]:
        """从内容中提取资源（同步，用于 HTML/正则等无需网络请求的规则）"""
        return []

    async def extract_async(self, url: str, text: str,
                            http_client=None) -> List[SniffedResource]:
        """从内容中提取资源（异步，用于需要调站点 API 的规则）

        默认返回空列表，子类按需重写。
        """
        return []


class _HTMLRule(SniffRule):
    """HTML 页面解析规则"""
    name = "html"

    def match(self, url: str, content_type: str, text: str) -> bool:
        return 'html' in content_type or text.lstrip().lower().startswith('<!doctype') \
            or text.lstrip().startswith('<html')

    def extract(self, url: str, text: str) -> List[SniffedResource]:
        parser = _MediaExtractor(url)
        try:
            parser.feed(text)
        except Exception as e:
            log.debug(f"HTML 解析异常: {e}")
        return parser.results


class _JSONRule(SniffRule):
    """JSON API 响应解析规则

    适用于返回 JSON 的 API（如 {"url": "xxx", "data": {...}}），
    递归搜索所有字符串字段中的媒体 URL。
    """
    name = "json"

    def match(self, url: str, content_type: str, text: str) -> bool:
        return 'json' in content_type or text.lstrip().startswith(('{', '['))

    def extract(self, url: str, text: str) -> List[SniffedResource]:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        results: List[SniffedResource] = []
        seen = set()

        def walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]")
            elif isinstance(obj, str) and len(obj) < 4096:
                rtype = classify_url(obj)
                if rtype != ResourceType.UNKNOWN and obj not in seen:
                    seen.add(obj)
                    abs_url = urljoin(url, obj)
                    # 用字段名做 label 启发
                    label = ""
                    if 'res' in path.lower() or 'quality' in path.lower():
                        label = path.split('.')[-1]
                    results.append(SniffedResource(
                        url=abs_url, type=rtype, source=f'json:{path}',
                        label=label, confidence=0.75,
                        extra={'json_path': path}
                    ))

        walk(data)
        return results


class _RegexRule(SniffRule):
    """正则兜底规则：从任意文本中提取媒体 URL

    当 HTML/JSON 解析都没命中时，用正则从 JS 变量、注释、data 属性中提取。
    """
    name = "regex"

    def match(self, url: str, content_type: str, text: str) -> bool:
        return True  # 总是适用，作为兜底

    def extract(self, url: str, text: str) -> List[SniffedResource]:
        results: List[SniffedResource] = []
        seen = set()

        # 1. data-* 属性
        for m in _DATA_ATTR_RE.finditer(text):
            u = m.group(1)
            if u not in seen:
                seen.add(u)
                results.append(SniffedResource(
                    url=urljoin(url, u),
                    type=classify_url(u),
                    source='regex:data_attr',
                    confidence=0.6
                ))

        # 2. 引号包裹的媒体 URL（JS 变量）
        for m in _QUOTED_URL_RE.finditer(text):
            u = m.group(1)
            if u not in seen:
                seen.add(u)
                results.append(SniffedResource(
                    url=urljoin(url, u),
                    type=classify_url(u),
                    source='regex:quoted',
                    confidence=0.5
                ))

        return results


# === 主探嗅器 ===

class ResourceSniffer:
    """资源探嗅器

    用法：
        sniffer = ResourceSniffer(http_client=http_client)
        result = await sniffer.sniff(url)
        if result.direct_urls:
            best = result.best
            await downloader.download_async(best.url, output_path)
    """

    def __init__(self, http_client=None, timeout: float = 30.0,
                 max_content_size: int = 5 * 1024 * 1024):
        """
        Args:
            http_client: 可选的 HttpClient 实例（带 UA/cookie/proxy），
                         None 时内部创建临时 client
            timeout: 单次页面请求超时
            max_content_size: 最多读取的页面字节数（防止超大响应）
        """
        self._http = http_client
        self._timeout = timeout
        self._max_size = max_content_size
        # 内置规则（按优先级顺序）
        self._rules: List[SniffRule] = [
            _HTMLRule(),
            _JSONRule(),
            _RegexRule(),  # 兜底
        ]
        # 注册站点专用规则（GitHub/Bilibili 等，需调 API 的规则）
        # 延迟导入避免循环依赖，并放在通用规则之前以优先命中
        try:
            from .sites import get_builtin_site_rules
            self._rules = get_builtin_site_rules() + self._rules
        except ImportError:
            pass
        # 探嗅结果缓存（url → SniffResult），避免同一 URL 重复请求
        self._cache: Dict[str, SniffResult] = {}

    def register_rule(self, rule: SniffRule, priority: int = None):
        """注册自定义探嗅规则

        Args:
            rule: 规则实例
            priority: 插入位置（None=追加到末尾，0=最高优先级）
        """
        if priority is None:
            self._rules.append(rule)
        else:
            self._rules.insert(priority, rule)
        log.debug(f"注册探嗅规则: {rule.name} (共 {len(self._rules)} 条)")

    async def sniff(self, url: str,
                    headers: Optional[Dict[str, str]] = None) -> SniffResult:
        """探嗅 URL，返回直链列表

        Args:
            url: 任意 URL（直链或页面）
            headers: 额外请求头（如 Referer/Cookie）

        Returns:
            SniffResult，含 direct_urls 列表
        """
        # 缓存命中
        if url in self._cache:
            return self._cache[url]

        result = SniffResult(original_url=url)

        # 1. 如果本身是直链，直接返回
        rtype = classify_url(url)
        if rtype != ResourceType.UNKNOWN:
            result.is_direct = True
            result.direct_urls = [SniffedResource(
                url=url, type=rtype, source='direct',
                confidence=1.0
            )]
            self._cache[url] = result
            return result

        result.sniffed = True
        all_resources: List[SniffedResource] = []
        text = ''  # 页面内容（站点规则命中时可能不拉取页面，保持空字符串）

        # 2. 优先尝试站点专用规则（异步，直接调站点 API，不需要页面 HTML）
        #    这样可绕过 B 站等强反爬站点的页面拉取 412 问题
        for rule in self._rules:
            if not hasattr(rule, 'extract_async'):
                continue
            try:
                # 站点规则 match 只看 URL，不依赖页面内容
                if rule.match(url, '', ''):
                    resources = await rule.extract_async(
                        url, '', http_client=self._http
                    )
                    if resources:
                        log.debug(f"站点规则 {rule.name} 命中 {len(resources)} 个资源")
                        all_resources.extend(resources)
                        result.original_url = url
            except Exception as e:
                log.debug(f"站点规则 {rule.name} 异常: {e}")

        # 3. 如果站点规则已命中，跳过页面拉取（避免不必要的请求）
        if not all_resources:
            # 4. 拉取页面内容，跑通用规则
            try:
                text, content_type, final_url = await self._fetch_page(url, headers)
            except Exception as e:
                result.error = f"页面拉取失败: {e}"
                log.debug(f"探嗅拉取失败 {url}: {e}")
                if all_resources:
                    # 站点规则有部分结果时仍返回（已在上面收集）
                    pass
                else:
                    self._cache[url] = result
                    return result
            else:
                result.original_url = final_url

                # 5. 依次应用通用规则
                for rule in self._rules:
                    try:
                        if rule.match(final_url, content_type, text):
                            resources = rule.extract(final_url, text)
                            if resources:
                                log.debug(f"规则 {rule.name} 命中 {len(resources)} 个资源")
                                all_resources.extend(resources)
                    except Exception as e:
                        log.debug(f"规则 {rule.name} 异常: {e}")

        # 去重 + 排序（按置信度降序）
        seen = set()
        for r in all_resources:
            if r.url not in seen:
                seen.add(r.url)
                result.direct_urls.append(r)
        result.direct_urls.sort(key=lambda r: -r.confidence)

        # 提取页面标题
        if '<title>' in text.lower():
            m = re.search(r'<title[^>]*>(.*?)</title>', text,
                          re.IGNORECASE | re.DOTALL)
            if m:
                result.page_title = m.group(1).strip()[:200]

        log.info(f"探嗅 {url} → {len(result.direct_urls)} 个直链"
                 f"{' (含 M3U8)' if any(r.type == ResourceType.M3U8 for r in result.direct_urls) else ''}")

        self._cache[url] = result
        return result

    async def _fetch_page(self, url: str,
                          headers: Optional[Dict[str, str]] = None
                          ) -> tuple:
        """拉取页面内容

        Returns:
            (text, content_type, final_url)
        """
        if self._http is not None:
            # 用注入的 HttpClient（带反爬能力）
            return await self._http.fetch_text(
                url, headers=headers, timeout=self._timeout,
                max_size=self._max_size
            )

        # 兜底：用 aiohttp 直接拉取（无反爬能力，但保证可用）
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers,
                                   allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                content_type = resp.headers.get('content-type', '')
                # 只读 max_size 字节，防止超大响应
                raw = await resp.content.read(self._max_size)
                # 尝试按 charset 解码
                charset = resp.charset or 'utf-8'
                try:
                    text = raw.decode(charset, errors='replace')
                except (LookupError, UnicodeDecodeError):
                    text = raw.decode('utf-8', errors='replace')
                return text, content_type, str(resp.url)

    def clear_cache(self):
        """清空探嗅缓存"""
        self._cache.clear()

    def list_rules(self) -> List[str]:
        """列出已注册的规则名"""
        return [r.name for r in self._rules]


__all__ = [
    'ResourceSniffer',
    'SniffResult',
    'SniffedResource',
    'ResourceType',
    'SniffRule',
    'classify_url',
    'is_direct_link',
]
