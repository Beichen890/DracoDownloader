# OpenDracoCLI 落地方案 — P2 至 P5 实施路线图

- **日期**: 2026-08-12
- **状态**: 待评审
- **前置**: P1 跨平台 Shell 内核已完成并提交（commit: 初始实现，71 测试通过）
- **关联**: [P1 设计文档](./2026-08-12-opendracocli-p1-kernel-design.md)

---

## 1. 总览

### 1.1 产品愿景

OpenDracoCLI 定位「AI 时代的智能终端」，最终形态包含 8 大能力：

| 能力 | 来源需求 | 承载阶段 |
|------|----------|----------|
| 跨平台双语法映射 + 别名 | 兼容 cmd/Linux | P1 ✅ |
| 沙箱隔离 + 高危命令 yes 确认 | 安全 | P2 |
| 身份验证（删 root 需验证） | 高危防护 | P2 |
| AI 纠错（命令错误时 AI 反问帮忙） | 智能辅助 | P3 |
| 命令角色化（终端命令一种角色感） | 角色化体验 | P3 |
| 感知与自互动 | 参考 DracoOCR | P3 |
| Agent 代码即行动（Python 自动化） | 参考 DracoHub CodingAgent | P4 |
| 用户包装常用命令为函数 | 函数化 | P4 |
| 可定制启动界面 | 炫酷界面 | P5 |
| 炫酷 AI 界面细化 | 参考 DracoHub CLI | P5 |

### 1.2 阶段依赖与接入方式

```
P1 内核 (已完成)
  │
  ├── P2 安全与风控
  │     └─ 注册 PreExecHook → RiskAssessmentHook
  │
  ├── P3 AI 智能层
  │     ├─ 注册 PreExecHook → AIRewriteHook (角色化/简化)
  │     └─ 注册 PostExecHook → ErrorCorrectHook (纠错反问)
  │
  ├── P4 Agent 自动化
  │     ├─ 新增 PythonChannelExecutor (实现 Executor 协议)
  │     └─ 用户 Python 函数包装 (代码即行动)
  │
  └── P5 打磨
        ├─ cli.py 渲染层 → textual (pipeline 不受影响)
        └─ 可定制启动界面
```

**核心原则**：每个阶段通过 P1 预留的扩展点接入，不改动内核管线。新能力 = 新钩子/新通道/新渲染层。

### 1.3 借鉴的上游设计模式

| 模式 | 来源 | 应用 |
|------|------|------|
| 集中化错误目录 | DracoDownloader `errors.py` | 已在 P1 落地，P3 AI 据错误码决策 |
| TaskStep 管线 | DracoDownloader `steps.py` | 已在 P1 落地，每条命令可观测可重试 |
| 代码即行动 | DracoHub CodingAgent | P4 Python 通道直接调库，不经过 shell |
| 感知自互动 | DracoOCR | P3 感知引擎：监听命令结果 → 主动建议 |
| 角色卡驱动 | DracoHub character_card | P3 命令角色化：把命令"性格化" |

---

## 2. P2 安全与风控

### 2.1 目标

1. **沙箱隔离**：高危命令在受限环境执行（限制可写路径、禁用网络等）
2. **yes 确认**：高危命令执行前需用户输入 yes 确认
3. **身份验证**：极高危操作（如删除系统目录）需验证身份（密码/生物特征）

### 2.2 新增模块

```
opendracocli/
├── security/
│   ├── __init__.py
│   ├── risk_assessor.py     # 风险评估器 (命令 → risk_level)
│   ├── sandbox.py            # 沙箱执行器 (受限 subprocess)
│   ├── confirmer.py          # yes 确认交互
│   ├── authenticator.py      # 身份验证
│   └── rules.json            # 高危命令规则表
├── hooks/
│   └── risk_hook.py          # RiskAssessmentHook (PreExecHook)
```

### 2.3 风险分级

| 等级 | 示例 | 处理 |
|------|------|------|
| `safe` | `ls`, `cat`, `echo` | 直接执行 |
| `caution` | `rm` 单文件, `mv` 覆盖 | yes 确认 |
| `danger` | `rm -rf`, `format`, `dd` | yes 确认 + 沙箱 |
| `critical` | `rm -rf /`, `mkfs`, 写系统目录 | 身份验证 + 沙箱 |

### 2.4 风险规则表 (rules.json)

