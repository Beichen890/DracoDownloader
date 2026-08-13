# OpenDracoCLI — P1 跨平台 Shell 内核 设计文档

- **日期**: 2026-08-12
- **阶段**: P1（5 阶段拆分的第一阶段）
- **状态**: 待评审
- **关联项目**: DracoHub-AI（CLI/Agent 哲学）、DracoDownloader（错误目录/TaskStep 管线哲学）

---

## 1. 背景与目标

OpenDracoCLI 定位为「AI 时代的智能终端」。整个产品拆分为 5 个阶段：

| 阶段 | 子系统 | 状态 |
|------|--------|------|
| **P1** | 跨平台 Shell 内核 | **本 spec** |
| P2 | 安全与风控（沙箱 + yes 确认 + 身份验证） | 后续 |
| P3 | AI 智能层（纠错 + 角色化 + 感知自互动） | 后续 |
| P4 | Agent 自动化（代码即行动 + Python 函数包装） | 后续 |
| P5 | 打磨（可定制启动界面 + 炫酷界面细化） | 后续 |

P1 是所有后续阶段的地基。本 spec 只覆盖 P1。

### P1 目标

1. 双语法自动映射：用户输入 `ls` 在 Windows 自动映射为 `dir`，输入 `dir` 在 Linux 自动映射为 `ls`；同时允许用户自定义简化别名/宏。
2. 纯 subprocess 调原生 shell 执行命令，最大化兼容性。
3. 提供基础 TUI 循环（输入、补全、历史回溯、多行、彩色输出）。
4. 用 SQLite 持久化命令历史（中等集粒度）。
5. 预留 PreExec/PostExec 钩子和事件总线，使 P2/P3/P4 不改动内核即可接入。

### P1 非目标（明确推迟）

沙箱隔离、高危命令 yes 确认、身份验证、AI 纠错、命令角色化、感知自互动、Python 函数包装、Agent 自动化、可定制启动界面、炫酷界面细化——全部不在 P1 范围。

---

## 2. 架构总览

### 2.1 执行管线

一次用户输入的完整生命周期：

```
用户输入
  → Lexer          (shlex 词法切分)
  → Parser         (识别管道 | / 重定向 > < / 逻辑 && ||)
  → Normalizer     (每个命令节点归一化为 canonical IR)
  → AliasExpander  (查别名表, 宏展开, 产出抽象命令名)
  → PlatformMapper (展开后命令名 → 当前平台原生命令名)
  → PreExec 钩子链 [P2/P3 预留接入点]
  → SubprocessExecutor (调原生 shell 执行)
  → PostExec 钩子链 [P3 预留接入点]
  → 事件总线        (发布 CommandExecuted 事件)
  → HistoryStore   (写 SQLite)
  → 返回输出给 TUI
```

### 2.2 关键架构决策

**决策 1：IR 只管单命令节点，组合语义透传 shell。**
管道 `|`、重定向 `>` `<`、`&&` `||` 这些 shell 组合操作的**执行语义**不在 IR 里重新实现——Parser 识别出它们后，SubprocessExecutor 将整条命令（含组合符）重新序列化为字符串交给原生 shell 执行。IR 只负责**单个命令节点**的命令名/参数归一化和平台映射。

注意：IR 中仍会记录 `operators` 和 `Redirect` 作为**元数据**（供可观测性、历史记录、P3 AI 感知使用），但 OpenDracoCLI 自己不实现这些组合操作的执行语义。这是「纯 subprocess 调原生 shell」选择的必然结果，避免重新实现 shell 语法解析器。

**决策 1b：别名展开先于平台映射（宏展开先于语义转换）。**
管线顺序固定为 `AliasExpander → PlatformMapper`。理由：别名展开产出的是**抽象命令名**（如 `ll = ls -la` 中的 `ls`），平台映射才把抽象名具体化为平台原生命令（`ls → dir`）。若先映射后展开，别名 `ll` 在 Windows 上会展开出逃逸映射的 `ls`，导致执行失败。这与编译器"预处理（宏展开）→ 编译（语义转换）"同一原则。无论别名嵌套多深，映射始终作用于展开后的最终命令名。

**决策 2：两条执行通道分离（为 P4 铺路）。**
P1 管线只走 **shell 通道**。P4 的 Agent 自动化走**独立的 Python 执行通道**（直接调 Python API/库，不经过 shell）。两条通道通过事件总线共享历史和可观测性，互不污染。这条分离线在 P1 就划清楚，P4 时无需回改内核。

