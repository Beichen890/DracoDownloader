# NOTICE — DracoDownloader 第三方依赖声明

## 许可证合规性

DracoDownloader 是一个 **零 GPL 依赖** 的项目。
所有外部依赖均使用宽松许可证（MIT / Apache 2.0 / BSD）。

## 外部依赖

| 库 | 版本 | 许可证 | 用途 |
|---|------|--------|------|
| [aiohttp](https://github.com/aio-libs/aiohttp) | >=3.9 | Apache 2.0 | HTTP/HTTPS 客户端 |
| [aioftp](https://github.com/aio-libs/aioftp) | >=0.20,<0.21 | Apache 2.0 | FTP/FTPS 客户端 |
| [pycryptodome](https://github.com/Legrandin/pycryptodome) | >=3.20 | BSD-2-Clause | M3U8 AES-128 解密 |

## 自研代码

以下模块为完全自主开发，无任何第三方代码：

### bittorrent/ 协议栈
- `bencode.py` — Bencode 编解码器 (BEP 3)
- `dht.py` — Kademlia DHT 网络协议 (BEP 5 / BEP 9)
- `peer.py` — Peer Wire Protocol (BEP 3 / BEP 6 / BEP 10)
- `downloader.py` — BT 下载器完整实现
- `magnet.py` — 磁力链接解析器 (BEP 9)
- `utils.py` — 工具函数

### protocols/ 协议驱动
- `http.py` — HTTP/HTTPS 多分片并发下载
- `ftp.py` — FTP/FTPS 下载
- `m3u8.py` — M3U8/HLS 流下载（含 AES-128 解密）
- `torrent.py` — BitTorrent 协议桥接

### 核心模块
- `core.py` — DracoDownloader 主类
- `scheduler.py` — 并发任务调度器
- `engine.py` — 下载引擎
- `progress.py` — 进度持久化管理
- `logger.py` — 日志系统
- `cli.py` — 命令行接口

## BitTorrent 协议合规

本项目的 BT 实现遵循以下 BEP（BitTorrent Enhancement Proposal）：

- BEP 3: BitTorrent Protocol
- BEP 5: DHT Protocol
- BEP 6: Fast Extension (部分)
- BEP 9: Metadata Extension (磁力链接)
- BEP 10: Extension Protocol (部分)
- BEP 20: Peer ID Conventions
- BEP 42: DHT Security Extension

## 感谢

- BitTorrent 协议规范社区
- aiohttp 和 aioftp 项目团队
- Python 异步生态贡献者

---

*本文件最后更新: 2025-07-12*