```json
{
  "critical": {
    "patterns": [
      {"cmd": "rm", "args_contain": ["-rf", "/"], "desc": "删除根目录"},
      {"cmd": "mkfs", "desc": "格式化文件系统"},
      {"cmd": "dd", "args_contain": ["of=/dev/"], "desc": "写裸设备"}
    ]
  },
  "danger": {
    "patterns": [
      {"cmd": "rm", "args_contain": ["-rf"], "desc": "递归强删"},
      {"cmd": "format", "desc": "格式化"},
      {"cmd": "chmod", "args_contain": ["777"], "desc": "全权限"}
    ]
  },
  "caution": {
    "patterns": [
      {"cmd": "rm", "desc": "删除文件"},
      {"cmd": "mv", "desc": "移动/覆盖"},
      {"cmd": "kill", "desc": "杀进程"}
    ]
  }
}
```

### 2.5 RiskAssessmentHook 流程

```
PreExecHook.pre_exec(ctx)
  → risk_assessor.assess(ctx.ir)
  → 返回 RiskLevel
  → safe: CONTINUE
  → caution: confirmer.ask_yes() → CONTINUE / BLOCK
  → danger: confirmer.ask_yes() + sandbox.prepare() → CONTINUE (标记沙箱)
  → critical: authenticator.verify() + confirmer.ask_yes() + sandbox.prepare()
              → CONTINUE / BLOCK
```

### 2.6 沙箱策略

- **路径限制**：`sandbox.py` 包装 executor，在命令前注入 `unshare`/`chroot`（Linux）或路径白名单检查
- **网络隔离**：P2 初期用 `--net=none`（Linux unshare）或禁用网络相关命令
- **可写路径白名单**：默认只允许写 `$HOME` 和 `$TMPDIR`
- **P2 简化**：先做路径白名单拦截（不真正 unshare），`danger`/`critical` 命令尝试写非白名单路径时 BLOCK

### 2.7 身份验证 (authenticator.py)

- **P2 初期**：密码验证（哈希存储于 `~/.opendracocli/auth.hash`）
- **验证流程**：`authenticator.verify()` → prompt_toolkit 隐藏输入 → 比对哈希
- **首次设置**：首次运行 `opendracocli --setup-auth` 设置密码
- **后续演进**：P3+ 可接入系统生物特征（Windows Hello / Touch ID）

### 2.8 测试策略

- 风险分级表单元测试（每条规则匹配/不匹配）
- RiskAssessmentHook 集成测试（mock confirmer/authenticator，验证各等级流程）
- 沙箱路径拦截测试
- 身份验证 round-trip 测试

### 2.9 完成定义

1. `rm -rf /tmp/test` 触发 yes 确认，输入 no 则 BLOCK
2. `rm -rf /` 触发身份验证，密码错误则 BLOCK
3. 沙箱拦截对 `/etc/` 的写操作
4. `is_high_risk` 字段正确写入历史记录
5. 安全规则可通过 `rules.json` 自定义扩展

---

## 3. P3 AI 智能层

### 3.1 目标

1. **AI 纠错**：命令执行失败（非零退出码）时，AI 分析错误并反问用户，提供修复建议
2. **命令角色化**：把终端命令赋予"角色感"（如把 `rm` 描述为"清道夫"，输出带角色语气）
3. **感知与自互动**：监听命令执行结果，主动感知异常模式并提供建议（参考 DracoOCR）

### 3.2 新增模块

```
opendracocli/
├── ai/
│   ├── __init__.py
│   ├── llm_client.py         # LLM 网关客户端 (对接 DracoHub llm-gateway)
│   ├── error_corrector.py    # 纠错引擎 (PostExecHook)
│   ├── rewriter.py           # 命令角色化/简化 (PreExecHook)
│   ├── perception.py         # 感知引擎 (订阅事件)
│   └── prompts.py            # Prompt 模板
├── hooks/
│   ├── ai_rewrite_hook.py    # AIRewriteHook (PreExecHook)
│   └── error_correct_hook.py # ErrorCorrectHook (PostExecHook)
```

### 3.3 AI 纠错流程 (ErrorCorrectHook)

```
PostExecHook.post_exec(ctx, result)
  → if result.exit_code != 0:
      → error_corrector.analyze(ctx.raw_input, result.stderr, result.exit_code)
      → LLM 分析错误原因 + 生成修复建议
      → 反问用户："检测到错误 X，是否尝试 Y？[y/n/edit]"
      → y: 改写 ctx.ir，重新执行
      → n: 放弃
      → edit: 用户手动修改建议后执行
```