**决策 3：分层翻译管线 + 事件总线（方案 A）。**
选用分层管线而非直接派发表，原因是 P2/P3/P4 都要往执行链里插东西（风控拦截、AI 纠错、Agent 自动化）。事件总线 + 钩子链让后续阶段只加订阅者不动内核。

**决策 4：借鉴上游两个设计模式。**
- **集中化错误目录**（抄 DracoDownloader `errors.py`）：稳定错误码字符串，P3 AI 据此决定怎么帮用户。
- **TaskStep 风格管线**：每条命令经过 `parse → normalize → map → risk → exec → history`，每步可观测、可单步重试。

---

## 3. 模块拆分

```
opendracocli/
├── opendracocli/
│   ├── __init__.py
│   ├── cli.py                  # TUI 主循环 (prompt_toolkit + rich)
│   ├── config.py               # 配置 (环境变量 DRACO_* 覆盖)
│   ├── errors.py               # 集中化错误目录
│   ├── events.py               # 事件总线
│   ├── logger.py               # 日志
│   ├── shell/
│   │   ├── __init__.py
│   │   ├── lexer.py            # shlex 词法
│   │   ├── parser.py           # 识别管道/重定向/逻辑操作符 → CommandIR
│   │   ├── ir.py               # canonical IR 定义 (CommandNode)
│   │   ├── normalizer.py       # 归一化
│   │   ├── platform_mapper.py  # IR → 当前平台原生命令
│   │   ├── alias_expander.py   # 别名/宏展开
│   │   ├── executor.py         # SubprocessExecutor
│   │   └── pipeline.py         # 编排整个管线 (TaskStep 风格)
│   ├── hooks/
│   │   ├── __init__.py
│   │   ├── base.py             # PreExecHook / PostExecHook 抽象
│   │   └── registry.py         # 钩子注册与链式调用
│   ├── history/
│   │   ├── __init__.py
│   │   ├── store.py            # SQLite 存储
│   │   └── models.py           # 数据模型 (dataclass)
│   ├── aliases/
│   │   ├── __init__.py
│   │   ├── manager.py          # 别名管理 (增删查改)
│   │   └── loader.py           # 从配置文件加载别名
│   └── mappings/
│       ├── __init__.py
│       ├── unix_to_win.json    # Unix → Windows 命令映射表
│       └── win_to_unix.json    # Windows → Unix 命令映射表
├── tests/
│   ├── test_lexer.py
│   ├── test_parser.py
│   ├── test_platform_mapper.py
│   ├── test_alias_expander.py
│   ├── test_executor.py
│   ├── test_history.py
│   └── test_pipeline.py
├── pyproject.toml
└── README.md
```

### 3.1 各模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| `cli.py` | prompt_toolkit 输入循环 + rich 渲染输出；会话管理；调用 pipeline | pipeline, history, aliases |
| `shell/lexer.py` | 用 shlex 切分输入为 token 流 | 无 |
| `shell/parser.py` | 识别 `\|` `>` `<` `&&` `\|\|` `;`，产出 `CommandIR`（含组合节点 + 单命令节点） | ir |
| `shell/ir.py` | `CommandNode`（命令名+参数）、`Pipeline`（节点序列）、`Redirect` 等 dataclass | 无 |
| `shell/normalizer.py` | 命令名小写归一、去前后空白、参数 trim | ir |
| `shell/platform_mapper.py` | 查 `mappings/*.json`，把 IR 命令名换成当前平台原生名；未命中则原样透传 | ir, mappings/ |
| `shell/alias_expander.py` | 查别名表，命中则展开为宏（多命令用 `&&` 连接）；递归深度限制 3 层防死循环 | aliases, ir |
| `shell/executor.py` | `SubprocessExecutor`：用 `asyncio.create_subprocess_exec` 调原生 shell，捕获 stdout/stderr/exit_code | 无 |
| `shell/pipeline.py` | 编排 lexer→parser→...→history 的完整流程，暴露 `async run(input) -> ExecResult` | 全部 shell 子模块 + hooks + history + events |
| `hooks/base.py` | `PreExecHook`（可阻断）、`PostExecHook`（只读）抽象基类 | events |
| `hooks/registry.py` | 钩子注册表，按优先级链式调用 | base |
| `events.py` | 简单同步事件总线：`subscribe(event_type, handler)` / `publish(event)` | 无 |
| `history/store.py` | SQLite 读写，`record()` / `search()` / `recent()` | models |
| `history/models.py` | `CommandRecord` dataclass | 无 |
| `aliases/manager.py` | 别名 CRUD + 持久化到配置文件 | loader |
| `aliases/loader.py` | 从 JSON/TOML 配置加载别名定义 | 无 |
| `errors.py` | 稳定错误码常量 + `DracoError` 异常 + 可翻译消息表 | 无 |
| `config.py` | `DracoConfig`，环境变量 `DRACO_*` 覆盖，校验器 | 无 |
| `logger.py` | 结构化日志（参考 DracoHub DebugLogger） | 无 |

