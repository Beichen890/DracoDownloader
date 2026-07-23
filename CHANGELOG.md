# Changelog

## [1.3.1] - 2026-07-23

### Fixed
- **BitTorrent `_request_piece` 死循环**: peer 阻塞时改为等待后继续请求，避免下载卡住
- **BitTorrent 重复 piece 块导致数据损坏**: `Piece.add_block()` 增加重复检测，`_on_piece_data()` 增加块大小验证
- **BitTorrent 多文件写入边界错误**: 修复跨越文件边界的 piece 写入位置计算，防止数据丢失
- **Tracker announce 阻塞事件循环**: 将 `urllib.request` 同步调用改为 `aiohttp` 异步调用
- **HTTPDriver 临时目录泄漏**: 下载异常时清理 `.parts` 临时目录
- **M3U8 进度回调并发安全**: 使用分片索引和大小直接计算进度，避免 glob 竞态

### Added
- **数据完整性测试**: 新增 `TestBTDownloaderDataIntegrity` 测试类

## [1.3.0] - 2026-07-22

### Added
- **TaskStep 步骤化管线**: 将下载流程分解为 probe→download→merge→verify→seed，Agent 可观测
- **集中化错误目录**: 稳定错误码 + 可重试标记，供 Agent 程序化处理
- **BT 多源加载器**: magnet / URL / 本地文件统一入口
- **Web Tracker 自动合并**: 拉取公开 tracker 列表与种子自带合并去重
- **BT 做种策略**: 分享率/时长双限速
- **配置系统**: ConfigItem + 校验器，支持环境变量 `DRACO_*` 覆盖
- **顺序下载模式**: 支持边下边看场景

## [1.1.0] - 2025-07-12

### Added
- **M3U8 AES-128 解密支持**: 解析 `#EXT-X-KEY` 标签，下载密钥，CBC 模式解密 TS 分片 (#4)
- **BT 稀缺分片优先策略**: `_select_rarest_piece()` 算法替代顺序请求，提升 swarm 效率 (#5)
- **DHT 引导节点配置化**: 新增 `DRACO_DHT_BOOTSTRAP_NODES` 环境变量支持 (#6)
- **集成测试套件**: Bencode/磁力链接/路由/调度器/Core API/进度管理 全覆盖测试 (#8)
- **`--verify` 校验选项**: CLI 支持 MD5/SHA1/SHA256 下载后文件哈希校验 (#9)
- **并发调度器**: 完整重写 `Scheduler._worker`，支持队列/超时/重试/取消传播 (#3)
- **OpenCodec Skill**: 用于 DracoDownloader 的 Openclaw 兼容 skill 文件配置

### Changed
- **core.download() 事件循环检测**: 修复 try/except 误捕获导致 async 上下文调用 `asyncio.run()` 的 bug (#2)
- **core.download() API**: 移除未使用的 `wait` 参数
- **Scheduler**: `set_executor()` 注入执行模式，`wait_for()` 异步等待结果
- **进度流**: `download_stream()` 轮询间隔 0.1s→0.5s，改进取消传播
- **HTTP 驱动**: 提取魔法数字为模块常量，增强 Range 416 处理
- **代码质量**: 替换所有裸 `except:` 为具体异常类型，统一日志级别

### Removed
- `wait` 参数从 `download()` 和 `_download_async()` 签名中移除

### Documentation
- 新增 `README.md`（中英双语，含架构图、API 示例、配置表）
- 新增 `LICENSE`（MIT）
- 新增 `CONTRIBUTING.md`（贡献指南）
- 新增 `NOTICE.md`（依赖声明和许可证合规）
- 更新 `requirements.txt` 为 pip 可安装格式
- 新增 `.codewhale/skills/draco-downloader/` skill 目录

## [1.0.0] - 2025-06-?? (Initial)

### Added
- HTTP/HTTPS 多分片并发下载
- FTP/FTPS 协议支持
- M3U8/HLS 流下载（基础版）
- BitTorrent / 磁力链接下载
- DHT Kademlia 网络
- Peer Wire Protocol
- 进度持久化管理
- 日志系统
- 命令行工具
