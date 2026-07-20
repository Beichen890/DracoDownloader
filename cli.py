#!/usr/bin/env python3
"""
DracoDownloader 命令行工具
用法: python -m DracoDownloader <URL> -o <OUTPUT>

支持:
  - HTTP/HTTPS 多分片下载（含自动优化分片/线程）
  - FTP/FTPS 下载
  - M3U8/HLS 流下载（含 AES-128 加密）
  - BitTorrent / 磁力链接下载
  - 自动最优镜像站选择 (--mirror)
  - 动态最优分片/线程数计算 (--optimize)
  - 文件完整性校验 (--verify)
"""

import sys
import asyncio
import argparse
import hashlib
from pathlib import Path

from DracoDownloader import DracoDownloader


def _format_speed(speed_bps: int) -> str:
    """将 bytes/s 格式化为人类可读字符串"""
    if speed_bps >= 1024 * 1024 * 1024:
        return f"{speed_bps / 1024 / 1024 / 1024:.2f} GB/s"
    if speed_bps >= 1024 * 1024:
        return f"{speed_bps / 1024 / 1024:.2f} MB/s"
    if speed_bps >= 1024:
        return f"{speed_bps / 1024:.1f} KB/s"
    return f"{speed_bps} B/s"


def progress_callback(event):
    """进度回调"""
    bar_length = 40
    filled = int(bar_length * event.progress / 100)
    bar = '█' * filled + '░' * (bar_length - filled)
    speed_str = _format_speed(event.speed)
    # 进度已到 100% 但速度仍大于 0，说明正在合并分片
    if event.progress >= 100 and event.speed > 0:
        status = f"合并中 {speed_str}"
    else:
        status = speed_str
    print(f"\r[{bar}] {event.progress:.1f}% {status}", end='', flush=True)


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
    downloader = DracoDownloader(
        auto_optimize=args.optimize,
        auto_mirror=args.mirror,
        mirror_region=args.mirror_region,
    )

    if args.list_protocols:
        print("支持的协议:")
        for p in downloader.list_protocols():
            print(f"  - {p}")
        return

    # --dry-run 模式：只展示优化/镜像信息，不实际下载
    if args.dry_run and args.url:
        print(f"🔍 预分析: {args.url}")
        print(f"📁 输出: {args.output}")
        print(f"⚙️  自动优化: {'开启' if args.optimize else '关闭'}")
        print(f"🦮 自动镜像: {'开启' if args.mirror else '关闭'} (区域: {args.mirror_region})")

        if args.optimize:
            print("\n📊 正在探测网络条件并计算最优参数...")
            try:
                params = await downloader.optimize_url(args.url)
                print(f"  ✅ 最优分片数: {params.shard_count}")
                print(f"  ✅ 最优线程数: {params.thread_count}")
                print(f"  ✅ 分片大小: {params.chunk_size / 1024 / 1024:.1f} MB")
                print(f"  ✅ 最大连接数: {params.max_connections}")
                print(f"  ✅ 估计速度: {params.estimated_speed_mbps:.1f} Mbps")
                print(f"  🎯 说明: {params.rationale}")
            except Exception as e:
                print(f"  ⚠️ 优化失败: {e}")
        return

    print(f"📥 下载: {args.url}")
    print(f"📁 输出: {args.output}")
    if args.mirror:
        print(f"🦮 自动镜像: 开启 (区域: {args.mirror_region})")
    if args.optimize:
        print(f"⚙️  自动优化: 开启")

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

        # 显示优化信息
        if result.optimization:
            opt = result.optimization
            print(f"⚙️  优化信息:")
            print(f"  - 分片数: {opt.get('shard_count', '?')}")
            print(f"  - 线程数: {opt.get('thread_count', '?')}")
            print(f"  - 连接数: {opt.get('max_connections', '?')}")
            print(f"  - 估计速度: {opt.get('estimated_speed_mbps', 0):.1f} Mbps")

        # 显示镜像信息
        if result.mirror_used:
            print(f"🦮 镜像站: {result.mirror_used}")

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
        description="DracoDownloader - 多协议下载工具（支持自动镜像和优化）",
        epilog="示例:\n"
               "  python -m DracoDownloader https://example.com/file.zip -o file.zip\n"
               "  python -m DracoDownloader https://example.com/stream.m3u8 -o video.mp4\n"
               "  python -m DracoDownloader magnet:?xt=urn:btih:... -o movie\n"
               "  python -m DracoDownloader https://... -o file --verify sha256\n"
               "  python -m DracoDownloader https://... -o file --mirror --optimize\n"
               "  python -m DracoDownloader https://... -o file --dry-run --optimize\n"
    )

    parser.add_argument("url", nargs="?", help="下载链接")
    parser.add_argument("-o", "--output", help="输出路径")
    parser.add_argument("-p", "--proxy", help="代理地址 (http://或socks5://)")
    parser.add_argument("-t", "--timeout", type=int, default=3600,
                        help="下载超时秒数 (默认: 3600)")

    # 优化选项
    opt_group = parser.add_argument_group("优化选项")
    opt_group.add_argument("--optimize", action="store_true", default=True,
                           help="启用自动最优分片/线程数计算 (默认: 开启)")
    opt_group.add_argument("--no-optimize", action="store_false", dest="optimize",
                           help="禁用自动优化")
    opt_group.add_argument("--dry-run", action="store_true",
                           help="只预分析并展示最优参数，不实际下载")

    # 镜像选项
    mirror_group = parser.add_argument_group("镜像选项")
    mirror_group.add_argument("--mirror", action="store_true", default=False,
                              help="启用自动最优镜像站选择")
    mirror_group.add_argument("--mirror-region", default="cn",
                              choices=["cn", "global", "auto"],
                              help="镜像区域 (默认: cn，auto=自动选择最佳区域)")

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
                        version=f"DracoDownloader 1.3.0")

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