### 3.4 命令角色化 (AIRewriteHook + rewriter.py)

**概念**：把命令映射为"角色"，输出带角色语气。

```json
{
  "rm": {"role": "清道夫", "tone": "冷静利落", "desc": "清扫不需要的文件"},
  "ls": {"role": "侦察兵", "tone": "细致汇报", "desc": "侦察目录内容"},
  "git": {"role": "档案官", "tone": "严谨记录", "desc": "管理版本档案"},
  "echo": {"role": "传令兵", "tone": "简洁传达", "desc": "传递信息"}
}
```

- **PreExec 改写**：不改变命令语义，只在输出前后注入角色化包装
- **输出包装**：命令执行后，stderr 为空且 exit=0 时，AI 用角色语气简评（如 rm 成功后"清道夫：已清扫 3 个文件"）
- **简化**：用户输入冗长命令时，AI 建议简化版（如 `git add . && git commit -m x && git push` → 建议设为别名）

### 3.5 感知与自互动 (perception.py)

参考 DracoOCR 的"感知 + 自互动"：

- **感知**：订阅 `CommandExecuted` / `CommandFailed` 事件，积累会话上下文
- **模式识别**：
  - 连续 3 次同类错误 → 主动建议
  - 同一目录反复 `ls` → 建议设别名
  - 频繁连用 `&&` → 建议封装为函数（P4 能力）
- **自互动**：AI 不只是被动响应，而是在检测到模式后**主动**通过 EventBus 发布 `SuggestionEvent`，TUI 渲染为非侵入式提示

### 3.6 LLM 网关对接

复用 DracoHub 的 `llm-gateway`（FastAPI 多 Provider 路由）：
- `ai/llm_client.py` 通过 HTTP 调用 gateway
- 支持流式输出（SSE），纠错建议实时呈现
- 离线降级：gateway 不可达时，AI 层静默降级，终端回到纯 shell 模式

### 3.7 测试策略

- 纠错引擎：mock LLM 返回，验证分析→建议→重试流程
- 角色化：验证输出包装不破坏命令语义
- 感知引擎：注入事件序列，验证模式识别触发
- 离线降级：mock gateway 超时，验证终端不崩溃

### 3.8 完成定义

1. 输入 `git statu`（拼错），AI 提示"是否指 `git status`？"，确认后执行
2. 命令成功执行后，输出含角色化简评
3. 连续 3 次拼错同类命令，AI 主动建议
4. LLM gateway 不可达时，终端正常工作（AI 层降级）

---

## 4. P4 Agent 自动化

### 4.1 目标

1. **代码即行动**：用户用 Python 函数描述意图，Agent 直接调 Python 库完成，不经过 shell（参考 DracoHub CodingAgent）
2. **用户函数包装**：用户可把几条常用命令包装为一个 Python 函数，注册为"命令"
3. **Python 通道**：独立于 shell 通道，共享历史和可观测性

### 4.2 新增模块

```
opendracocli/
├── agent/
│   ├── __init__.py
│   ├── python_executor.py    # PythonChannelExecutor (实现 Executor 协议)
│   ├── function_registry.py  # 用户函数注册表
│   ├── code_runner.py        # 沙箱化 Python 代码执行
│   └── templates.py          # 常用自动化模板
├── shell/
│   └── pipeline.py           # 修改: 增加 Python 通道路由
```

### 4.3 双通道架构

```
用户输入
  → pipeline.run()
  → 判断: shell 命令 还是 Python 函数？
      → shell 命令: 走 P1 管线 (SubprocessExecutor)
      → Python 函数: 走 P4 管线 (PythonChannelExecutor)
  → 两条通道都产出 ExecResult + 发布 CommandExecuted 事件
  → 共享 HistoryStore
```

### 4.4 Python 函数包装

用户在 `~/.opendracocli/functions.py` 定义函数：

```python
# 用户自定义函数
def deploy(ctx):
    """一键部署：拉代码 + 装依赖 + 重启服务"""
    ctx.shell("git pull")
    ctx.shell("pip install -r requirements.txt")
    ctx.shell("systemctl restart myapp")
    return {"status": "deployed"}

def clean_logs(ctx, days=7):
    """清理 N 天前的日志"""
    ctx.shell(f"find /var/log -mtime +{days} -delete")
    return {"cleaned": True}
```

