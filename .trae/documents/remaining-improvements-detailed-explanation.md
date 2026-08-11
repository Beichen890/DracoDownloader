# 剩余建议（I7-I15）详细解释与落地方案

> 本文是对 `docs/comparison/improvement_ideas.md` 中 9 条未落地建议的详细展开。
> 已落地 I1-I6 见原清单，本文聚焦：每条建议的「为什么」「怎么改」「改哪里」「风险/工作量」「验收标准」。
> 本文为 **规划/解释文档**，不含代码实现；落地时再按本方案原创编写。

---

## 总览

| 编号 | 主题 | 优先级 | 落地难度 | 工作量估计 | 涉及文件数 |
|------|------|--------|---------|-----------|-----------|
| I7 | 任务暂停改链接/头/代理续传 | 高 | 中 | 中 | 3 |
| I8 | BT 增强（DHT 持久化 + 文件优先级 + 代理） | 中 | 高 | 大 | 4 |
| I9 | TLS 指纹模拟（HTTP/2&3 后端抽象） | 中 | 高 | 大 | 2 |
| I10 | M3U8 直播录制 + 边下边解密 | 低 | 高 | 大 | 1 |
| I11 | ADR 架构决策记录 | 中 | 低 | 小 | 0（仅新增 md） |
| I12 | pack 插件化架构 | 低 | 高 | 大 | 1+ |
| I13 | 速度限制 | 中 | 低 | 小 | 2 |
| I14 | 任务计划（完成后动作） | 低 | 低 | 小 | 0（已覆盖） |
| I15 | HTTP/3 支持 | 低 | 高 | 大 | 同 I9 |

---

## I7：任务暂停改链接/头/代理续传（高优先级）