---

## 4. 数据流

以用户在 Windows 上输入 `ll && grep foo`（已定义别名 `ll = ls -la`）为例：

1. **Lexer**: `['ll', '&&', 'grep', 'foo']`
2. **Parser**: 识别 `&&`，产出 `CommandIR(nodes=[CommandNode('ll', []), CommandNode('grep', ['foo'])], operators=['&&'])`
3. **Normalizer**: 命令名归一小写（已小写，无变化）
4. **AliasExpander**: `ll` 命中别名，展开为 `ls -la`。IR 变为 `[CommandNode('ls', ['-la']), CommandNode('grep', ['foo'])]`。注意此处产出的是**抽象命令名 `ls`**，尚未平台映射。
5. **PlatformMapper**: Windows 平台，`ls → dir`；`grep` 无映射透传。IR 变为 `[CommandNode('dir', ['-la']), CommandNode('grep', ['foo'])]`
6. **PreExec 钩子**: P1 无注册钩子，直接通过
7. **SubprocessExecutor**: 将 IR 重新序列化为 `dir -la && grep foo`，调 `cmd /c "dir -la && grep foo"`
8. **PostExec 钩子**: P1 无注册钩子
9. **事件总线**: 发布 `CommandExecuted(raw='ll && grep foo', mapped='dir -la && grep foo', exit_code=0, alias_used='ll', ...)`
10. **HistoryStore**: 写入 SQLite 一条记录
11. **TUI**: rich 渲染 stdout

> 对比：若顺序反为先映射后展开，第 4 步 `ll` 会被 PlatformMapper 当成未知命令透传，第 5 步展开成 `ls -la`，`ls` 在 Windows 不存在 → 执行失败。这正是决策 1b 要避免的。

### 4.1 映射表的对称性

`mappings/unix_to_win.json` 与 `win_to_unix.json` 是两张独立表（而非自动反推），因为某些命令参数语义不对等（如 `ls -la` 的 `-la` 在 `dir` 上不完全等价）。P1 只做命令名映射，参数透传；参数级智能转换留给 P3 AI 层。这是有意为之的边界。

---

## 5. 数据模型

### 5.1 CommandIR (shell/ir.py)

```python
@dataclass
class CommandNode:
    name: str                    # 归一化后的命令名 (小写)
    args: list[str]              # 参数列表
    raw: str                     # 原始文本片段

@dataclass
class CommandIR:
    nodes: list[CommandNode]     # 命令节点序列
    operators: list[str]         # 节点间的操作符 ('&&', '||', '|', ';', '')
    redirects: list[Redirect]    # 重定向 (>, <, >>)
    raw_input: str               # 用户原始完整输入

@dataclass
class Redirect:
    kind: str                    # 'stdout', 'stderr', 'stdin', 'append'
    target: str                  # 文件路径
```

### 5.2 CommandRecord (history/models.py)

```python
@dataclass
class CommandRecord:
    id: int | None
    raw_input: str               # 用户原始输入
    canonical_ir: str            # 归一化 IR 的 JSON
    mapped_command: str          # 映射+展开后最终交给 shell 的命令
    platform: str                # 'win' | 'linux' | 'darwin'
    cwd: str                     # 执行时工作目录
    exit_code: int | None
    started_at: float            # Unix 时间戳
    duration_ms: int
    stdout_summary: str          # stdout 前 N 行 (默认 20 行, 截断)
    stderr_summary: str          # stderr 前 N 行
    is_high_risk: bool           # P1 恒 False, P2 风控填充
    alias_used: str | None       # 命中的别名名, 无则 None
    session_id: str              # 会话 ID (TUI 启动生成)
```

### 5.3 SQLite Schema (history/store.py)

