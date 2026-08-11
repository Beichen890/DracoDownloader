# 计划：克隆 Ghost-Downloader-3 学习并与 DracoDownloader 全维度对比

## Summary（摘要）

DracoDownloader 是 MIT 许可、定位"零 GPL 依赖、AI Agent 原生"的纯 Python 多协议下载库；
Ghost-Downloader-3（XiaoYouChR，GPL-3.0）是 Python+PySide6 的跨平台 GUI 下载器，含 IDM 式免合并分块、AI 智能加速、TLS 指纹模拟、M3U8 边下边解密、浏览器扩展、Android 端等亮点。

本计划目标：
1. 把 Ghost-Downloader-3 克隆到 `/workspace/.reference/Ghost-Downloader-3`（隔离 + gitignore，**绝不进入 DracoDownloader 仓库历史**）。
2. 只读学习其架构与亮点实现思路（**clean-room，不抄 GPL 代码**）。
3. 产出两份思路级文档：全维度对比报告 + 可执行改进建议清单。

产出物严格为"思路/方法/架构对比"，不含任何 GPL 代码片段；改进建议若日后落地，须用原创代码独立实现。

---

## Phase 1 探索结论（Current State Analysis）

### DracoDownloader（当前项目，MIT）已确认的关键事实

来源：`README.md`、`pyproject.toml`、`LICENSE`、`core.py`、`engine.py`、`steps.py`、`protocols/base.py`、`protocols/http.py`、`optimizer.py`、`.gitignore`。

- **定位**：`pyproject.toml` description = "Pure Python multi-protocol download engine for AI Agents — zero GPL dependencies"；README 明确"零 GPL 依赖"为核心卖点。
- **许可**：MIT（`LICENSE`，Copyright 2025 Beichen890）。
- **依赖**：仅 `aiohttp>=3.9,<4`、`aioftp>=0.20,<0.21`；可选 `pycryptodome`（M3U8 AES-128）。**无 niquests、无 libtorrent、无 PySide6、无 uvloop**。
- **协议栈**：
  - HTTP/HTTPS：`protocols/http.py`，异步多分片，含 `_SpeedTracker`（滑动窗口 2s）、`_ProgressReporter`（线程安全汇总、50ms 节流）、合并缓冲 16MiB、分片重试退避、`_DEFAULT_MAX_CONNECTIONS=64`。
  - FTP/FTPS：`protocols/ftp.py`（aioftp，被动模式 + REST 续传）。
  - M3U8/HLS：`protocols/m3u8.py`（主/子清单、AES-128 解密、多分片并发）。
  - BitTorrent：`bittorrent/` **纯自研**——`bencode.py`/`dht.py`(Kademlia)/`peer.py`(Peer Wire BEP3/6/10)/`magnet.py`(BEP9)/`downloader.py`(稀缺分片优先)/`loaders.py`/`trackers.py`/`seeding.py`。
- **核心抽象**：`protocols/base.py` 定义 `ProtocolDriver`(match/probe/download/resume) + `DownloadHandle`；`protocols/__init__.py` 的 `ProtocolRouter` 路由。
- **调度与生命周期**：`scheduler.py`（队列/并发/超时/重试/取消广播）、`engine.py`（active/paused 字典式生命周期，较薄）、`progress.py`（.progress 断点持久化）。
- **优化器**：`optimizer.py`——`NetworkProfile` + `OptimalParams` + `BandwidthProbe`；三法取中位数（目标分片大小 / BDP / 带宽并发限制）；线程数综合 CPU 核数×4、延迟、带宽。
- **镜像选择器**：`mirror_selector.py`——评分 40%延迟+40%带宽+20%DNS，内置 8 CN + 4 global 镜像。
- **TaskStep 管线**：`steps.py`——`StepStatus`/`TaskStep`/`StepPipeline`/`build_standard_pipeline`，标准步骤 `probe→download→merge→verify→seed`，支持 `describe()` 预览、`retry_step()` 单步重试、`on_step_failed` 回调。**这是 Draco 的差异化亮点**。
- **错误目录**：`errors.py`（`DracoError` + 稳定错误码 + retryable 标记，README 提到如 `draco.http_status`）。
- **CLI**：`cli.py`，支持 `--dry-run/--optimize/--no-optimize/--mirror/--verify/--list-protocols`。
- **没有的东西**：无 GUI、无浏览器扩展、无 Android、无 TLS 指纹模拟、无 HTTP/2&HTTP/3、无插件系统、无 M3U8 直播录制、无 eD2k。
- **.gitignore 现状**：未忽略 `.reference/`，需新增。