### 为什么
当前 [scheduler.py:265-284](file:///workspace/scheduler.py) 的 `pause`/`resume` 只切换任务状态（RUNNING→PAUSED→QUEUED），[core.py:436-439](file:///workspace/core.py) 的 `pause`/`resume` 透传到调度器。Agent 下载大文件失败时，只能取消重下；无法「换镜像/换代理/换 UA 续传」。

### 现状（探索结果）
- [protocols/base.py:14-28](file:///workspace/protocols/base.py) `DownloadHandle` 是 dataclass，含 `url/headers/proxy/paused/cancelled`，但无「修改后续传」接口。
- [protocols/http.py:663-666](file:///workspace/protocols/http.py) `resume` 直接调 `download`，靠 `.progress` 文件恢复，**但 url/headers/proxy 来自 handle，pause 期间无法改**。
- 自适应路径 [protocols/http.py:562-585](file:///workspace/protocols/http.py) 的 `_load_adaptive_progress` 已记录每分片已写字节，具备续传基础。

### 怎么改（落地方案）
1. **扩展 DownloadHandle**：新增 `update_for_resume(url=None, headers=None, proxy=None)` 方法，校验新 URL 的 `total_size` 必须与原 handle 一致（同大小才允许复用进度），不一致则抛 `ValueError`。
2. **核心层暴露接口**：`DracoDownloader` 新增 `pause_and_reconfigure(task_id, url=None, headers=None, proxy=None)`：
   - 调 `scheduler.pause(task_id)` 设置 PAUSED；
   - 通过 `scheduler.get_handle(task_id)` 拿到 handle（需在 scheduler 新增 `get_handle`）；
   - 调 `handle.update_for_resume(...)`；
   - 调 `scheduler.resume(task_id)`。
3. **HTTP 驱动支持续传时换 url**：`_download_adaptive` 和 `_download_chunk` 的 HTTP 请求已用 `handle.url`，resume 后自然用新 url；只需确保 `.progress` 文件名不绑定 url（当前绑定 `output_path`，✅ 已满足）。
4. **同大小校验**：resume 前对新 url 做一次 `probe`（HEAD），对比 `total_size`；不一致拒绝并提示。

### 改哪里
| 文件 | 改动 |
|------|------|
| [protocols/base.py](file:///workspace/protocols/base.py) | `DownloadHandle` 新增 `update_for_resume` 方法 + 大小校验 |
| [scheduler.py](file:///workspace/scheduler.py) | 新增 `get_handle(task_id)` 取 handle；`pause`/`resume` 已有 |
| [core.py](file:///workspace/core.py) | `DracoDownloader` 新增 `pause_and_reconfigure` 方法，串联 pause→改 handle→resume |

### 风险
- 新 url 若实际是不同文件（hash 不同），续传会写出坏文件 → 靠 size 校验兜底，强校验需下完算 hash（成本高，不做）。
- 自适应路径分裂产生的分片区间表保存在 `.progress` 的 `ranges` 字段，换 url 后区间不变，安全。

### 验收
- 单元测试：构造 task → pause → 改 url（同 size）→ resume → 文件完整且大小正确。
- 单元测试：改 url（不同 size）→ 抛 ValueError。
- 集成测试：用本地 server 起两个端口指向同文件，pause 后换端口 resume 成功。

---

## I8：BT 增强（DHT 持久化 + 文件优先级 + 代理）（中优先级）

### 为什么
当前 BT 实现是纯自研，[bittorrent/dht.py:541-547](file:///workspace/bittorrent/dht.py) `close` 只关 transport，**路由表不落盘**，每次启动重新 bootstrap；[bittorrent/downloader.py:72-83](file:///workspace/bittorrent/downloader.py) `TorrentMeta` 有 `is_multi_file`/`files` 但下载时**无文件优先级**（全量下载）；[bittorrent/peer.py:114-117](file:///workspace/peer.py) `asyncio.open_connection` 直连，**无代理支持**。

### 三部分

#### I8a：DHT 路由表持久化
- **怎么改**：`DHTClient` 新增 `save_state(path)` / `load_state(path)`，把 `routing_table.buckets` 序列化为紧凑格式（每个 Node 26 字节：id20+ip4+port2，复用现有 `encode_nodes_compact`）；`start()` 时若存在 state 文件则 `load_state` 后再 bootstrap（加速 peer 发现）。
- **改哪里**：[bittorrent/dht.py](file:///workspace/bittorrent/dht.py) `DHTClient` 新增两个方法 + `start` 调用 `load_state`；`close` 调 `save_state`。
- **文件路径**：默认 `~/.draco/dht_state.dat`（可配置）。

#### I8b：多文件种子文件优先级
- **怎么改**：`BTDownloader` 新增 `file_priorities: Dict[str, int]` 参数（0=不下载,1-7=优先级）；`_select_rarest_piece` 跳过 priority=0 文件覆盖的 piece；高优先级 piece 优先请求（支持边下边看）。
- **改哪里**：[bittorrent/downloader.py](file:///workspace/bittorrent/downloader.py) `BTDownloader.__init__` 加参数；`_select_rarest_piece` 加优先级过滤；`_build_pieces`（若有）建立 piece→file 映射。
- **现状**：`TorrentMeta.files` 已有 `[{path, length}]` 结构，piece→file 映射可按 piece 边界算出。

#### I8c：peer SOCKS5 代理
- **怎么改**：`PeerConnection.connect` 改用可选的 proxy；若指定 proxy 则用 `python-socks`（MIT）或自研 SOCKS5 握手建立隧道再交给 asyncio。
- **改哪里**：[bittorrent/peer.py:111-129](file:///workspace/bittorrent/peer.py) `connect` 方法；构造函数加 `proxy` 参数。
- **依赖**：`python-socks`（MIT，可选依赖）。

### 风险
- DHT state 文件可能含失效节点 → load 后靠 `fail_count` 自然淘汰，低风险。
- 文件优先级计算 piece→file 映射在 piece 跨文件边界时需仔细，建议参考 libtorrent 的 `file_view` 逻辑但原创实现。
- SOCKS5 代理引入新依赖，需在 setup.py 标记 optional。

### 验收
- I8a：启动两次，第二次 peer 发现速度 > 第一次（节点数更多更快）。
- I8b：多文件种子，设某文件 priority=0，下载完成后该文件不存在或不完整。
- I8c：通过本地 SOCKS5 代理（如 ssh -D）连接 peer 成功。

---

## I9：TLS 指纹模拟 / HTTP 后端抽象（中优先级）

### 为什么
当前 [protocols/http.py:6](file:///workspace/protocols/http.py) 用 `aiohttp`，TLS 指纹是 Python/aiohttp 默认的，易被 Cloudflare 等反爬识别；且 aiohttp 不支持 HTTP/2&3。

### 怎么改
1. **抽象 HTTPBackend 接口**：定义 `class HTTPBackend(ABC)` 含 `get(url, headers) -> ResponseContext`，ResponseContext 支持 `iter_chunked` + `status` + `headers`。
2. **双实现**：
   - `AiohttpBackend`（默认，现有行为）
   - `CurlCffiBackend`（可选，用 `curl_cffi` MIT，模拟 Chrome/Firefox 指纹）
3. **HTTPDriver 构造加 `backend` 参数**：默认 `aiohttp`，反爬站点传 `curl_cffi`。
4. **最小侵入**：现有 `_download_chunk`/`_download_adaptive` 改用 `self._backend.get(...)` 而非直接 `session.get(...)`。

### 改哪里
| 文件 | 改动 |
|------|------|
| [protocols/http.py](file:///workspace/protocols/http.py) | 新增 `HTTPBackend` 抽象 + 两个实现；`HTTPDriver.__init__` 加 `backend` 参数；下载方法改用 backend |
| setup.py / requirements | `curl_cffi` 标记为 optional extras |

### 风险
- `curl_cffi` 是同步库，需 `asyncio.to_thread` 包装或用其 async API（`curl_cffi.requests.AsyncSession`）。
- 双 backend 行为差异需测试覆盖（Range/header 处理）。
- 体积：curl_cffi 依赖 libcurl，~5MB。

### 验收
- 默认 backend 跑通现有 74 测试。
- curl_cffi backend 能下载 Cloudflare 防护的测试 URL（手动验证）。
- 切换 backend 不影响断点续传/自适应路径。

---

## I10：M3U8 直播录制 + 边下边解密（低优先级）

### 为什么
[protocols/m3u8.py:49](file:///workspace/protocols/m3u8.py) `probe` 已识别 `is_live`（`#EXT-X-ENDLIST` 不存在），但下载逻辑按「固定分片列表」处理，**不支持持续拉取新分片**；解密是下载完再解，非流水线。

### 怎么改
1. **直播模式**：`download` 中若 `is_live=True`，进入循环模式——每隔 N 秒重新拉 playlist，解析新出现的分片 URL，追加到下载队列，直到用户 cancel 或超时。
2. **边下边解密流水线**：用 `asyncio.Queue` 解耦「下载分片」和「解密+写文件」两个阶段，下载完一个分片入队，解密 worker 出队处理。
3. **录制控制**：暴露 `stop_after_duration` 或 `stop_after_segments` 参数。

### 改哪里
| 文件 | 改动 |
|------|------|
| [protocols/m3u8.py](file:///workspace/protocols/m3u8.py) | `download` 新增直播分支 + 流水线；`probe` 已识别 is_live |

### 风险
- 直播 playlist 分片可能被服务端清理（404）→ 需容错跳过。
- 解密 key 可能轮换（不同分片不同 key）→ 需按分片解析 `#EXT-X-KEY`。
- Agent 场景较少用，低优先级。

### 验收
- 对接公开测试直播流，录制 N 秒后停止，输出文件可播放。
- 加密直播流能正确解密。

---

## I11：ADR 架构决策记录（中优先级）

### 为什么
[docs/](file:///workspace/docs) 当前只有 `comparison/`，无 `adr/`。关键决策（自研 BT vs libtorrent、aiohttp vs niquests、自适应分裂算法）散落代码注释，新贡献者难理解决策背景。

### 怎么改
参考 Ghost 的 `docs/adr/0001-xxxx.md` 格式（但内容原创），每条 ADR 含：
- **Status**：Proposed/Accepted/Deprecated
- **Context**：决策背景与约束
- **Decision**：决定
- **Consequences**：后果（正面+负面）

### 建议首批 ADR
| 编号 | 主题 |
|------|------|
| 0001 | 为何自研 BT 协议而非用 libtorrent |
| 0002 | 为何 aiohttp 作为默认 HTTP 后端 |
| 0003 | 为何采用免合并定位写（os.pwrite） |
| 0004 | 为何自适应分裂而非固定分片 |
| 0005 | 为何 MIT 而非 GPL（与 Ghost 隔离） |

### 改哪里
- 仅新增 `docs/adr/0001-0005.md`，不改代码。

### 验收
- 5 篇 ADR 写完，含 Context/Decision/Consequences 三段。

---

## I12：pack 插件化架构（低优先级）

### 为什么
当前 [protocols/__init__.py:12-33](file:///workspace/protocols/__init__.py) `ProtocolRouter` 硬编码注册 4 个 driver，第三方无法扩展协议。

### 怎么改
1. 定义 `ProtocolPack` 协议（entry_points 注册），含 `manifest` + `driver_class`。
2. `ProtocolRouter` 改为扫描 `draco.packs` entry_points 动态加载。
3. 内置 driver 也改为 pack 注册（向后兼容）。

### 改哪里
| 文件 | 改动 |
|------|------|
| [protocols/__init__.py](file:///workspace/protocols/__init__.py) | `ProtocolRouter` 改用 entry_points 动态加载 |
| setup.py | 注册 `draco.packs` entry point group |

### 风险
- 重构面大，影响所有协议注册路径；低优先级，暂缓。

### 验收
- 第三方包注册 entry_point 后，`router.list_supported()` 能看到。

---

## I13：速度限制（中优先级）

### 为什么
[protocols/http.py](file:///workspace/protocols/http.py) 无限速；Agent 批量下载时可能占满带宽。

### 怎么改
1. `HTTPDriver.__init__` 加 `max_speed_bps: Optional[int]` 参数。
2. 新增 `_RateLimiter` 类：基于令牌桶（token bucket），每次 `iter_chunked` 拿数据前 `await limiter.acquire(len(data))`，超速则 `await asyncio.sleep`。
3. 自适应路径和单线程路径都用同一个 limiter。

### 改哪里
| 文件 | 改动 |
|------|------|
| [protocols/http.py](file:///workspace/protocols/http.py) | 新增 `_RateLimiter`；`HTTPDriver` 加参数；worker 中插入限速 |
| [core.py](file:///workspace/core.py) | `download_async` 透传 `max_speed` 到 driver（可选） |

### 风险
- 令牌桶精度：1ms sleep 粒度足够；多 worker 共享 limiter 需加锁（asyncio.Lock）。

### 验收
- 设 `max_speed_bps=1MB/s`，下载 10MB 文件耗时 ~10s ±1s。
- 限速不影响断点续传正确性。

---

## I14：任务计划（完成后动作）（低优先级，已覆盖）

### 为什么
原清单已标注「已由 TaskStep 管线的步骤回调覆盖，无需额外实现」。

### 结论
**不落地**。Draco 作为库，暴露回调即可，系统操作（关机/打开文件）应由 Agent 层做。当前 [core.py:283](file:///workspace/core.py) 的 `progress_wrapper` + `DownloadResult` 已支持完成后回调，覆盖此需求。

---

## I15：HTTP/3 支持（低优先级）

### 为什么
aiohttp 不支持 HTTP/3；弱网下 HTTP/3（QUIC）更稳。

### 怎么改
**与 I9 合并**。引入 `curl_cffi` 或 `niquests`（均 MIT）作为可选后端后，HTTP/3 自动可用（这些库支持 HTTP/3 协商）。无需单独实现。

### 验收
- 见 I9，backend 为 curl_cffi 时对支持 HTTP/3 的站点走 QUIC（通过 `resp.http_version` 验证）。

---

## 落地优先级建议

按「价值/工作量比」排序，建议落地顺序：

1. **I13 速度限制**（低难度 + 中价值，半天内可完成）
2. **I11 ADR**（零代码风险 + 提升可维护性，1-2 天）
3. **I7 暂停改链接续传**（高价值 + 中难度，2-3 天）
4. **I8a DHT 持久化**（I8 中最独立、最高性价比的部分，1 天）
5. **I8b 文件优先级**（边下边看价值，2-3 天）
6. **I9 TLS 指纹**（反爬价值，3-5 天，含测试）
7. **I8c peer 代理**（依赖 python-socks，1-2 天）
8. **I10 直播录制**（场景少，3-5 天）
9. **I12 插件化**（重构大，暂缓）

I14 不落地（已覆盖），I15 随 I9 自然获得。

---

## 假设与决策

- 本文档为**规划/解释**性质，落地时按各条「怎么改」原创实现，不复制 Ghost 任何代码。
- 所有新增依赖（curl_cffi/python-socks）均为 MIT 许可，标为 optional extras，不影响核心安装。
- 落地前应先在 `docs/comparison/improvement_ideas.md` 将对应条目的 ⏳ 改为 ✅ 并补「Draco 落地点」。
- 每条建议落地后补对应单元/集成测试，不破坏现有 74 测试。