- `function_registry.py` 扫描加载，函数名成为"命令"
- 输入 `deploy` → 走 Python 通道执行 `deploy()` 函数
- 输入 `clean_logs days=3` → Python 通道，参数解析
- `ctx.shell()` 让函数内部可调 shell 命令（复用 P1 管线）

### 4.5 代码即行动

更高级：用户描述意图，AI 生成 Python 函数并执行（P3 LLM + P4 执行器结合）：

```
用户: "把所有 .pyc 文件删掉"
  → AI 生成:
    def clean_pyc(ctx):
        ctx.shell("find . -name '*.pyc' -delete")
  → PythonChannelExecutor 执行
```

### 4.6 沙箱化执行

- `code_runner.py` 用 RestrictedPython（参考 DracoHub sub_agent/security.py）限制危险操作
- 用户函数默认可调 `ctx.shell()`（复用 P2 风控）
- AI 生成的函数需用户确认后才执行

### 4.7 测试策略

- Python 通道 round-trip：定义函数 → 执行 → 验证结果入历史
- `ctx.shell()` 嵌套调用测试
- 参数解析测试
- 沙箱拦截危险 Python 操作

### 4.8 完成定义

1. 在 `~/.opendracocli/functions.py` 定义 `deploy()`，输入 `deploy` 执行
2. 函数内 `ctx.shell("ls")` 正常调 shell 通道
3. Python 通道执行结果写入共享历史
4. AI 生成函数需 yes 确认后执行

---

## 5. P5 打磨

### 5.1 目标

1. **可定制启动界面**：用户可配置启动横幅（ASCII art / 个人信息 / 系统状态）
2. **炫酷界面细化**：从 rich 升级到 textual，提供完整 TUI 应用体验（参考 DracoHub CLI）
3. **主题系统**：配色/字体/布局可定制

### 5.2 新增模块

```
opendracocli/
├── tui/
│   ├── __init__.py
│   ├── app.py               # textual Application 主类
│   ├── widgets/
│   │   ├── chat.py          # 命令交互 widget
│   │   ├── history.py       # 历史侧栏 widget
│   │   ├── status.py        # 底部状态栏
│   │   └── ai_panel.py      # AI 建议/纠错面板
│   ├── theme.py             # 主题系统
│   └── startup.py           # 启动界面渲染
├── themes/                  # 预置主题
│   ├── default.json
│   ├── cyberpunk.json
│   └── minimal.json
```

### 5.3 启动界面定制

`~/.opendracocli/startup.toml`：

```toml
[startup]
banner = "custom_ascii.txt"  # 或 " builtin:dragon"
show_system_info = true
show_last_commands = 3
greeting = "欢迎回来，Master"

[layout]
sidebar = true
sidebar_width = 30
```

### 5.4 渲染层替换

- `cli.py` 的循环逻辑保留，渲染层从 rich 换成 textual
- `pipeline.py` / `history` / `hooks` 完全不动
- textual 提供完整 TUI：分栏布局、鼠标交互、响应式

### 5.5 完成定义

1. 启动时显示自定义 ASCII art 横幅
2. textual 提供分栏布局（命令区 + 历史侧栏 + AI 面板）
3. 可切换至少 3 个预置主题
4. 所有 P1-P4 功能在新 TUI 下正常工作

---

## 6. 实施优先级与建议顺序

### 6.1 推荐顺序

```
P1 ✅ → P2 → P3 → P4 → P5
```

**理由**：
- P2 安全必须先于 P3 AI：AI 纠错可能改写命令，需先有风控兜底
- P3 AI 先于 P4 Agent：P4 的"AI 生成代码"依赖 P3 的 LLM 基础设施
- P5 打磨最后做：避免在功能未定时反复改 UI

### 6.2 可并行项

- P3 感知引擎 与 P3 纠错引擎 可并行开发
- P4 函数注册表 与 P4 Python 执行器 可并行
- P5 主题系统 可与 P5 textual 迁移并行

### 6.3 风险点

| 风险 | 阶段 | 缓解 |
|------|------|------|
| LLM gateway 不稳定 | P3 | 离线降级，AI 层可选 |
| 沙箱跨平台差异大 | P2 | P2 先做路径白名单，unshare/chroot 留 P2.1 |
| textual 学习成本 | P5 | rich 先撑着，textual 作为 P5 增量 |
| 用户函数安全风险 | P4 | RestrictedPython + P2 风控复用 |

---

## 7. 模块依赖矩阵