### Ghost-Downloader-3（学习对象，GPL-3.0）已确认的关键事实

来源：GitHub 仓库主页、README_zh.md、CSDN 深度解析、tev6 fork。

- **许可**：GPL-3.0（**强传染性**，任何代码引用都会迫使 DracoDownloader 变为 GPL，**绝对禁止抄代码**）。
- **技术栈**：PySide6(Fluent Design) + niquests(HTTP/1.1&2&3 + TLS 指纹) + libtorrent(BT) + aioftp + uvloop/winloop。
- **架构**：`app/`(服务层，如 `app/services/core_service.py` 含 AI 加速)、`features/`(按协议分特性模块)、`browser_extension/`、`android/`、`docs/adr/`(架构决策记录)、`scripts/`、`tests/`。
- **亮点**：
  1. IDM 式智能分块**且无需合并文件**（预分配空间 + 各线程直接写对应区域）。
  2. AI 智能加速（带宽预测模型 / 连接数自适应 / 分块大小优化）。
  3. 模拟真实浏览器 TLS 指纹（niquests），降低被封锁概率。
  4. M3U8 直播录制 + 边下边实时解密（Android 全链路支持）。
  5. 任务暂停后可改链接/请求头/代理再续，进度不丢。
  6. 配套浏览器扩展嗅探网页媒体资源。
  7. 最小化到托盘自动释放 UI 资源，后台内存极低。
  8. 完整 Android 移动端界面 + 后台下载 + 完成通知。
  9. 跨平台矩阵：Linux glibc2.35+ / Win 7 SP1+ / macOS 13+ / Android 9+，x86_64 & arm64。
  10. 计划中的插件 API、eD2k、Native 下载引擎。
- **BT 实现**：基于 libtorrent（C++ 库，BSD/MIT 但通过 Python 绑定使用），**与 Draco 纯自研路线根本不同**——这是对比重点。

---

## Phase 2 澄清结论（用户已确认）

| 问题 | 用户选择 |
|------|---------|
| 克隆位置 | `/workspace/.reference/Ghost-Downloader-3`（隐藏目录 + gitignore） |
| 产出形式 | 对比报告 + 改进建议清单（思路级，不含 GPL 代码） |
| 对比深度 | 全维度详细对比 |

---

## Proposed Changes（执行步骤）

### 步骤 1：隔离 GPL 代码——更新 .gitignore

**文件**：`/workspace/.gitignore`
**改动**：在 `# Project` 段追加 `.reference/`，确保克隆下来的 GPL 代码永远不会被 `git add` 进 DracoDownloader 历史。
**为什么**：MIT 项目一旦混入 GPL 代码提交，会引发许可证污染；这是整个学习任务的合规底线。
**怎么做**：在文件末尾 `# Project` 段下增加一行 `.reference/`。

### 步骤 2：克隆 Ghost-Downloader-3 到隔离目录

**命令**：`git clone --depth 1 https://github.com/XiaoYouChR/Ghost-Downloader-3.git /workspace/.reference/Ghost-Downloader-3`
**为什么**：`--depth 1` 只拉最新快照，体积小、足够学习；放在 `.reference/` 与 gitignore 双保险。
**验证**：克隆后 `LS /workspace/.reference/Ghost-Downloader-3` 确认 `app/`、`features/`、`docs/adr/`、`README*.md` 存在。

### 步骤 3：只读精读 Ghost-Downloader-3 关键文件（学习，不抄）

按维度对照阅读以下文件（路径基于克隆后的 `/workspace/.reference/Ghost-Downloader-3/`）：

