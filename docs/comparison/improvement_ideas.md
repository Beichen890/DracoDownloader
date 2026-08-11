# DracoDownloader 改进建议清单

> 基于 Ghost-Downloader-3 学习的思路级建议（不含任何 GPL 代码）。
> 每条建议均为"思路/方向"，后续实现须原创编写并独立验证。
> 已落地的 6 项（I1-I6）标注 ✅，未落地的（I7-I15）标注 ⏳。

---

## ✅ 已落地（本次完成）

### I1：免合并定位写（HTTP）
- **来源亮点**：Ghost 用 `pwrite(fd, chunk, position)` + `ftruncate` 预分配，各 worker 直接写最终文件偏移，零合并。
- **思路描述**：下载前 `os.ftruncate` 预分配目标文件；每个分片 worker 用 `os.pwrite` 直接写对应偏移；省去"分片落临时文件 → 合并"的 IO。
- **对 Agent 场景的价值**：大文件下载省去合并时间（4GB 视频可省数十秒），减少磁盘占用（无临时分片文件）。
- **落地难度**：中（需处理断点续传格式变化）。
- **许可证风险**：无（`os.pwrite`/`os.ftruncate` 是 POSIX 标准接口）。
- **Draco 落地点**：[protocols/http.py](file:///workspace/protocols/http.py) `_download_adaptive` / `_adaptive_worker`。
- **状态**：✅ 已落地，64 测试通过。

### I2：运行时自适应加速控制器
- **来源亮点**：Ghost `_autoSpeedUp` 速度采样 → 稳定则分裂最慢分片 → 5 秒后对比 worker 增比 vs 速度增比决定是否继续。
- **思路描述**：独立 `AdaptiveSpeedupController` 类，喂速度采样；速度稳定（偏差 ≤15%）时首次触发，记录基线并分裂若干最慢分片；观察期后对比增比，收益不足则停止。
- **对 Agent 场景的价值**：自动适应当前网络，无需 Agent 手动调参；慢分片动态分裂提升整体吞吐。
- **落地难度**：中。
- **许可证风险**：无。
- **Draco 落地点**：[optimizer.py](file:///workspace/optimizer.py) `AdaptiveSpeedupController`。
- **状态**：✅ 已落地，冒烟测试通过。

### I3：历史带宽记忆（跨任务反馈）
- **来源亮点**：Ghost 无此能力（每次重新探测）。
- **思路描述**：下载完成后 `record_actual` 按域名记忆实际带宽，指数加权融合（0.6 新 + 0.4 旧）；下次同站下载 `profile_for` 直接取历史画像，减少重复探测。
- **对 Agent 场景的价值**：Agent 批量下载同站资源时，后续任务跳过探测，启动更快。
- **落地难度**：低。
- **许可证风险**：无。
- **Draco 落地点**：[optimizer.py](file:///workspace/optimizer.py) `DownloadOptimizer.record_actual` / `profile_for`。
- **状态**：✅ 已落地。

### I4：BT 流水线请求
- **来源亮点**：libtorrent 内置流水线；Ghost Python 层无此逻辑（靠 C++ 内核）。
- **思路描述**：`_request_piece` 保持 `MAX_PIPELINED_REQUESTS`(=5) 个 in-flight 请求，而非"发一个等一个"，提升单 peer 吞吐。
- **对 Agent 场景的价值**：BT 下载吞吐显著提升（单 peer 从串行变并行）。
- **落地难度**：中（需正确处理 in-flight 计数与 cancel）。
- **许可证风险**：无。
- **Draco 落地点**：[bittorrent/downloader.py](file:///workspace/bittorrent/downloader.py) `_request_piece`。
- **状态**：✅ 已落地。

### I5：BT endgame 模式
- **来源亮点**：libtorrent 内置 endgame；Ghost Python 层无此逻辑。
- **思路描述**：剩余 piece 数 ≤ `ENDGAME_THRESHOLD`(=5) 时进入 endgame，允许对 in-progress piece 向多个 peer 同时发请求，先到先用，避免慢 peer 拖住收尾。
- **对 Agent 场景的价值**：BT 下载收尾阶段从"串行等最慢 peer"变"并发抢"，显著缩短尾部延迟。
- **落地难度**：中。
- **许可证风险**：无。
- **Draco 落地点**：[bittorrent/downloader.py](file:///workspace/bittorrent/downloader.py) `_is_endgame` + `_select_rarest_piece` 的 `allow_in_progress` 分支。
- **状态**：✅ 已落地。

### I6：自适应下载回退机制
- **来源亮点**：Ghost 在 Range 不支持时降级单流。
- **思路描述**：自适应路径失败时回退到原"分片落盘 + 合并"路径，保证兼容性。
- **对 Agent 场景的价值**：新路径有 bug 时不影响主流程，渐进迁移。
- **落地难度**：低。
- **许可证风险**：无。
- **Draco 落地点**：[protocols/http.py](file:///workspace/protocols/http.py) `download()` 的 try/except 回退。
- **状态**：✅ 已落地。

---

## ⏳ 未落地（后续建议）

### I7：任务暂停改链接/头/代理续传
- **来源亮点**：Ghost `canReuseProgress`（同大小可复用进度）+ `setOptions`（改 headers/userAgent/subworkerCount）。
- **思路描述**：扩展 `DownloadHandle`，支持暂停后修改 url/headers/proxy 再 resume；同大小文件复用已下载进度。
- **对 Agent 场景的价值**：Agent 下载失败时可切换镜像/代理续传，不丢进度。
- **落地难度**：中。
- **许可证风险**：无。
- **建议优先级**：高。
- **建议落地点**：`protocols/base.py` `DownloadHandle` + `core.py` `download_async`。

### I8：BT 增强（DHT 持久化 + 文件优先级 + 代理）
- **来源亮点**：Ghost libtorrent 的 `save_dht_state`/`read_session_params`、`file_priorities`/`prioritize_files`、proxy 设置。
- **思路描述**：
  - DHT 路由表序列化到磁盘，二次启动更快发现 peer；
  - 多文件种子支持按文件选择下载（priority 0-7）；
  - peer 连接支持 SOCKS5 代理。
- **对 Agent 场景的价值**：BT 下载更灵活（选文件）、更稳（代理）、更快（DHT 持久化）。
- **落地难度**：高（DHT 序列化 + peer 代理需改 peer.py 的 `asyncio.open_connection`）。
- **许可证风险**：无。
- **建议优先级**：中。
- **建议落地点**：`bittorrent/dht.py`、`bittorrent/downloader.py`、`bittorrent/peer.py`。

### I9：TLS 指纹模拟（HTTP/2&3）
- **来源亮点**：Ghost 用 niquests 模拟真实浏览器 TLS 指纹 + 支持 HTTP/2&3。
- **思路描述**：评估引入 `curl_cffi`（MIT）或 `niquests`（MIT）作为可选 HTTP 后端，针对反爬站点用浏览器指纹。
- **对 Agent 场景的价值**：下载被 Cloudflare 等防护的资源时不易被拦。
- **落地难度**：高（需抽象 HTTP 后端接口，aiohttp/curl_cffi 双实现）。
- **许可证风险**：无（curl_cffi/niquests 均 MIT，非 GPL）。
- **建议优先级**：中。
- **建议落地点**：`protocols/http.py` 新增 backend 抽象 + 可选依赖。

### I10：M3U8 直播录制 + 边下边解密
- **来源亮点**：Ghost `m3u8_pack` 支持直播录制，边下边实时解密，Android 全链路。
- **思路描述**：扩展 `protocols/m3u8.py`，支持直播流（无结束的 playlist）持续录制；解密与下载流水线化。
- **对 Agent 场景的价值**：Agent 可录制直播流。
- **落地难度**：高。
- **许可证风险**：无。
- **建议优先级**：低（Agent 场景较少）。
- **建议落地点**：`protocols/m3u8.py`。

### I11：ADR 架构决策记录
- **来源亮点**：Ghost `docs/adr/0001-0008`，每个关键决策一个 md，含上下文/决策/后果。
- **思路描述**：为 Draco 关键决策（如"为何自研 BT 而非用 libtorrent"、"为何 aiohttp 而非 niquests"）写 ADR。
- **对 Agent 场景的价值**：提升项目可维护性，便于贡献者理解决策。
- **落地难度**：低。
- **许可证风险**：无。
- **建议优先级**：中。
- **建议落地点**：`docs/adr/`。

### I12：pack 插件化架构
- **来源亮点**：Ghost `features/*_pack/` + `manifest.toml` + `pack_loader` 动态加载。
- **思路描述**：把 `protocols/` 改为可插拔 pack，第三方可注册自定义协议驱动。
- **对 Agent 场景的价值**：Agent 可按需扩展协议。
- **落地难度**：高（需重构 ProtocolRouter 为插件注册中心）。
- **许可证风险**：无。
- **建议优先级**：低。
- **建议落地点**：`protocols/__init__.py`。

### I13：速度限制
- **来源亮点**：Ghost `speed_meter.py` + `waitForSpeedLimit`。
- **思路描述**：HTTPDriver 加 `max_speed_bps` 参数，超速时 await sleep。
- **对 Agent 场景的价值**：Agent 可控速避免占满带宽。
- **落地难度**：低。
- **许可证风险**：无。
- **建议优先级**：中。
- **建议落地点**：`protocols/http.py` + `core.py`。

### I14：任务计划（完成后动作）
- **来源亮点**：Ghost `plan.py`（完成后关机/重启/睡眠/打开文件）。
- **思路描述**：Draco 作为库，可暴露 `on_complete` 回调，由 Agent 决定后续动作（而非库内做系统操作）。
- **对 Agent 场景的价值**：低（Agent 自己能做）。
- **落地难度**：低。
- **许可证风险**：无。
- **建议优先级**：低。
- **建议落地点**：已由 TaskStep 管线的步骤回调覆盖，无需额外实现。

### I15：HTTP/3 支持
- **来源亮点**：Ghost niquests 支持 HTTP/3。
- **思路描述**：aiohttp 不支持 HTTP/3，需换后端（见 I9）。
- **对 Agent 场景的价值**：弱网下 HTTP/3 更稳。
- **落地难度**：高（与 I9 合并）。
- **许可证风险**：无。
- **建议优先级**：低。
- **建议落地点**：同 I9。

---

## 优先级总览

| 优先级 | 建议编号 | 主题 |
|--------|---------|------|
| **已落地** | I1-I6 | 免合并/自适应加速/历史记忆/BT流水线/endgame/回退 |
| **高** | I7 | 任务暂停改链接续传 |
| **中** | I8, I9, I11, I13 | BT增强/TLS指纹/ADR/速度限制 |
| **低** | I10, I12, I14, I15 | 直播录制/插件化/任务计划/HTTP3 |

---

## 合规声明

本清单所有建议均为思路级，不含 Ghost 任何源码。
已落地的 I1-I6 用 Draco 原创 snake_case 代码实现，未复制 Ghost 的 camelCase 结构/变量名/实现细节。
