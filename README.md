# DracoDownloader

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**纯自研 Python 多协议下载器** — 无 GPL 依赖，支持 HTTP/HTTPS、FTP/FTPS、M3U8/HLS、BitTorrent / 磁力链接。
AI时代的自动档下载器！！！你还在为下载python而花费8美元吗？你还在为网络不通而烦恼吗？你还在为Openclaw不停重试而支付吗？

---

## 功能特性

### 协议支持
| 协议 | 特性 | 依赖 |
|------|------|------|
| HTTP/HTTPS | 多分片并发、断点续传、Range 探测 | aiohttp |
| FTP/FTPS | 被动模式、断点续传 (REST) | aioftp |
| M3U8/HLS | 主/子清单选择、AES-128 解密、多分片并发 | aiohttp, pycryptodome |
| BitTorrent | DHT (Kademlia)、Peer Wire Protocol、磁力链接、.torrent 文件 | 纯自研 |

### 架构亮点
- **纯自研 BT 协议栈**: Bencode、DHT、Peer Wire Protocol 全部自研，零 GPL 依赖
- **并发调度器**: 带队列、并发控制、超时管理、自动重试、取消传播
- **稀缺分片优先**: BT 下载采用稀有优先算法，提升 swarm 效率
- **配置化 DHT**: 引导节点可通过环境变量 `DRACO_DHT_BOOTSTRAP_NODES` 配置
- **进度持久化**: 支持断点续传进度文件 (.progress)
- **文件校验**: 内置 `--verify` 支持 MD5/SHA1/SHA256

---

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

或者仅安装核心功能（不含 AES-128 解密）：

```bash
pip install aiohttp aioftp
```

### 命令行使用

```bash
# HTTP/HTTPS 下载
python -m DracoDownloader https://example.com/file.zip -o file.zip

# M3U8 流下载
python -m DracoDownloader https://example.com/stream.m3u8 -o video.mp4

# 磁力链接下载
python -m DracoDownloader "magnet:?xt=urn:btih:..." -o ./downloads

# 文件校验
python -m DracoDownloader https://example.com/file.zip -o file.zip --verify sha256

# 指定期望哈希值
python -m DracoDownloader https://example.com/file.zip -o file.zip --verify sha256 --hash abc123...

# 列出支持的协议
python -m DracoDownloader --list-protocols
```

### Python API

```python
import asyncio
from DracoDownloader import DracoDownloader

async def main():
    downloader = DracoDownloader(max_concurrent=5)

    # 异步下载
    result = await downloader.download_async(
        url="https://example.com/file.zip",
        output_path="./downloads/file.zip",
        callback=lambda event: print(f"进度: {event.progress:.1f}%")
    )
    print(f"成功: {result.success}, 大小: {result.size} bytes")

    # 流式进度迭代
    async for event in downloader.download_stream(
        url="https://example.com/large-file.iso",
        output_path="./downloads/large-file.iso"
    ):
        print(f"进度: {event.progress:.1f}%, 速度: {event.speed // 1024} KB/s")

asyncio.run(main())
```

---

## 项目结构

```
DracoDownloader/
├── __init__.py              # 包入口，导出主要类和版本
├── core.py                  # DracoDownloader 主类，API 入口
├── scheduler.py             # 并发调度器（队列/超时/重试/取消）
├── engine.py                # 下载引擎（生命周期管理）
├── progress.py              # 进度持久化管理
├── logger.py                # 日志系统
├── cli.py                   # 命令行工具
├── requirements.txt         # 依赖声明
│
├── protocols/               # 协议驱动
│   ├── __init__.py          # 协议路由器 ProtocolRouter
│   ├── base.py              # 抽象基类 ProtocolDriver + DownloadHandle
│   ├── http.py              # HTTP/HTTPS 多分片下载
│   ├── ftp.py               # FTP/FTPS 下载
│   ├── m3u8.py              # M3U8/HLS 下载（支持 AES-128）
│   └── torrent.py           # BitTorrent 协议桥接
│
├── bittorrent/              # 纯自研 BT 协议栈
│   ├── __init__.py
│   ├── bencode.py           # BEP 3 编解码器
│   ├── dht.py               # Kademlia DHT 网络（配置化引导节点）
│   ├── downloader.py        # 完整 BT 下载器（稀缺分片优先）
│   ├── magnet.py            # 磁力链接解析器 (BEP 9)
│   ├── peer.py              # Peer Wire Protocol (BEP 3/6/10)
│   └── utils.py             # 工具函数
│
└── tests/                   # 测试套件
    └── __init__.py          # 集成测试（pytest + pytest-asyncio）
```

---

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DRACO_DHT_BOOTSTRAP_NODES` | `router.bittorrent.com:6881,...` | DHT 引导节点列表（逗号分隔） |
| `DRACO_LOG_LEVEL` | `INFO` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `DRACO_LOG_FILE` | (空) | 日志文件路径（可选） |

示例：

```bash
# 使用自定义 DHT 引导节点
export DRACO_DHT_BOOTSTRAP_NODES="dht.example.com:6881,router.bittorrent.com:6881"

# 开启调试日志
export DRACO_LOG_LEVEL=DEBUG

# 日志输出到文件
export DRACO_LOG_FILE=./draco.log
```

---

## 依赖声明

DracoDownloader 的依赖设计原则是 **零 GPL 依赖**，所有协议的实现均为自主开发或使用宽松许可证的库。

| 依赖 | 许可证 | 用途 | 是否必需 |
|------|--------|------|----------|
| aiohttp | Apache 2.0 | HTTP/HTTPS 协议 | 是 |
| aioftp | Apache 2.0 | FTP/FTPS 协议 | 是 |
| pycryptodome | BSD-2-Clause | M3U8 AES-128 解密 | 否（可选） |

### 自研代码

以下模块为完全自主开发，不依赖任何第三方库：

- `bittorrent/bencode.py` — Bencode 编解码器 (BEP 3)
- `bittorrent/dht.py` — Kademlia DHT 网络 (BEP 5/BEP 9)
- `bittorrent/peer.py` — Peer Wire Protocol (BEP 3/BEP 6/BEP 10)
- `bittorrent/downloader.py` — BT 下载器
- `bittorrent/magnet.py` — 磁力链接解析器
- `protocols/m3u8.py` — M3U8/HLS 流下载器

这些模块的代码完全遵从 [MIT License](LICENSE)。

---

## 测试

```bash
# 安装测试依赖
pip install pytest pytest-asyncio

# 运行测试
python -m pytest tests/ -v

# 运行特定测试
python -m pytest tests/ -v -k "TestBencode"
python -m pytest tests/ -v -k "TestProtocolRouter"
```

---

## License

[MIT License](LICENSE) — 请自由使用、修改和分发。

---

## 关于

DracoDownloader 是 DracoHub 项目的一部分，专为 Agent 原生下载场景设计。
使用纯 Python 实现，在所有主流操作系统上均可运行。