1. **架构与设计决策**：`README.md`、`README_zh.md`、`docs/adr/`（架构决策记录，理解为什么这么设计）、`Ghost-Downloader-3.py`（入口）。
2. **AI 加速核心**：`app/services/core_service.py`（带宽预测 / 连接自适应 / 分块优化算法）。
3. **免合并分块**：在 `features/` 与 `app/services/` 下定位 HTTP 分块下载实现，看"预分配 + 各线程直接写偏移"如何做到零合并。
4. **TLS 指纹**：niquests 的使用方式与配置点（仅学思路，Draco 用 aiohttp，能否/是否值得引入需评估）。
5. **M3U8 直播 + 边下边解密**：`features/` 下 m3u8 模块，看直播录制流水线与实时解密如何串接。
6. **BT 桥接 libtorrent**：与 Draco 纯自研 BT 栈对比工程取舍（功能 vs 体积 vs 许可）。
7. **任务暂停改链接续传**：session 持久化与断点模型设计。
8. **插件/Step 架构**：与 Draco `steps.py` 的 TaskStep 管线对照，看可观测性思路差异。
9. **浏览器扩展**：`browser_extension/` 嗅探与 WebSocket 通信思路（Draco 是 Agent 库，不一定需要，但值得记录）。
10. **跨平台/Android/托盘**：`android/`、`scripts/`，看移动端与后台低内存方案（Draco 无 GUI，多数不适用，仅记录）。

**合规约束**：
- 只读、只理解、只做笔记；**禁止复制任何代码块到 DracoDownloader 源码或产出文档**。
- 产出文档中若需引用 GhostDownloader 的设计，只描述"它做了什么/怎么组织的"，不贴其源码。
- 引用 DracoDownloader 自身代码时，用 `file:///` 链接指向实际文件，可贴 Draco 自己的 MIT 代码。

### 步骤 4：产出对比报告

**文件**：`/workspace/docs/comparison/GhostDownloader_vs_DracoDownloader.md`
**为什么放这里**：`pyproject.toml` 的 `setuptools.exclude-package-data` 已排除 `docs*`，不会打进 PyPI wheel；放 `docs/comparison/` 语义清晰。
**内容结构**（全维度）：

1. **项目定位与许可证对比**（MIT vs GPL，Agent 库 vs GUI 应用，零 GPL 依赖红线）。
2. **整体架构对比**（Draco 分层：core/scheduler/engine/router/optimizer/mirror/steps/errors；Ghost：app/services + features + GUI + 扩展 + 移动端）。
3. **协议栈对比表**（HTTP/FTP/M3U8/BT/磁力/eD2k，逐项：实现方式、依赖、许可证影响）。
4. **HTTP 分块策略对比**（Draco：多分片 + 合并缓冲 16MiB + 退避重试；Ghost：IDM 式预分配 + 直接写偏移 + 免合并）—— 重点分析免合并的可借鉴思路与 Draco 落地代价。
5. **并发与调度对比**（Draco `scheduler.py` 队列/超时/重试/取消广播 vs Ghost 的 session/并发模型）。
6. **优化/AI 加速对比**（Draco `optimizer.py` 三法中位数 + BDP + `BandwidthProbe`；Ghost `core_service.py` AI 加速）—— 对比"规则化优化"与"AI 自适应"的工程取舍。
7. **镜像选择对比**（Draco 12+ 镜像 40/40/20 评分；Ghost 是否有同类机制）。
8. **断点续传与任务可编辑性对比**（Draco `.progress` + TaskStep 单步重试；Ghost 暂停改链接/头/代理续传）。
9. **M3U8/HLS 对比**（Draco AES-128 解密 + 多分片；Ghost 直播录制 + 边下边解密 + Android 全链路）。
10. **BitTorrent 对比**（Draco 纯自研 BEP3/5/6/9/10 + DHT + 稀缺优先 + 做种；Ghost libtorrent 桥接）—— 工程取舍重点章节。
11. **TLS 指纹 / HTTP2&3 对比**（Draco aiohttp 无；Ghost niquests 有）—— 评估对 Agent 场景的必要性。
12. **可观测性 / Agent 友好性对比**（Draco TaskStep 管线 + 错误目录是强项；Ghost 偏 GUI 用户视角）。
13. **跨平台 / UI / 扩展 / 移动端对比**（Draco 无；Ghost 全有）—— 标注对 Draco 是否适用。
14. **依赖与体积对比表**（依赖清单 + 许可证 + 安装体积影响）。
15. **亮点提炼**（Ghost 值得学的 N 个思路，每个注明：是否适用于 Agent 库场景 / 落地难度 / 许可证风险）。
16. **Draco 已有优势**（自研 BT、TaskStep、镜像选择、零 GPL、Agent 原生 API）—— 不妄自菲薄。

### 步骤 5：产出改进建议清单

