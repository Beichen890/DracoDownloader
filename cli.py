#!/usr/bin/env python3
"""
DracoDownloader 命令行工具
用法: python -m DracoDownloader <URL> -o <OUTPUT>

支持:
  - HTTP/HTTPS 多分片下载
  - FTP/FTPS 下载
  - M3U8/HLS 流下载（含 AES-128 加密）
  - BitTorrent / 磁力链接下载
  - 文件完整性校验 (--verify)
"""

import sys
import asyncio
import argparse
import hashlib
from pathlib import Path

from DracoDownloader import DracoDownloader


def progress_callback(event):
    """进度回调"""
    bar_length = 40
    filled = int(bar_length * event.progress / 100)
    bar = '█' * filled + '░' * (bar_length - filled)
    speed_mb = event.speed / 1024 / 1024
    print(f"\r[{bar}] {event.progress:.1f}% {speed_mb:.2f} MB/s", end='', flush=True)


def verify_file(path: str, algorithm: str = "sha256") -> str:
    """
    计算文件的哈希值用于校验

    Args:
        path: 文件路径
        algorithm: 哈希算法 (md5, sha1, sha256)

    Returns:
        十六进制哈希字符串
    """
    h = hashlib.new(algorithm)
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def main_async(args):
    downloader = DracoDownloader()

    if args.list_protocols:
        print("支持的协议:")
        for p in downloader.list_protocols():
            print(f"  - {p}")
        return

    print(f"📥 下载: {args.url}")
    print(f"📁 输出: {args.output}")

    result = await downloader.download_async(
        url=args.url,
        output_path=args.output,
        proxy=args.proxy,
        callback=progress_callback
    )

    print()
    if result.success:
        size_mb = result.size / 1024 / 1024
        print(f"✅ 完成! 大小: {size_mb:.2f} MB, "
              f"速度: {result.speed:.2f} MB/s, 耗时: {result.duration:.1f}s")

        # --verify 校验
        if args.verify:
            try:
                print(f"🔍 校验文件: {args.output}")
                file_hash = verify_file(args.output, args.verify)
                print(f"  {args.verify.upper()}: {file_hash}")
                if args.hash:
                    match = file_hash.lower() == args.hash.lower()
                    print(f"  {'✅ 匹配' if match else '❌ 不匹配'}"
                          f" (期望: {args.hash.lower()})")
            except FileNotFoundError:
                print(f"  ⚠️ 文件不存在: {args.output}")
            except Exception as e:
                print(f"  ⚠️ 校验失败: {e}")
    else:
        print(f"❌ 失败: {result.error}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="DracoDownloader - 多协议下载工具",
        epilog="示例:\n"
               "  python -m DracoDownloader https://example.com/file.zip -o file.zip\n"
               "  python -m DracoDownloader https://example.com/stream.m3u8 -o video.mp4\n"
               "  python -m DracoDownloader magnet:?xt=urn:btih:... -o movie\n"
               "  python -m DracoDownloader https://... -o file --verify sha256\n"
    )

    parser.add_argument("url", nargs="?", help="下载链接")
    parser.add_argument("-o", "--output", help="输出路径")
    parser.add_argument("-p", "--proxy", help="代理地址 (http://或socks5://)")
    parser.add_argument("-t", "--timeout", type=int, default=3600,
                        help="下载超时秒数 (默认: 3600)")

    # 校验选项
    verify_group = parser.add_argument_group("校验选项")
    verify_group.add_argument("--verify", nargs="?", const="sha256",
                              choices=["md5", "sha1", "sha256"],
                              help="下载后校验文件哈希 (默认: sha256)")
    verify_group.add_argument("--hash", help="期望的哈希值，与 --verify 配合使用")

    # 信息选项
    parser.add_argument("--list-protocols", action="store_true",
                        help="列出支持的协议")
    parser.add_argument("-v", "--version", action="version",
                        version=f"DracoDownloader 1.0.0")

    args = parser.parse_args()

    # --list-protocols 不需要 url
    if args.list_protocols:
        asyncio.run(main_async(args))
        return

    # 必须提供 url
    if not args.url:
        parser.print_help()
        sys.exit(1)

    if not args.output:
        parser.error("-o/--output 是必需的")

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
