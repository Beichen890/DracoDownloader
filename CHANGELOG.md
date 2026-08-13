# Changelog

## [1.4.3] - 2026-08-12

### Fixed
- **CLI 版本号一致性**: `--version` 从 `__version__` 读取，不再硬编码 1.3.0
- **CLI URL 参数顺序**: 新增 `-u/--url` 命名参数，`-o file -u URL` 顺序可用（位置参数仍兼容）
- **带宽探测低估**: 探测窗口 3s→6s + 丢弃前 2s TCP slow start 段 + probe_size 5MB→10MB；`optimize_for_url` 优先用历史 `profile_for` 实际带宽，并与实时探测融合（历史权重 0.6）
- **断点保存间隔**: 自适应下载断点保存 2s→1s，崩溃最多丢 1s 进度

### Removed
- **DownloadEngine 死代码**: 删除 `engine.py` 及 `core.py`/`__init__.py` 中的引用（实例化后从未被下载流程使用）

## [1.4.2] - 2026-08-12

### Fixed
- **直链识别补充 .bin/.dat/.dump 后缀**: `sniffer.py` 的 `_DIRECT_LINK_EXTENSIONS` 新增通用二进制后缀，修复 `https://speed.hetzner.de/100MB.bin` 等测速文件被误判为非直链的问题
- **探嗅失败回退直接下载**: `core.py` 的 `_resolve_direct_url` 在探嗅无结果/探嗅异常（非反爬）时回退为直接下载，不再直接失败。避免无约定后缀的直链因探嗅失败而整体下载失败（AntiBotError 仍抛出）
- **CLI 资源泄漏**: `cli.py` 的 `main_async` 改用 `async with DracoDownloader(...)`，确保 HttpClient 的 aiohttp session 正确关闭，修复 "Unclosed client session" 警告
- **CLI --timeout 参数生效**: `download_async` 调用时传入 `timeout=args.timeout`，用户设置的超时不再被忽略

### Added
- `tests/test_sniffer_http.py`: `test_generic_binary_extensions` 验证 .bin/.dat/.dump/.flash 后缀识别
- `tests/test_direct_url_resolution.py`: 3 个回退测试（探嗅无结果/异常/best None → 回退直接下载）

## [1.4.1] - 2026-08-12

### Fixed
- **长尾分片卡死（核心修复）**: 解决多分片下载时单个分片拖慢整体速度的问题
  - `sock_read` 超时: 分片请求加 15s 单次读取间隔超时，防止服务器完全 hold 连接
  - **慢速 trickle 检测（速率守卫）**: sock_read 抓不到"持续发送少量数据"的 trickle 场景（如 CDN 路由变慢后以 9 KB/s 吐字节），新增应用层滑动窗口速率检测，grace 5s + 窗口 10s，速度低于 200 Kbps 抛 `TimeoutError` 触发重连
  - 孤儿分片接管: worker 连续超时退出后，监控器检测无活跃 worker 的分片并重新 spawn
  - 自适应控制器速度归零时触发紧急分裂（`AdaptiveSpeedupController.feed`）

### Added
- `tests/test_http_stall.py`: 完全 stall + 慢速 trickle 两类场景的 4 个测试用例（合并路径 + 自适应路径各 2 个）

## [1.4.0] - 2026-08-11

### Added
- **资源探嗅引擎**: 给定网页 URL 自动解析出可下载直链（`sniffer.py`），支持插拔式 `SniffRule` 框架
- **站点专用规则**: GitHub（release/blob/archive + 加速站候选）、Bilibili（WBI 签名 + DASH 流 + 分P）
- **反反爬 HTTP 客户端**: `http_client.py` 持久 session + UA 轮换 + cookie 复用 + 智能重试 + `AntiBotError` 分类
- **clean_headers 模式**: 调 REST API 时剥离浏览器伪装头，避免触发 403/412
- **Bilibili 扫码登录**: `auth.py` passport API 扫码 + cookie 持久化，登录后可下载 1080P+ 高画质
- **站点规则优先执行**: 直接调站点 API 绕过强反爬页面拉取
- **cookie 注入/读取**: `HttpClient.set_cookies()` / `get_cookies()`，domain 作用域到子域
- **DracoDownloader 登录态 API**: `login_bilibili()` / `check_bilibili_login()` / `logout_bilibili()`，启动自动加载已保存 cookie
- **测试**: `tests/test_sites.py`、`tests/test_sniffer_http.py`、`tests/test_direct_url_resolution.py`、`tests/test_auth.py`
- **端到端实测脚本**: `.e2e/run_sites_e2e.py`（GitHub/Bilibili 探嗅 + 可选扫码登录）

### Changed
- `protocols/http.py`、`protocols/m3u8.py` 接入共享 HttpClient（UA/cookie/proxy/Referer 复用）
- `core.py` 集成探嗅器 + HttpClient + BilibiliAuth，新增 `bilibili_storage_path` 参数
- `errors.py` 新增 `AntiBotError` 反爬错误类

## [1.3.2] - 2026-07-23

### Fixed
- **BitTorrent `_request_piece` 死循环**: peer 阻塞时改为等待后继续请求，避免下载卡住
- **BitTorrent 重复 piece 块导致数据损坏**: `Piece.add_block()` 增加重复检测，`_on_piece_data()` 增加块大小验证
- **BitTorrent 多文件写入边界错误**: 修复跨越文件边界的 piece 写入位置计算，防止数据丢失
- **Tracker announce 阻塞事件循环**: 将 `urllib.request` 同步调用改为 `aiohttp` 异步调用
- **HTTPDriver 临时目录泄漏**: 下载异常时清理 `.parts` 临时目录
- **M3U8 进度回调并发安全**: 使用分片索引和大小直接计算进度，避免 glob 竞态
- **PyPI 维护者信息**: 修正 `pyproject.toml` 作者为 `Beichen890 <15530226931@163.com>`

### Added
- **数据完整性测试**: 新增 `TestBTDownloaderDataIntegrity` 测试类

## [1.3.1] - 2026-07-23

### Fixed
- 同 v1.3.2（元数据维护者信息未修正）

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