```
P2 安全:
  security/risk_assessor.py → hooks/risk_hook.py → HookRegistry
  security/sandbox.py → shell/executor.py (包装)
  security/authenticator.py → hooks/risk_hook.py

P3 AI:
  ai/llm_client.py → (外部 llm-gateway)
  ai/error_corrector.py → hooks/error_correct_hook.py → HookRegistry
  ai/rewriter.py → hooks/ai_rewrite_hook.py → HookRegistry
  ai/perception.py → events.EventBus (订阅)

P4 Agent:
  agent/python_executor.py → shell/executor.Executor (协议实现)
  agent/function_registry.py → agent/python_executor.py
  shell/pipeline.py (修改: 通道路由)
  security/ (复用 P2 沙箱)

P5 打磨:
  tui/app.py → shell/pipeline (调用)
  tui/ → cli.py (替换渲染层)
  其他模块不动
```

---

## 8. 配置演进

```python
# P1 (已完成)
@dataclass
class DracoConfig:
    history_db_path: str
    mappings_dir: str
    aliases_file: str
    ...

# P2 追加
    security_rules_file: str = "~/.opendracocli/rules.json"
    auth_hash_file: str = "~/.opendracocli/auth.hash"
    sandbox_writable_paths: list[str] = field(default_factory=lambda: ["~", "/tmp"])
    require_auth_for: str = "critical"  # critical / danger / off

# P3 追加
    llm_gateway_url: str = "http://localhost:8000"
    ai_enabled: bool = True
    ai_correction_enabled: bool = True
    ai_roleplay_enabled: bool = True
    ai_perception_enabled: bool = True

# P4 追加
    functions_file: str = "~/.opendracocli/functions.py"
    agent_code_sandbox: bool = True

# P5 追加
    theme: str = "default"
    startup_config: str = "~/.opendracocli/startup.toml"
    tui_engine: str = "textual"  # textual / rich
```

---

## 9. 事件演进

```python
# P1 (已完成)
EVT_COMMAND_EXECUTED
EVT_COMMAND_FAILED
EVT_SESSION_STARTED
EVT_SESSION_ENDED

# P3 追加
EVT_SUGGESTION          # 感知引擎主动建议
EVT_AI_CORRECTION       # AI 纠错发生
EVT_ROLEPLAY_OUTPUT     # 角色化输出

# P4 追加
EVT_AGENT_EXECUTED      # Python 通道执行
EVT_FUNCTION_CALLED     # 用户函数被调用

# P2 追加
EVT_RISK_BLOCKED        # 高危命令被拦截
EVT_AUTH_REQUIRED       # 触发身份验证
```

---

## 10. 错误码演进

```python
# P1 (已完成)
ERR_SHELL_PARSE_ERROR
ERR_SHELL_UNSUPPORTED_SYNTAX
ERR_SHELL_COMMAND_NOT_FOUND
ERR_ALIAS_UNDEFINED
ERR_ALIAS_RECURSIVE
ERR_EXEC_TIMEOUT
ERR_EXEC_FAILED
ERR_EXEC_CANCELLED
ERR_HISTORY_DB_ERROR

# P2 追加
ERR_RISK_BLOCKED = "draco.risk.blocked"
ERR_RISK_DENIED = "draco.risk.denied"          # 用户拒绝 yes
ERR_AUTH_FAILED = "draco.auth.failed"
ERR_SANDBOX_VIOLATION = "draco.sandbox.violation"

# P3 追加
ERR_AI_UNAVAILABLE = "draco.ai.unavailable"    # LLM 不可达
ERR_AI_CORRECTION_FAILED = "draco.ai.correction_failed"

# P4 追加
ERR_AGENT_SYNTAX = "draco.agent.syntax"        # 用户函数语法错误
ERR_AGENT_SANDBOX = "draco.agent.sandbox"      # 沙箱拦截
ERR_FUNCTION_NOT_FOUND = "draco.function.not_found"
```

---

## 11. 总结

P1 已完成内核地基，扩展点（PreExecHook / PostExecHook / EventBus / Executor 协议）全部就位。P2-P5 每个阶段通过注册新钩子、新通道、新渲染层接入，**不改动内核管线**。这保证了：
- 增量开发：每阶段独立可测可交付
- 回归可控：新能力不影响已有功能
- 渐进增强：AI/Agent 层可降级，终端始终可用

下一步建议从 **P2 安全与风控** 开始，为后续 AI 改写命令提供风控兜底。
