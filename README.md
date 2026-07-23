# DracoDownloader

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/draco-downloader)](https://pypi.org/project/draco-downloader/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
**Agent专用 Python 多协议下载器** — 无 GPL 依赖，支持 HTTP/HTTPS、FTP/FTPS、M3U8/HLS、BitTorrent / 磁力链接,AI时代的自动挡下载器！
> ⭐ **如果这个项目对你有帮助，欢迎点 Star 支持！**  
> Star 是开发者最大的动力，也是帮助更多人发现这个项目的最好方式 🙏
>
> ## 🌟 为什么选择 DracoDownloader？

> 你还在为下载 Python 包花费 8 美元而心痛吗？  
> 你还在为网络不通而烦恼吗？  
> 你还在为 OpenClaw 无休止的重试而支付巨额账单吗？  
> ** DracoDownloader —— 让 AI Agent 拥有属于自己的下载能力！**

| 特性 | DracoDownloader | 传统方案 (aria2/FFmpeg) |
|------|----------------|------------------------|
| **GPL 依赖** | ✅ **零** GPL 依赖 | ❌ 依赖 GPL 库 |
| **许可证** | ⚖️ MIT 宽松许可 | ⚠️ GPL 传染性 |
| **部署方式** | 📦 `pip install` 即用 | 🔧 需额外安装二进制 |
| **AI 集成** | 🤖 原生 async/await | 🔄 需进程包装 |
| **自研协议** | 🧬 完全自主可控 | 📚 依赖第三方实现 |

> **DracoDownloader**：多协议一键下载 + 自动镜像加速 + 智能分片并发，Agent 调用的最佳选择，省时省钱省心！
> v1.3.1 重磅更新：**TaskStep 步骤化管线** + **集中化错误目录** + **BT 多源加载器/Web Tracker/做种策略** + **配置系统** + **关键缺陷修复**，Agent 可观测性大幅提升！

---

## 功能特性

### 🚀 协议支持
| 协议 | 特性 | 依赖 |
|------|------|------|
| HTTP/HTTPS | 多分片并发、断点续传、Range 探测、**动态参数优化** | aiohttp |
| FTP/FTPS | 被动模式、断点续传 (REST) | aioftp |
| M3U8/HLS | 主/子清单选择、AES-128 解密、多分片并发 | aiohttp, pycryptodome |
| BitTorrent | DHT (Kademlia)、Peer Wire Protocol、磁力链接、.torrent 文件 | 纯自研 |

### 🌟 架构亮点
- **纯自研 BT 协议栈**: Bencode、DHT、Peer Wire Protocol 全部自研，零 GPL 依赖
- **并发调度器**: 带队列、并发控制、超时管理、自动重试、取消广播
- **🆕 动态优化引擎**: 自动探测网络条件，计算最优分片数、线程数和连接数（详见下方优化原理）
- **🆕 智能镜像选择器**: 多维度评分（延迟/带宽/DNS），自动切换到最快镜像
- **稀缺分片优先**: BT 下载采用稀有优先算法，提升 swarm 效率
- **配置化 DHT**: 引导节点可通过环境变量 `DRACO_DHT_BOOTSTRAP_NODES` 配置
- **进度持久化**: 支持断点续传进度文件 (.progress)
- **文件校验**: 内置 `--verify` 支持 MD5/SHA1/SHA256

---

## 快速开始

### 安装

```bash
# 从 PyPI 安装（推荐）
pip install draco-downloader

# 或者从源码安装
pip install -r requirements.txt
```

### 命令行使用

```bash
# HTTP/HTTPS 下载（自动优化分片/线程数）
python -m DracoDownloader https://example.com/file.zip -o file.zip

# 启用自动镜像选择（自动选择最快镜像站）
python -m DracoDownloader https://example.com/file.zip -o file.zip --mirror

# 预分析模式：只展示优化信息，不下载
python -m DracoDownloader https://example.com/file.zip -o file.zip --dry-run --optimize

# 禁用自动优化（使用原始固定参数）
python -m DracoDownloader https://example.com/file.zip -o file.zip --no-optimize

# M3U8 流下载
python -m DracoDownloader https://example.com/stream.m3u8 -o video.mp4

# 磁力链接下载
python -m DracoDownloader "magnet:?xt=urn:btih:..." -o ./downloads

# 文件校验
python -m DracoDownloader https://example.com/file.zip -o file.zip --verify sha256

# 列出支持的协议
python -m DracoDownloader --list-protocols
```

### Python API

```python
import asyncio
from DracoDownloader import DracoDownloader

async def main():
    # 创建下载器（启用自动优化和镜像选择）
    downloader = DracoDownloader(
        max_concurrent=5,
        auto_optimize=True,      # 自动计算最优分片/线程数
        auto_mirror=True,        # 自动选择最优镜像站
        mirror_region="cn",      # 镜像区域
    )

    # 异步下载（自动优化参数）
    result = await downloader.download_async(
        url="https://example.com/file.zip",
        output_path="./downloads/file.zip",
        callback=lambda event: print(f"进度: {event.progress:.1f}%")
    )
    print(f"成功: {result.success}, 大小: {result.size} bytes")
    if result.optimization:
        print(f"分片数: {result.optimization['shard_count']}, "
              f"线程数: {result.optimization['thread_count']}")

    # 预优化模式（先探测，再下载）
    params = await downloader.optimize_url("https://example.com/large-file.iso")
    print(f"推荐分片数: {params.shard_count}, 理由: {params.rationale}")

    # 流式进度迭代
    async for event in downloader.download_stream(
        url="https://example.com/large-file.iso",
        output_path="./downloads/large-file.iso"
    ):
        print(f"进度: {event.progress:.1f}%, 速度: {event.speed // 1024} KB/s")

asyncio.run(main())
```

### 镜像选择器独立使用

```python
import asyncio
from DracoDownloader import MirrorSelector, CN_MIRRORS

async def main():
    selector = MirrorSelector()
    best = await selector.select_best(CN_MIRRORS)
    print(f"最快镜像: {best.name}, 延迟: {best.latency_ms:.0f}ms, 带宽: {best.bandwidth_mbps:.1f}Mbps")

asyncio.run(main())
```

### 优化器独立使用

```python
import asyncio
from DracoDownloader import DownloadOptimizer

async def main():
    optimizer = DownloadOptimizer()
    params = await optimizer.optimize_for_url(
        url="https://example.com/file.zip",
        file_size=500 * 1024 * 1024  # 500MB
    )
    print(f"最优分片数: {params.shard_count}, 最优线程数: {params.thread_count}")

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
├── mirror_selector.py       # 🆕 镜像选择器（自动选最快镜像站）
├── optimizer.py             # 🆕 下载优化器（动态分片/线程数计算）
├── requirements.txt         # 依赖声明
│
├── protocols/               # 协议驱动
│   ├── __init__.py          # 协议路由器 ProtocolRouter
│   ├── base.py              # 抽象基类 ProtocolDriver + DownloadHandle
│   ├── http.py              # HTTP/HTTPS 多分片下载（含优化集成）
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
│   └── utils.py              # 工具函数
│
└── tests/                   # 测试套件
    └── __init__.py          # 集成测试（pytest + pytest-asyncio）
```

---

## 动态优化原理

### 最优分片数计算
优化器综合以下三种方法计算最优分片数：
1. **基于目标分片大小** — 保证每个分片在 512KB~64MB 之间
2. **基于带宽延迟积 (BDP)** — 确保 TCP 窗口不被限制
3. **基于带宽并发限制** — 避免过多连接导致争用

最终取中位数作为推荐分片数。

### 最优线程数计算
线程数综合以下因素：
- **CPU 核心数** — IO 密集型场景使用 4x 核心数
- **网络延迟** — 高延迟需要更多并发连接补偿
- **可用带宽** — 高带宽需要更多线程打满

### 镜像选择策略
镜像站评分基于：
- **延迟 (40%)** — HTTP HEAD 请求响应时间
- **带宽 (40%)** — 实际测速下载带宽
- **DNS 解析时间 (20%)** — DNS 查询速度

内置中国大陆 **8 个主流镜像站**（华为云、阿里云、腾讯云、清华 TUNA、中科大 USTC、网易、上交 SJTUG、北外 BFSU）+ **国际 4 个 CDN 节点**。

---

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DRACO_DHT_BOOTSTRAP_NODES` | `router.bittorrent.com:6881,...` | DHT 引导节点列表（逗号分隔） |
| `DRACO_LOG_LEVEL` | `INFO` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `DRACO_LOG_FILE` | (空) | 日志文件路径（可选） |
| `DRACO_MIRROR_DISABLE` | (空) | 设为 `1` 禁用自动镜像选择 |
| `DRACO_OPTIMIZE_DISABLE` | (空) | 设为 `1` 禁用自动优化 |

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
- **`mirror_selector.py`** — 🆕 镜像选择器（纯自研）
- **`optimizer.py`** — 🆕 下载优化器（纯自研）

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

## ⭐ 支持这个项目
如果你觉得 DracoDownloader 有用，欢迎：
- ⭐ **点 Star** — 让更多人发现这个项目
- 🍴 **Fork** — 参与改进
- 🐛 **提 Issue** — 反馈问题
- 💬 **分享** — 推荐给需要的朋友

v1.3.1 新增特性与修复：
- 🦮 **自动最优镜像站选择** — 内置 12+ 镜像节点，自动选最快的
- ⚙️ **动态最优分片数/线程数计算** — 基于网络探测 + BDP 算法
- 📊 **--dry-run 预分析模式** — 下载前先看最优参数
- ⏱️ **带宽延迟积 (BDP) 优化算法** — 科学计算并发度
- 🧩 **TaskStep 步骤化管线** — 将下载分解为 probe→download→merge→verify→seed，Agent 可预览/观测/单步重试
- 🚨 **集中化错误目录** — 稳定错误码 + 可重试标记，Agent 程序化处理（如 `draco.http_status`）
- ⚙️ **配置系统** — ConfigItem + 校验器，环境变量 `DRACO_*` 覆盖，17 项可调参数
- 🧲 **BT 多源加载器** — magnet / URL / 本地文件统一入口，自动识别来源
- 🌐 **Web Tracker 自动合并** — 拉取公开 tracker 列表与种子自带合并去重，提高 peer 发现率
- 🌱 **BT 做种策略** — 分享率/时长双限速，下载完成后自动做种
- ▶️ **顺序下载** — 支持边下边看场景，按 piece 索引顺序下载
- 📈 **流式进度输出** — 滑动窗口实时速度计算，合并阶段持续进度回调（非阻塞合并）

-  ## ☕ 支持这个项目
[![爱发电](https://img.shields.io/badge/爱发电-支持我-FF6B6B?style=for-the-badge&logo=ko-fi&logoColor=white)](https://afdian.com/a/Beichen890)

你的每一杯咖啡，都是让我多写一行代码的动力 ☕

---

*DracoDownloader — AI Agent 时代的数据管道*