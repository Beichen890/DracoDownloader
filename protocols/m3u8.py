"""
M3U8/HLS 协议驱动 - 纯 Python 实现
支持 AES-128 解密、多分片并发、主/子清单选择
"""

import asyncio
import aiohttp
import re
import shutil
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Optional, Dict, Any, Callable, List, Tuple

from .base import ProtocolDriver, DownloadHandle


class M3U8Driver(ProtocolDriver):
    """M3U8/HLS 下载驱动 - 纯 Python，不依赖 FFmpeg"""

    def __init__(self, max_concurrent: int = 16, timeout: int = 30):
        self.max_concurrent = max_concurrent
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def match(self, url: str) -> bool:
        # M3U8 以 .m3u8/.m3u 结尾，且是 HTTP 协议
        if not (url.startswith('http://') or url.startswith('https://')):
            return False
        return url.endswith(('.m3u8', '.m3u')) or '.m3u8' in url.lower()

    async def probe(self, url: str) -> Dict[str, Any]:
        """探测 M3U8 信息"""
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                content = await resp.text()

            # 如果是主清单，找最佳子清单
            if '#EXT-X-STREAM-INF' in content:
                variant_url = self._find_best_variant(content, url)
                if variant_url:
                    async with session.get(variant_url) as resp2:
                        content = await resp2.text()

            segments = self._parse_segments(content, url)
            duration = self._total_duration(content)
            is_live = '#EXT-X-ENDLIST' not in content
            has_aes = '#EXT-X-KEY:METHOD=AES-128' in content

            # 尝试获取第一个分片的大小
            first_seg_size = 0
            if segments:
                try:
                    seg_url = segments[0]
                    if not seg_url.startswith('http'):
                        seg_url = urljoin(self._base_url(url), seg_url)
                    async with session.head(seg_url) as resp:
                        first_seg_size = int(resp.headers.get('content-length', 0))
                except Exception:
                    pass

            return {
                'size': len(segments) * (first_seg_size or 1024 * 1024),  # 估算
                'segments': len(segments),
                'duration': duration,
                'is_live': is_live,
                'is_encrypted': has_aes,
                'first_segment_size': first_seg_size,
                'filename': url.split('/')[-1].replace('.m3u8', '.mp4').replace('.m3u', '.mp4')
            }

    def _parse_key_info(self, content: str, base_url: str) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
        """解析 #EXT-X-KEY 信息"""
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('#EXT-X-KEY:'):
                params_str = line[len('#EXT-X-KEY:'):]
                params = {}
                for p in re.finditer(r'(\w+)=("([^"]*)"|([^",\s]+))', params_str):
                    key = p.group(1)
                    value = p.group(3) or p.group(4) or ''
                    params[key] = value

                method = params.get('METHOD', 'NONE')
                if method != 'AES-128':
                    return (method, None, None)

                key_uri = params.get('URI', '')
                if key_uri and not key_uri.startswith('http'):
                    key_uri = urljoin(base_url, key_uri)

                # 解析 IV（可选）
                iv_str = params.get('IV', '')
                iv = None
                if iv_str:
                    iv_hex = iv_str
                    if iv_hex.startswith('0x') or iv_hex.startswith('0X'):
                        iv_hex = iv_hex[2:]
                    try:
                        iv = bytes.fromhex(iv_hex.zfill(32))
                    except ValueError:
                        pass

                return ('AES-128', key_uri, iv)

        return ('NONE', None, None)

    async def _download_key(self, session: aiohttp.ClientSession,
                            key_uri: str) -> bytes:
        """下载 AES-128 密钥"""
        async with session.get(key_uri) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to download AES key: HTTP {resp.status}")
            return await resp.read()

    def _decrypt_segment(self, data: bytes, key: bytes, iv: bytes) -> bytes:
        """AES-128-CBC 解密 TS 分片"""
        try:
            from Crypto.Cipher import AES
        except ImportError:
            raise RuntimeError(
                "AES-128 decryption requires pycryptodome. "
                "Install it with: pip install pycryptodome"
            )

        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        return cipher.decrypt(data)

    async def download(self, handle: DownloadHandle,
                       callback: Optional[Callable[[int, int, int], None]] = None):
        """下载 M3U8 流（支持 AES-128 加密）"""
        output_path = Path(handle.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        url = handle.url
        base_url = self._base_url(url)

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            # 1. 获取主清单
            async with session.get(url) as resp:
                content = await resp.text()

            # 2. 如果是主清单，选最佳子清单
            if '#EXT-X-STREAM-INF' in content:
                variant_url = self._find_best_variant(content, url)
                if variant_url:
                    async with session.get(variant_url) as resp2:
                        content = await resp2.text()
                    base_url = self._base_url(variant_url)

            # 3. 解析 AES-128 密钥信息
            method, key_uri, iv = self._parse_key_info(content, base_url)
            aes_key = None
            if method == 'AES-128' and key_uri:
                log_key = getattr(self, '_log', None)
                # 使用模块级 logger
                from ..logger import get_logger
                logger = get_logger('m3u8')
                logger.info(f"AES-128 encrypted stream, downloading key: {key_uri[:60]}")
                aes_key = await self._download_key(session, key_uri)
                if len(aes_key) != 16:
                    raise RuntimeError(f"Invalid AES key length: {len(aes_key)} (expected 16)")

            # 4. 解析分片列表
            segments = self._parse_segments(content, url)
            if not segments:
                raise RuntimeError("未找到可下载的分片")

            # 5. 创建临时目录
            temp_dir = output_path.parent / f".{output_path.name}.segments"
            temp_dir.mkdir(exist_ok=True)

            # 6. 并发下载分片
            semaphore = asyncio.Semaphore(self.max_concurrent)
            tasks = []
            total_segments = len(segments)

            for i, seg_url in enumerate(segments):
                if not seg_url.startswith('http'):
                    seg_url = urljoin(base_url, seg_url)

                # 计算每个分片的 IV（如果未在 EXT-X-KEY 中指定）
                seg_iv = iv
                if method == 'AES-128' and seg_iv is None:
                    # 使用序列号作为 IV (BEP 20 / RFC 8216)
                    seq = i
                    seg_iv = (seq).to_bytes(16, byteorder='big')

                tasks.append(self._download_segment(
                    session, seg_url, temp_dir, i, semaphore,
                    callback, total_segments, i + 1,
                    aes_key=aes_key, iv=seg_iv
                ))

            await asyncio.gather(*tasks)

            # 7. 合并分片
            with open(output_path, 'wb') as out:
                for i in range(total_segments):
                    seg_file = temp_dir / f"segment_{i:08d}.ts"
                    if seg_file.exists():
                        with open(seg_file, 'rb') as f:
                            out.write(f.read())

            # 8. 清理
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _download_segment(self, session: aiohttp.ClientSession,
                                url: str, temp_dir: Path, idx: int,
                                semaphore: asyncio.Semaphore,
                                callback: Optional[Callable],
                                total: int, current: int,
                                aes_key: Optional[bytes] = None,
                                iv: Optional[bytes] = None):
        """下载单个 TS 分片（可选 AES-128 解密）"""
        output_file = temp_dir / f"segment_{idx:08d}.ts"

        if output_file.exists() and output_file.stat().st_size > 0:
            return

        for attempt in range(3):
            try:
                async with semaphore:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")

                        data = bytearray()
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            data.extend(chunk)

                    # AES-128 解密
                    if aes_key is not None and iv is not None:
                        data = bytearray(self._decrypt_segment(bytes(data), aes_key, iv))

                    # 写入文件
                    with open(output_file, 'wb') as f:
                        f.write(data)

                    if callback:
                        total_downloaded = sum(
                            f.stat().st_size for f in temp_dir.glob("segment_*")
                        )
                        callback(total_downloaded, 0, 0)
                    return
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)

    def _parse_segments(self, content: str, base_url: str) -> List[str]:
        """解析 M3U8 分片列表"""
        segments = []
        for line in content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                segments.append(line)
        return segments

    def _total_duration(self, content: str) -> float:
        """计算总时长"""
        total = 0.0
        for line in content.split('\n'):
            if line.startswith('#EXTINF:'):
                match = re.search(r'#EXTINF:([\d.]+)', line)
                if match:
                    total += float(match.group(1))
        return total

    def _find_best_variant(self, content: str, base_url: str) -> Optional[str]:
        """找最高分辨率子清单"""
        best_url = None
        best_res = 0
        lines = content.split('\n')

        for i, line in enumerate(lines):
            if 'RESOLUTION=' in line:
                match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                if match:
                    res = int(match.group(1)) * int(match.group(2))
                    if res > best_res:
                        best_res = res
                        if i + 1 < len(lines):
                            url = lines[i + 1].strip()
                            if url and not url.startswith('#'):
                                if not url.startswith('http'):
                                    url = urljoin(self._base_url(base_url), url)
                                best_url = url
        return best_url

    def _base_url(self, url: str) -> str:
        """提取 base URL"""
        parsed = urlparse(url)
        path = parsed.path
        if '/' in path:
            path = path[:path.rfind('/') + 1]
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    async def resume(self, handle: DownloadHandle,
                     callback: Optional[Callable[[int, int, int], None]] = None):
        """M3U8 断点续传"""
        await self.download(handle, callback)
