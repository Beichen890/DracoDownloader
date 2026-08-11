# Ghost-Downloader-3 vs DracoDownloader 全维度对比报告

> 本报告基于对 Ghost-Downloader-3 仓库（GPL-3.0，作者 XiaoYouChR，克隆于 `.reference/`）的只读学习，
> 与 DracoDownloader（MIT，本仓库）实际源码逐项对比。
> **合规声明**：本报告只描述 Ghost 的设计与方法，不引用其任何源码片段。Draco 自身代码引用用 `file:///` 链接。

---

## 1. 项目定位与许可证

| 维度 | DracoDownloader | Ghost-Downloader-3 |
|------|----------------|--------------------|
| 许可证 | **MIT**（宽松，可商用闭源） | **GPL-3.0**（强传染性） |
| 定位 | AI Agent 原生下载**库**（pip install 即用，无 GUI） | 跨平台**桌面/移动应用**（PySide6 GUI） |
| 形态 | `draco-downloader` PyPI 包，纯 Python 库 | 可执行程序（Windows/Linux/macOS/Android 安装包） |
| 核心卖点 | 零 GPL 依赖、async/await、Agent 可观测（TaskStep） | IDM 式免合并分块、AI 智能加速、TLS 指纹、浏览器扩展 |
| 目标用户 | AI Agent / 开发者程序化调用 | 终端用户图形化下载 |

**关键差异**：Draco 是"库"，Ghost 是"应用"。Draco 不能引入 GPL 依赖（会迫使下游 Agent 项目变 GPL），
因此 Ghost 的 libtorrent、PySide6 等 GPL/LGPL 重量级依赖对 Draco 是红线。

---

## 2. 整体架构对比

### DracoDownloader 分层（[core.py](file:///workspace/core.py)、[protocols/](file:///workspace/protocols)）

```
core.py            # DracoDownloader 主类，API 入口
├── scheduler.py   # 并发调度器（队列/超时/重试/取消广播）
├── engine.py      # 下载引擎生命周期（active/paused 字典）
├── progress.py    # .progress 断点持久化
├── optimizer.py   # 🆕 下载优化器 + AdaptiveSpeedupController（本次新增）
├── mirror_selector.py  # 镜像选择器（12+ 节点 40/40/20 评分）
├── steps.py       # TaskStep 管线（probe→download→merge→verify→seed）
├── errors.py      # 集中化错误目录（稳定错误码 + retryable）
└── protocols/     # 协议驱动
    ├── base.py        # ProtocolDriver 抽象基类 + DownloadHandle
    ├── __init__.py    # ProtocolRouter 路由
    ├── http.py        # 🆕 HTTP 多分片 + 免合并定位写 + 自适应分裂
    ├── ftp.py         # FTP/FTPS
    ├── m3u8.py        # M3U8/HLS + AES-128
    └── torrent.py     # BT 桥接
└── bittorrent/    # 纯自研 BT 协议栈（零 GPL）
    ├── bencode.py / dht.py / peer.py / magnet.py / downloader.py
    ├── loaders.py / trackers.py / seeding.py
```

### Ghost-Downloader-3 分层（`app/` + `features/`）

```
app/
├── services/          # 服务层（signal-driven actors）
│   ├── task_service.py / feature_service.py / pack_loader.py
│   ├── speed_meter.py / plan.py / runtime_status.py
│   └── aria2_rpc.py / browser_service.py / coroutine_runner.py
├── models/            # Task / TaskStep / TaskFile 数据模型
├── platform/          # 跨平台适配（desktop/android/文件系统/通知）
├── view/              # PySide6 Fluent Design UI（cards/dialogs/pages/shell）
├── config/            # cfg.py 配置中心
└── client.py / container.py / signal_bus.py
features/              # 按协议分特性包（pack 插件化）
├── http_pack/         # HTTP 任务（含 AI 加速 _autoSpeedUp）
├── bittorrent_pack/   # BT 任务（libtorrent session 封装）
├── m3u8_pack/ / ftp_pack/ / ed2k_pack/ / ffmpeg_pack/
├── bili_pack/ / huggingface_pack/ / github_pack/ / yt_dlp_pack/
└── *_pack/manifest.toml  # pack 清单（entry/class/dependencies）
```

