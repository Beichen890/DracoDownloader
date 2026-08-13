"""归档操作类 Draco 函数

跨平台的 tar/zip 压缩解压——用 Python 标准库 tarfile/zipfile 实现，
不依赖系统 tar/zip 命令（Windows 无 tar，Linux 行为差异）。
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path
from typing import List, Union


def _expand_sources(sources: List[str], cwd: str) -> List[str]:
    """把 sources 里的目录展开为文件列表（保留目录原样）

    Args:
        sources: 源路径列表（相对 cwd 或绝对）
        cwd: 当前工作目录

    Returns:
        归一化的路径列表（绝对路径）
    """
    result: List[str] = []
    for s in sources:
        p = s if os.path.isabs(s) else os.path.join(cwd, s)
        result.append(p)
    return result


def ftar(
    ctx,
    archive: str,
    sources: str = "",
    extract: bool = False,
    dest: str = ".",
    gzip: bool = False,
    verbose: bool = False,
    cwd: str = "",
) -> int:
    """tar 归档（替代 tar 命令，跨平台）

    用 tarfile 实现，Windows/Linux 行为一致。

    用法:
        ftar out.tar src            # 打包 src 到 out.tar
        ftar out.tar src gzip=true  # 打包并 gzip 压缩（out.tar.gz）
        ftar out.tar extract=true   # 解压 out.tar 到当前目录
        ftar out.tar.gz extract=true dest=./out  # 解压到 ./out
        ftar out.tar "src1 src2"    # 多源打包（空格分隔）

    Args:
        ctx: AgentContext
        archive: 归档文件路径
        sources: 源文件/目录列表（空格分隔）；extract=True 时忽略
        extract: True=解压，False=打包
        dest: 解压目标目录（仅 extract=True 时有效）
        gzip: 启用 gzip 压缩（.tar.gz / .tgz）
        verbose: 详细输出
        cwd: 工作目录（覆盖 session cwd）

    Returns:
        处理的文件数（打包：加入归档数；解压：释放文件数）；失败返回 -1
    """
    base_cwd = cwd or (ctx.cwd if ctx and hasattr(ctx, "cwd") else os.getcwd())
    archive_path = archive if os.path.isabs(archive) else os.path.join(base_cwd, archive)

    if extract:
        # 解压
        dest_path = dest if os.path.isabs(dest) else os.path.join(base_cwd, dest)
        os.makedirs(dest_path, exist_ok=True)
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                members = tf.getmembers()
                tf.extractall(dest_path)
        except (tarfile.TarError, OSError) as e:
            print(f"ftar: 解压失败: {e}")
            return -1
        if verbose:
            for m in members:
                print(f"  {m.name}")
        print(f"ftar: 解压 {len(members)} 项到 {dest_path}")
        return len(members)

    # 打包
    if not sources:
        print("ftar: 打包需要 sources 参数")
        return -1
    src_list = _expand_sources(sources.split(), base_cwd)
    mode = "w:gz" if gzip else "w"
    count = 0
    try:
        with tarfile.open(archive_path, mode) as tf:
            for src in src_list:
                if not os.path.exists(src):
                    print(f"ftar: 警告: {src} 不存在，跳过")
                    continue
                arcname = os.path.basename(os.path.normpath(src))
                tf.add(src, arcname=arcname, recursive=True)
                count += 1
                if verbose:
                    print(f"  + {arcname}")
    except (tarfile.TarError, OSError) as e:
        print(f"ftar: 打包失败: {e}")
        return -1
    print(f"ftar: 已打包 {count} 项到 {archive_path}")
    return count


def fzip(
    ctx,
    archive: str,
    sources: str = "",
    extract: bool = False,
    dest: str = ".",
    verbose: bool = False,
    cwd: str = "",
) -> int:
    """zip 归档（替代 zip/unzip 命令，跨平台）

    用 zipfile 实现，Windows/Linux 行为一致。

    用法:
        fzip out.zip src               # 压缩 src 到 out.zip
        fzip out.zip "src1 src2"       # 多源压缩
        fzip out.zip extract=true      # 解压 out.zip 到当前目录
        fzip out.zip extract=true dest=./out

    Args:
        ctx: AgentContext
        archive: zip 文件路径
        sources: 源文件/目录列表（空格分隔）
        extract: True=解压，False=压缩
        dest: 解压目标目录
        verbose: 详细输出
        cwd: 工作目录

    Returns:
        处理的文件数；失败返回 -1
    """
    base_cwd = cwd or (ctx.cwd if ctx and hasattr(ctx, "cwd") else os.getcwd())
    archive_path = archive if os.path.isabs(archive) else os.path.join(base_cwd, archive)

    if extract:
        dest_path = dest if os.path.isabs(dest) else os.path.join(base_cwd, dest)
        os.makedirs(dest_path, exist_ok=True)
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                members = zf.namelist()
                zf.extractall(dest_path)
        except (zipfile.BadZipFile, OSError) as e:
            print(f"fzip: 解压失败: {e}")
            return -1
        if verbose:
            for m in members:
                print(f"  {m}")
        print(f"fzip: 解压 {len(members)} 项到 {dest_path}")
        return len(members)

    if not sources:
        print("fzip: 压缩需要 sources 参数")
        return -1
    src_list = _expand_sources(sources.split(), base_cwd)
    count = 0
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in src_list:
                if not os.path.exists(src):
                    print(f"fzip: 警告: {src} 不存在，跳过")
                    continue
                if os.path.isfile(src):
                    arcname = os.path.basename(src)
                    zf.write(src, arcname)
                    count += 1
                    if verbose:
                        print(f"  + {arcname}")
                elif os.path.isdir(src):
                    root = os.path.dirname(os.path.normpath(src))
                    for dirpath, _, filenames in os.walk(src):
                        for fn in filenames:
                            full = os.path.join(dirpath, fn)
                            arcname = os.path.relpath(full, root)
                            zf.write(full, arcname)
                            count += 1
                            if verbose:
                                print(f"  + {arcname}")
    except (zipfile.BadZipFile, OSError) as e:
        print(f"fzip: 压缩失败: {e}")
        return -1
    print(f"fzip: 已压缩 {count} 项到 {archive_path}")
    return count