```sql
CREATE TABLE IF NOT EXISTS commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_input       TEXT    NOT NULL,
    canonical_ir    TEXT,
    mapped_command  TEXT,
    platform        TEXT    NOT NULL,
    cwd             TEXT    NOT NULL,
    exit_code       INTEGER,
    started_at      REAL    NOT NULL,
    duration_ms     INTEGER,
    stdout_summary  TEXT,
    stderr_summary  TEXT,
    is_high_risk    INTEGER DEFAULT 0,
    alias_used      TEXT,
    session_id      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commands_started_at ON commands(started_at);
CREATE INDEX IF NOT EXISTS idx_commands_session    ON commands(session_id);
CREATE INDEX IF NOT EXISTS idx_commands_raw_input  ON commands(raw_input);
```

数据库文件默认路径：`~/.opendracocli/history.db`，可通过 `DRACO_HISTORY_DB` 环境变量覆盖。

---

## 6. 错误处理

### 6.1 集中化错误目录 (errors.py)

稳定错误码字符串契约（不随版本变更），P3 AI 据此决定如何帮用户：

| 错误码 | 含义 | 可重试 |
|--------|------|--------|
| `draco.shell.parse_error` | 输入解析失败（语法错误） | 否 |
| `draco.shell.unsupported_syntax` | 不支持的 shell 语法 | 否 |
| `draco.shell.command_not_found` | 命令在当前平台不存在 | 否 |
| `draco.alias.undefined` | 引用了未定义的别名 | 否 |
| `draco.alias.recursive` | 别名递归超过 3 层 | 否 |
| `draco.exec.timeout` | 执行超时 | 是 |
| `draco.exec.failed` | 执行失败（非 0 退出码） | 视情况 |
| `draco.exec.cancelled` | 用户取消 (Ctrl+C) | 否 |
| `draco.history.db_error` | 历史数据库读写错误 | 是 |

### 6.2 DracoError 异常

```python
@dataclass
class DracoError(Exception):
    code: str                    # 稳定错误码
    message: str                 # 默认中文消息
    detail: dict[str, Any]       # 结构化上下文 (供 P3 AI 消费)
    retryable: bool = False
```

### 6.3 错误流转

- 解析/映射/别名阶段抛 `DracoError` → pipeline 捕获 → 发布 `CommandFailed` 事件 → 写历史（exit_code=None）→ TUI 用 rich 红色渲染错误码 + 消息
- 执行阶段非 0 退出码 **不抛异常**，作为正常 `ExecResult(exit_code=非0)` 流转（命令失败是正常结果，不是 OpenDracoCLI 的错误）

---

## 7. 预留扩展点

### 7.1 PreExecHook (hooks/base.py)

```python
class PreExecHook(ABC):
    priority: int = 100          # 数字越小越先执行

    @abstractmethod
    async def pre_exec(self, ctx: ExecContext) -> ExecDecision:
        """返回 CONTINUE / BLOCK(reason)"""
        ...

# ExecDecision.CONTINUE — 放行
# ExecDecision.BLOCK(reason, code) — 阻断执行, 记录原因
```

**P2 接入**：注册 `RiskAssessmentHook`，高危命令返回 `BLOCK`，触发 yes 确认流程。
**P3 接入**：注册 `AIRewriteHook`，可改写 `ctx.ir`（命令角色化、简化）。

### 7.2 PostExecHook (hooks/base.py)

```python
class PostExecHook(ABC):
    priority: int = 100

    @abstractmethod
    async def post_exec(self, ctx: ExecContext, result: ExecResult) -> None:
        """只读, 不能改结果。用于纠错建议、统计等。"""
        ...
```

**P3 接入**：注册 `ErrorCorrectHook`，`exit_code != 0` 时调 LLM 生成纠错建议，反问用户。

### 7.3 事件总线 (events.py)

```python
class EventBus:
    def subscribe(self, event_type: str, handler: Callable) -> None: ...
    def publish(self, event: Event) -> None: ...

# P1 内置事件:
# - CommandExecuted  (成功执行)
# - CommandFailed    (解析/映射/别名阶段错误)
# - SessionStarted   (TUI 启动)
# - SessionEnded     (TUI 退出)
```

### 7.4 Python 执行通道接口（P4 预留）

`executor.py` 中定义 `Executor` 协议，`SubprocessExecutor` 是其实现。P4 新增 `PythonChannelExecutor` 实现，走独立通道。两条通道都产出 `ExecResult`、都发布 `CommandExecuted` 事件，共享 history。

---

## 8. 配置 (config.py)