**架构差异要点**：
- Ghost 采用 **pack 插件化**（每个协议是一个独立 pack，有 `manifest.toml`），通过 `pack_loader` 动态加载。
  Draco 采用**静态 ProtocolRouter 路由**，协议驱动在 `protocols/` 下直接注册。
- Ghost 强调 **signal-driven actors**（[CLAUDE.md](file:///workspace/.reference/Ghost-Downloader-3/CLAUDE.md) 宪法），
  Service 拥有私有状态，对外只通过 Qt 信号通信。Draco 是纯 async 库，靠回调/事件迭代器通信。
- Ghost 有 **ADR（架构决策记录）** `docs/adr/0001-0008`，Draco 无（本次建议补）。
- Ghost 有 **封闭动词表**（load/save/fetch/probe/parse/match/build/create/set/update/refresh...），
  Draco 用常规 snake_case，无强制词汇表。

---

## 3. 协议栈对比

| 协议 | Draco 实现 | 依赖 | 许可证 | Ghost 实现 | 依赖 |
|------|-----------|------|--------|-----------|------|
| HTTP/HTTPS | 自研多分片 + 🆕 免合并定位写 + 自适应分裂 | aiohttp | Apache 2.0 | `http_pack` + wreqs(niquests) | niquests MIT |
| HTTP/2&3 | ❌ 不支持（aiohttp 限制） | - | - | ✅ niquests 支持 | niquests |
| TLS 指纹 | ❌ aiohttp 默认指纹 | - | - | ✅ 模拟真实浏览器 | niquests |
| FTP/FTPS | `protocols/ftp.py` 被动+REST续传 | aioftp | Apache 2.0 | `ftp_pack` | aioftp |
| M3U8/HLS | `protocols/m3u8.py` AES-128解密+多分片 | aiohttp+pycryptodome | BSD-2 | `m3u8_pack` + 直播录制+边下边解密 | + ffmpeg |
| BitTorrent | **纯自研** BEP3/5/6/9/10 + DHT + 稀缺优先 + 🆕 流水线+endgame | 无 | MIT | `bittorrent_pack` libtorrent 封装 | **libtorrent** |
| 磁力链接 | `bittorrent/magnet.py` BEP9 自研 | 无 | MIT | libtorrent `parse_magnet_url` | libtorrent |
| eD2k | ❌ 不支持 | - | - | `ed2k_pack/python_ed2k/` 纯 Python | 无 |
| 浏览器扩展 | ❌ 无 | - | - | `browser_extension/` TS+React 嗅探 | - |

**BT 实现路线根本不同**（重点章节）：
- **Draco**：`bittorrent/` 全部自研——Bencode 编解码、Kademlia DHT、Peer Wire Protocol、磁力解析、
  稀缺分片优先、做种策略。零 GPL 依赖，但功能不如 libtorrent 完整。
- **Ghost**：`features/bittorrent_pack/session.py` 是 libtorrent 的薄封装，
  DHT 状态持久化、resume data、文件优先级、UPnP/NAT-PMP、LSD、endgame、流水线等
  全部由 libtorrent C++ 内核提供，Python 层只做配置和 alert 路由。

**工程取舍**：libtorrent 功能完整但体积大（需编译 C++，Android 还要专门 recipe）；
Draco 自研栈轻量可控，但缺 UPnP/LSD/文件优先级/代理等。本次落地补了**流水线**和**endgame**两项核心性能优化。

---

## 4. HTTP 分块策略对比（重点）

| 维度 | Draco（优化前） | Draco（本次优化后） | Ghost |
|------|----------------|--------------------|-------|
| 分片方式 | 等分 N 片，落临时文件 | 等分 N 片，**定位写最终文件** | 等分 N 片，`pwrite` 定位写 |
| 是否需要合并 | ✅ 需要（16MiB 缓冲合并） | ❌ **免合并**（os.pwrite 直写偏移） | ❌ 免合并 |
| 预分配 | ❌ 临时文件 | ✅ `ftruncate` 预分配 | ✅ `ftruncate` |
| 运行时分裂最慢分片 | ❌ 固定分片 | ✅ **AdaptiveSpeedupController** | ✅ `_splitSlowest` + `_reassignSubworker` |
| 断点续传 | `.progress`（布尔数组） | `.progress`（每分片已写字节） | `.ghd`（每 worker 的 start/position/end） |
| 不支持 Range 降级 | 单流下载 | 自适应失败回退合并路径 | 降级单流，清 record |

**学到的核心思路**（已用原创代码落地，见 [optimizer.py](file:///workspace/optimizer.py) `AdaptiveSpeedupController` 与 [protocols/http.py](file:///workspace/protocols/http.py) `_download_adaptive`）：
1. **免合并**：预分配文件 + 各 worker 用定位写（`os.pwrite`）直接写对应偏移，省去合并 IO。
2. **运行时分裂**：监控速度稳定后，把剩余字节最多的分片一分为二，新增 worker 接管后半段。
3. **收益判定**：对比 worker 增比 vs 速度增比，收益不足则停止加速（避免无谓分裂）。

---

## 5. AI 加速对比（重点）

| 维度 | Draco | Ghost |
|------|-------|-------|
| 加速类型 | 规则化静态优化（下载前一次性探测） | **运行时自适应启发式**（非真 ML） |
| 速度采样 | BandwidthProbe 下载前测速 | `_speedHistory` 运行时 5 点采样 |
| 触发条件 | 文件大小 + BDP + 带宽并发限制 | 速度稳定（最大偏差 ≤15%） |
| 加速动作 | 计算最优分片数/线程数（取中位数） | 分裂最慢分片 +4 worker |
| 收益判定 | 无（一次性计算） | 5 秒后对比增比，收益不足则禁用 |
| 历史反馈 | ❌ 无（每次重新探测） | ❌ 无 | 🆕 Draco 新增 `record_actual` 按域名记忆 |

**Ghost "AI 加速"真身**（位于 `features/http_pack/task.py` 的 `_autoSpeedUp`）：
- 维护最近 5 个速度采样，计算平均速度与最大偏差
- 偏差 > 15%（速度不稳定）→ 不加速
- 首次触发：记录基线（worker 数、速度），分裂 4 次最慢分片
- 5 秒观察期后：`speedRatio < 0.8 * workerRatio` → 禁用自动加速；否则重置继续

**Draco 落地**（[optimizer.py](file:///workspace/optimizer.py) `AdaptiveSpeedupController`）：
- 同样的启发式思路（采样/稳定判定/基线/观察期/增比对比），但用 Draco 自己的 snake_case + 独立类实现
- 额外加了 `record_actual` / `profile_for`：下载完成后按域名记忆实际带宽，
  用指数加权（0.6 新 + 0.4 旧）融合，下次同站下载直接用历史画像，减少重复探测

**结论**：Ghost 的"AI"是营销词，实质是启发式反馈控制。Draco 学其思路并加了历史画像，比 Ghost 多一层跨任务记忆。

---

## 6. 并发与调度对比

| 维度 | Draco | Ghost |
|------|-------|-------|
| 调度器 | `scheduler.py`：队列+并发+超时+重试+取消广播 | libtorrent 内核调度 + `task_service.py` 任务级 |
| HTTP worker 并发 | asyncio.Semaphore(max_conn) | asyncio.TaskGroup + 每 subworker 一个 task |
| BT peer 并发 | ConnectionPool(Semaphore) + 🆕 流水线 | libtorrent 内核 |
| 速度限制 | ❌ 无 | `speed_meter.py` + `waitForSpeedLimit` |
| 任务计划 | ❌ 无 | `plan.py`（完成后关机/重启/睡眠/打开文件） |

---

## 7. 断点续传与任务可编辑性

| 维度 | Draco | Ghost |
|------|-------|-------|
| HTTP 断点 | `.progress` 布尔数组 / 🆕 每分片已写字节 | `.ghd` 二进制（start/position/end × 24B） |
| BT 断点 | `.progress` | libtorrent resume data（含 piece bitmap） |
| 暂停后改链接 | ❌ | ✅ `canReuseProgress`（同大小可复用） |
| 暂停后改请求头 | ❌ | ✅ `setOptions` |
| 暂停后改代理 | ❌ | ✅ `_onProxyChanged` 运行时切代理 |
| 暂停后改 subworker 数 | ❌ | ✅ `subworkerCount` 可调 |

**学到的思路**：Ghost 的"任务可编辑性"是 GUI 应用的强需求，Draco 作为 Agent 库可通过
TaskStep 管线的 `retry_step` + handle 字段扩展实现类似能力（见改进建议 I2）。

---

## 8. M3U8/HLS 对比

| 维度 | Draco | Ghost |
|------|-------|-------|
| 主/子清单 | ✅ | ✅ |
| AES-128 解密 | ✅ pycryptodome | ✅ |
| 多分片并发 | ✅ | ✅ |
| 直播录制 | ❌ | ✅ 边下边实时解密 |
| Android 全链路 | ❌ | ✅ |
| FFmpeg 合并 | ❌ 需用户自带 | ✅ `ffmpeg_pack` + 最小 LGPL 自建 |

---

## 9. 可观测性 / Agent 友好性（Draco 强项）

| 维度 | Draco | Ghost |
|------|-------|-------|
| 任务步骤预览 | ✅ `StepPipeline.describe()` | ❌ 无 |
| 单步重试 | ✅ `retry_step(name)` | ❌ |
| 稳定错误码 | ✅ `errors.py`（如 `draco.http_status`） | ✅ `error_catalog.py`（按 pack 分） |
| 流式进度迭代 | ✅ `download_stream` async generator | ❌ Qt 信号 |
| 预分析模式 | ✅ `--dry-run --optimize` | ❌ |
| 镜像选择器独立用 | ✅ `MirrorSelector` | ❌ |

**Draco 的差异化优势**：TaskStep 管线让 AI Agent 能预览下载会经过哪些阶段、
在任意阶段介入、针对单步失败精细重试——这是 GUI 应用不需要、但 Agent 强需要的能力。

---

## 10. 跨平台 / UI / 扩展 / 移动端

| 维度 | Draco | Ghost |
|------|-------|-------|
| GUI | ❌ 纯库 | ✅ PySide6 Fluent Design |
| 浏览器扩展 | ❌ | ✅ TS+React 嗅探媒体资源 |
| Android | ❌ | ✅ 完整移动端 + 后台下载 + 通知 |
| 托盘最小化 | ❌ | ✅ 释放 UI 资源，后台低内存 |
| 系统集成 | ❌ | ✅ 文件关联/URL scheme/开机自启/通知 |
| 国际化 | ❌ | ✅ Crowdin 7 语言 |

**结论**：这些对 Draco（Agent 库）多数不适用，但浏览器扩展嗅探思路可启发 Draco
未来提供"URL → 资源清单"的 Agent 友好 API。

---

## 11. 依赖与体积对比

| 依赖 | 许可证 | Draco | Ghost | 用途 |
|------|--------|-------|-------|------|
| aiohttp | Apache 2.0 | ✅ | ❌ | HTTP |
| aioftp | Apache 2.0 | ✅ | ✅ | FTP |
| pycryptodome | BSD-2 | ✅可选 | ❌ | M3U8 解密 |
| niquests/wreqs | MIT | ❌ | ✅ | HTTP/2&3 + TLS 指纹 |
| libtorrent | BSD-2 | ❌ | ✅ | BT（C++ 编译，体积大） |
| PySide6 | LGPL-3.0 | ❌ | ✅ | GUI |
| uvloop/winloop | MIT | ❌ | ✅ | 事件循环加速 |
| loguru | MIT | ❌ | ✅ | 日志 |
| ffmpeg | LGPL | ❌ | ✅自建 | M3U8 合并 |

**Draco 体积**：仅 aiohttp + aioftp，纯 Python 轮子，pip install 秒装。
**Ghost 体积**：需编译 libtorrent + PySide6 + ffmpeg，打包后 100MB+。

---

## 12. 亮点提炼（Ghost 值得学的思路）

| # | 亮点 | 适用于 Agent 库？ | 落地难度 | 许可证风险 | Draco 落地状态 |
|---|------|------------------|---------|-----------|---------------|
| L1 | 免合并定位写（pwrite + ftruncate） | ✅ 高价值 | 中 | 无 | ✅ **已落地** [http.py](file:///workspace/protocols/http.py) `_download_adaptive` |
| L2 | 运行时分裂最慢分片 | ✅ 高价值 | 中 | 无 | ✅ **已落地** [optimizer.py](file:///workspace/optimizer.py) `AdaptiveSpeedupController` |
| L3 | 速度采样 + 收益判定反馈环 | ✅ 高价值 | 低 | 无 | ✅ **已落地** 同上 |
| L4 | 历史带宽记忆（按域名） | ✅ 中价值 | 低 | 无 | ✅ **已落地** `record_actual` / `profile_for` |
| L5 | BT 流水线请求 | ✅ 高价值 | 中 | 无 | ✅ **已落地** [downloader.py](file:///workspace/bittorrent/downloader.py) `_request_piece` |
| L6 | BT endgame 模式 | ✅ 高价值 | 中 | 无 | ✅ **已落地** `_is_endgame` + `_select_rarest_piece` |
| L7 | 任务暂停改链接/头/代理续传 | 中（Agent 场景） | 中 | 无 | ⏳ 建议（见 I2） |
| L8 | TLS 指纹模拟 | 中（反封锁场景） | 高 | 无（curl_cffi MIT） | ⏳ 建议（见 I3） |
| L9 | M3U8 直播录制 + 边下边解密 | 中 | 高 | 无 | ⏳ 建议（见 I4） |
| L10 | DHT 状态持久化 | ✅ 中价值 | 中 | 无 | ⏳ 建议（见 I8） |
| L11 | BT 文件优先级（多文件选择下载） | ✅ 中价值 | 中 | 无 | ⏳ 建议（见 I8） |
| L12 | UPnP/NAT-PMP 端口映射 | 低（Agent 库） | 高 | 无 | ⏳ 低优先 |
| L13 | ADR 架构决策记录 | ✅ 高价值 | 低 | 无 | ⏳ 建议（见 I10） |
| L14 | pack 插件化架构 | 中 | 高 | 无 | ⏳ 建议（见 I6） |
| L15 | 浏览器扩展嗅探 | 低（非库职责） | 高 | 无 | ❌ 不适用 |

---

## 13. Draco 已有优势（不妄自菲薄）

1. **纯自研 BT 协议栈**：Bencode/DHT/Peer Wire 全自研，零 GPL，Agent 可审计。
2. **TaskStep 管线**：[steps.py](file:///workspace/steps.py) 让 Agent 预览/介入/单步重试，Ghost 无此能力。
3. **集中化错误目录**：[errors.py](file:///workspace/errors.py) 稳定错误码 + retryable，Agent 可程序化处理。
4. **镜像选择器**：[mirror_selector.py](file:///workspace/mirror_selector.py) 12+ 节点 40/40/20 评分，Ghost 无。
5. **三法中位数优化器**：BDP + 目标分片 + 带宽并发，科学计算并发度。
6. **零 GPL 依赖**：MIT 宽松，下游 Agent 项目可闭源商用。
7. **pip install 即用**：纯 Python 轮子，无需编译 C++。
8. **流式进度迭代**：`download_stream` async generator，Agent 友好。

---

## 14. 总结

Draco 与 Ghost 是**不同定位的互补关系**：
- Ghost 是面向终端用户的 GUI 应用，功能全（含 eD2k/浏览器扩展/移动端），但 GPL + 重依赖。
- Draco 是面向 AI Agent 的纯库，零 GPL + 轻量，可观测性强（TaskStep/错误码/流式迭代）。

本次学习已将 Ghost 最有价值的 6 项**纯算法思路**（L1-L6）用原创代码落地到 Draco，
不引入任何 GPL 依赖，不抄袭 Ghost 代码结构/变量名。剩余 9 项（L7-L15）作为后续改进建议。
