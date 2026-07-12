"""
FTP/FTPS 协议驱动
"""

import asyncio
import aioftp
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from urllib.parse import urlparse

from .base import ProtocolDriver, DownloadHandle


class FTPDriver(ProtocolDriver):
    """FTP/FTPS 下载驱动"""
    
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
    
    def match(self, url: str) -> bool:
        return url.startswith(("ftp://", "ftps://"))
    
    async def probe(self, url: str) -> Dict[str, Any]:
        """探测 FTP 文件信息"""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 21
        username = parsed.username or "anonymous"
        password = parsed.password or "anon@"
        path = parsed.path or "/"
        
        client = aioftp.Client()
        try:
            await client.connect(host, port)
            await client.login(username, password)
            
            # 获取文件大小
            info = await client.stat(path)
            size = info.get('size', 0)
            
            return {
                'size': size,
                'supports_range': False,  # FTP 不常用 Range
                'filename': path.split('/')[-1] or 'ftp_file',
                'username': username,
                'host': host
            }
        finally:
            client.close()
    
    async def download(self, handle: DownloadHandle,
                       callback: Optional[Callable[[int, int, int], None]] = None):
        """下载 FTP 文件"""
        parsed = urlparse(handle.url)
        host = parsed.hostname or ""
        port = parsed.port or 21
        username = parsed.username or "anonymous"
        password = parsed.password or "anon@"
        path = parsed.path or "/"
        
        output_path = Path(handle.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        client = aioftp.Client()
        try:
            await client.connect(host, port)
            await client.login(username, password)
            
            downloaded = 0
            total = handle.total_size or 0
            
            # 下载流
            async with client.download_stream(path) as stream:
                with open(output_path, 'wb') as f:
                    async for data in stream.iter_chunked(8192):
                        f.write(data)
                        downloaded += len(data)
                        if callback:
                            callback(downloaded, total or downloaded, 0)
        finally:
            client.close()
    
    async def resume(self, handle: DownloadHandle,
                     callback: Optional[Callable[[int, int, int], None]] = None):
        """FTP 断点续传 - 从文件末尾开始"""
        parsed = urlparse(handle.url)
        host = parsed.hostname or ""
        port = parsed.port or 21
        username = parsed.username or "anonymous"
        password = parsed.password or "anon@"
        path = parsed.path or "/"
        
        output_path = Path(handle.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 检查已下载大小
        resume_pos = output_path.stat().st_size if output_path.exists() else 0
        
        client = aioftp.Client()
        try:
            await client.connect(host, port)
            await client.login(username, password)
            
            # 使用 REST 命令续传
            # aioftp 支持 resume
            downloaded = resume_pos
            total = handle.total_size or 0
            
            async with client.download_stream(path, offset=resume_pos) as stream:
                with open(output_path, 'ab') as f:
                    async for data in stream.iter_chunked(8192):
                        f.write(data)
                        downloaded += len(data)
                        if callback:
                            callback(downloaded, total or downloaded, 0)
        finally:
            client.close()