**文件**：`/workspace/docs/comparison/improvement_ideas.md`
**内容结构**：每条建议含 `编号 / 来源亮点 / 思路描述(无代码) / 对 Agent 场景的价值 / 落地难度 / 许可证风险 / 建议优先级 / 建议的 Draco 落地点(文件级)`。
**候选建议**（最终以阅读后为准，此处为预判方向）：
- I1：免合并分块——预分配 + 分片直接写偏移（落地于 `protocols/http.py`，需评估 aiohttp 下实现路径）。
- I2：任务暂停改链接/头/代理续传（落地于 `scheduler.py`/`progress.py`/`DownloadHandle`）。
- I3：HTTP/2 与 TLS 指纹——评估引入 `niquests` 或 `curl_cffi`（注意许可证：curl_cffi 为 MIT，niquests 为 MIT，均非 GPL，但需重测依赖体积）。
- I4：M3U8 直播录制 + 边下边解密流式输出（落地于 `protocols/m3u8.py`）。
- I5：AI 加速——在 `optimizer.py` 现有规则化基础上加自适应反馈环（用历史下载速率修正 `NetworkProfile`）。
- I6：插件/扩展点 API——参考 Ghost 计划中的插件思路，给 Draco 设计 Agent 可注册的 hook（落地于 `protocols/base.py` 或新增 `plugins.py`）。
- I7：HTTP/3 支持评估（aiohttp 不支持，需换客户端，权衡）。
- I8：BT 增强——自研栈对标 libtorrent 功能差距（如 UDP tracker、PEX、ut_metadata、超级做种）。
- I9：跨平台二进制分发评估（Draco 是库不需要 GUI，可能不适用，记录取舍）。
- I10：架构决策记录（ADR）——借鉴 Ghost `docs/adr/` 做法，给 Draco 关键决策留 ADR。

**合规底线**：所有建议均为"思路/方向"，不含 Ghost 代码；后续若实现，须原创编写并独立验证。

---

## Assumptions & Decisions（假设与决策）

1. **假设**：`https://github.com/XiaoYouChR/Ghost-Downloader-3.git` 可在沙箱内 `git clone`（已联网验证仓库存在）。若 clone 失败，回退方案：用 `WebFetch` 拉取 GitHub raw 关键文件 + 仓库 API 列目录，仍能完成对比（但深度受限）。
2. **决策**：用 `--depth 1` 克隆，不拉全量历史，减少体积与 GPL 代码留存面。
3. **决策**：GPL 代码只放在 `.reference/` 且 gitignore，**绝不**复制到 `protocols/`、`bittorrent/`、`docs/` 等 Draco 源码区。
4. **决策**：产出文档只描述 Ghost 的"设计与方法"，不引用其源码片段；引用 Draco 自身代码时用 `file:///` 链接（符合 Code Reference 规范）。
5. **决策**：不改 DracoDownloader 任何业务代码；本任务交付物仅为两份 md 文档 + `.gitignore` 一行新增。
6. **假设**：`/workspace/docs/comparison/` 目录不存在，需创建（Write 工具会自动建父目录）。
7. **决策**：对比报告语言为中文（与用户最新消息一致）。

---

## Verification（验证步骤）

1. **隔离验证**：`git -C /workspace status` 确认 `.reference/` 不出现在 untracked 列表（gitignore 生效）。
2. **克隆验证**：`LS /workspace/.reference/Ghost-Downloader-3` 确认 `app/`、`features/`、`docs/adr/`、`README*.md` 存在。
3. **产出验证**：
   - `Read /workspace/docs/comparison/GhostDownloader_vs_DracoDownloader.md` 确认 16 个章节齐全。
   - `Read /workspace/docs/comparison/improvement_ideas.md` 确认每条建议 8 字段齐全。
4. **合规验证**：`Grep` 在 `/workspace/docs/comparison/` 与 `/workspace/protocols|bittorrent|core.py|engine.py` 中搜索是否有从 Ghost 复制的特征代码（如 niquests/libtorrent import、Fluent Design 类名）——应只在对比报告的"描述"中出现，不在 Draco 源码出现。
5. **链接验证**：产出文档中所有指向 Draco 代码的 `file:///` 链接路径真实存在。
6. **无副作用验证**：`git -C /workspace diff --stat` 应只显示 `.gitignore` 一处改动 + 两个新 md 文件 + `.trae/documents/` 计划文件。