```python
@dataclass
class DracoConfig:
    history_db_path: str = "~/.opendracocli/history.db"
    mappings_dir: str = "mappings"        # 平台映射表目录
    aliases_file: str = "~/.opendracocli/aliases.json"
    stdout_summary_lines: int = 20
    exec_timeout: float = 3600.0          # 单命令默认超时
    max_alias_depth: int = 3              # 别名递归深度上限
    log_level: str = "INFO"
```

所有字段支持 `DRACO_*` 环境变量覆盖（如 `DRACO_HISTORY_DB`、`DRACO_LOG_LEVEL`），参考 DracoDownloader 的配置哲学。

---

## 9. 测试策略

| 测试文件 | 覆盖范围 | 类型 |
|----------|----------|------|
| `test_lexer.py` | shlex 切分边界（引号、转义、空格） | 单元 |
| `test_parser.py` | 管道/重定向/`&&`/`\|\|`/`;` 识别 + IR 构造 | 单元 |
| `test_platform_mapper.py` | ls↔dir 双向映射、未命中透传、参数保留 | 单元 |
| `test_alias_expander.py` | 宏展开、递归深度限制、未定义别名报错 | 单元 |
| `test_executor.py` | subprocess mock、超时、取消、退出码捕获 | 单元 |
| `test_history.py` | SQLite round-trip、search、recent、摘要截断 | 单元 |
| `test_pipeline.py` | 端到端：输入→执行→历史写入→事件发布 | 集成 |

**跨平台 CI**：GitHub Actions 矩阵 `ubuntu-latest` + `windows-latest`，确保映射在双平台都跑通。

---

## 10. 技术选型

| 依赖 | 用途 | 版本 |
|------|------|------|
| Python | 运行时 | >= 3.10 |
| `rich` | 彩色输出渲染 | >= 13.7 |
| `prompt_toolkit` | 输入行（补全/历史/多行） | >= 3.0 |
| `sqlite3` | 历史存储（标准库） | — |
| `shlex` | 词法切分（标准库） | — |
| `pytest` | 测试 | >= 7.0 |

无 GPL 依赖（延续 DracoDownloader 哲学），全部 MIT 或更宽松许可。

---

## 11. P1 完成定义 (Definition of Done)

1. `pip install -e .` 后 `opendracocli` 命令可启动 TUI
2. 在 Windows 输入 `ls` 实际执行 `dir`；在 Linux 输入 `dir` 实际执行 `ls`（参数透传）
3. 管道 `|`、重定向 `>`、`&&` `||` 在双平台正常工作（透传原生 shell）
4. 用户可定义别名 `mydeploy = git add . && git commit -m "x" && git push`，输入 `mydeploy` 展开执行
5. 所有命令（含失败）写入 `~/.opendracocli/history.db`
6. TUI 支持上下方向键回溯历史、Tab 补全命令名
7. 全部测试通过，双平台 CI 绿
8. PreExecHook/PostExecHook/EventBus 接口已定义并有单元测试覆盖（即使 P1 不注册业务钩子）

---

## 12. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 命令名映射后参数语义不对等（如 `ls -la` → `dir -la` 在 Windows 报错） | P1 接受这一限制，参数透传；P3 AI 层做智能参数转换。文档明确说明 |
| subprocess 调原生 shell 在不同 shell（bash/zsh/powershell/cmd）行为不一 | P1 固定：Windows 用 `cmd /c`，Unix 用 `bash -c`（可通过配置覆盖） |
| 别名递归导致死循环 | `max_alias_depth=3` 硬限制，超限抛 `draco.alias.recursive` |
| SQLite 并发（TUI + 未来后台 Agent 同时写） | P1 单进程无并发问题；P4 引入多通道时改用 WAL 模式 |

---

## 13. 后续阶段接入示意

- **P2 安全**：注册 `RiskAssessmentHook(PreExecHook)`，高危命令 `BLOCK` → 触发 yes 确认 → 身份验证。不改内核。
- **P3 AI**：注册 `AIRewriteHook(PreExecHook)` 做命令角色化/简化；注册 `ErrorCorrectHook(PostExecHook)` 在 `exit_code!=0` 时调 LLM 反问用户。不改内核。
- **P4 Agent**：新增 `PythonChannelExecutor` 实现 `Executor` 协议，走独立通道；用户 Python 函数包装作为该通道的命令源。不改 shell 通道。
- **P5 打磨**：`cli.py` 的 TUI 渲染层可整体替换为 textual，pipeline/history/hooks 不受影响。
