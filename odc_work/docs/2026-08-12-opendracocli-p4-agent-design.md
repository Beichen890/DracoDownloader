# OpenDracoCLI P4 — Agent 自动化（代码即行动）

- **日期**: 2026-08-12
- **状态**: 待评审
- **前置**: P1 跨平台 Shell 内核 / P2 安全与风控 / P3 AI 智能层 均已合并 main
- **关联**:
  - [P1 内核设计](./2026-08-12-opendracocli-p1-kernel-design.md)
  - [P2 安全与风控设计](./2026-08-12-opendracocli-p2-security-design.md)
  - [P3 AI 智能层设计](./2026-08-12-opendracocli-p3-ai-layer-design.md)
  - [P2-P5 落地路线图](./2026-08-12-opendracocli-落地路线图-P2-P5.md)

---

## 1. 目标与非目标

### 1.1 目标

1. **代码即行动**（参考 DracoHub CodingAgent）：用户描述意图，AI 生成 Python 函数并执行，直接调 Python 库完成，不经过 shell
2. **用户函数包装**：用户在 `~/.opendracocli/functions.py` 把常用命令包装为 Python 函数，函数名即"命令"
3. **Python 通道**：独立于 shell 通道，共享历史与可观测性，复用 P2 风控（函数内 `ctx.shell()` 走完整管线）
4. **沙箱与确认**：用户函数默认信任；AI 生成代码需 yes 确认 + AST 危险调用黑名单

### 1.2 非目标

- 不做多 Agent 编排/任务图（留 P6+）
- 不做函数热重载文件监听（`/agent reload` 手动触发即可）
- 不引入 RestrictedPython 重依赖（用 AST 黑名单 + 确认流程足够）
- 不改 P1 管线内核：通道路由在 CLI 层判断，pipeline 不动

---

## 2. 设计原则

| 原则 | 落地 |
| --- | --- |
| 代码即行动（DracoHub CodingAgent） | AI 生成 Python 函数而非 shell 命令；函数可直接 `import` 调库 |
| 不改动内核管线 | 通道路由在 `cli._exec_command`，ShellPipeline 零改动 |
| 复用 P1-P3 基础设施 | `ctx.shell()` → ShellPipeline（含 P2 风控 + P3 AI）；历史/事件/错误目录复用 |
| 渐进增强 + 降级 | `agent_enabled=False` 默认关闭；AI 不可用时函数通道仍可用 |
| 用户写函数 = 信任，AI 写代码 = 确认 | 用户 `functions.py` 直接 exec；AI 生成需 `ask_yes` + AST 检查 |

---

## 3. 架构

### 3.1 双通道路由

```
用户输入
  → cli._exec_command(text)
  → 路由判断：
      ├─ slash 命令 (/...)        → 内置命令处理
      ├─ 首 token 命中 FunctionRegistry → Python 通道 (PythonChannelExecutor)
      └─ 其他                     → Shell 通道 (ShellPipeline, P1-P3 不变)
  → 两条通道都：
      ├─ 写共享 HistoryStore
      ├─ 发事件 (EVT_AGENT_EXECUTED / EVT_COMMAND_EXECUTED)
      └─ 可被钩子观测
```

路由判断在 CLI 层而非 pipeline 内部，保证 ShellPipeline 零改动，且 Python 通道可独立演进。

### 3.2 模块划分

```
opendracocli/
├── agent/
│   ├── __init__.py
│   ├── context.py            # AgentContext（ctx.shell/log/env/cwd/print）
│   ├── arg_parser.py         # 函数参数解析（positional + keyword）
│   ├── function_registry.py  # FunctionRegistry（加载 functions.py + 注册）
│   ├── code_runner.py        # 沙箱化执行（AST 黑名单 + 确认）
│   ├── python_executor.py    # PythonChannelExecutor（独立通道）
│   ├── code_generator.py     # AI 生成函数（复用 P3 LLMClient）
│   └── templates.py          # 常用自动化模板（示例库）
├── cli.py                    # 修改: 通道路由 + /agent 命令族
├── config.py                 # 扩展: P4 字段
├── errors.py                 # 扩展: P4 错误码
└── events.py                 # 扩展: P4 事件
```

---

## 4. 核心接口契约

### 4.1 AgentContext

用户函数接收的执行上下文，复用 DracoHub CodingAgent 的 ctx 模式：

```python
class AgentContext:
    """用户函数的执行上下文

    用户函数签名约定：def name(ctx, *args, **kwargs) -> Any
    ctx 提供：
      - ctx.shell(cmd): 调 shell 通道（复用 P1 管线 + P2 风控），返回 dict
      - ctx.log(msg): 记录日志（显示给用户）
      - ctx.print(msg): 打印到 stdout（函数内可见输出）
      - ctx.env: 环境变量快照
      - ctx.cwd: 工作目录
      - ctx.config: DracoConfig 引用（只读）
    """
    async def shell(self, cmd: str) -> dict: ...
    def log(self, msg: str) -> None: ...
    def print(self, msg: str) -> None: ...
```

`ctx.shell()` 内部调用 ShellPipeline.run()，因此 P2 风控 + P3 AI 纠错全部生效，不绕过安全。

### 4.2 FunctionRegistry

```python
@dataclass
class RegisteredFunction:
    name: str
    func: Callable
    signature: inspect.Signature
    doc: str
    source: str
    is_builtin: bool

class FunctionRegistry:
    def load_user_file(self, path: Path) -> int: ...   # exec functions.py，返回加载数
    def register(self, name: str, func: Callable, *, builtin=False) -> None: ...
    def get(self, name: str) -> Optional[RegisteredFunction]: ...
    def match(self, first_token: str) -> Optional[RegisteredFunction]: ...  # 路由判断
    def list(self) -> Dict[str, RegisteredFunction]: ...
    def reload(self) -> int: ...
```

### 4.3 PythonChannelExecutor

```python
@dataclass
class AgentExecResult:
    success: bool
    exit_code: Optional[int]       # 0 成功 / 1 异常 / None 被取消
    stdout: str
    stderr: str
    return_value: Any = None
    duration_ms: int = 0
    func_name: str = ""
    mapped_command: str = ""       # "func_name(arg=1)" 供历史记录

class PythonChannelExecutor:
    async def execute(
        self,
        reg_func: RegisteredFunction,
        args: list,
        kwargs: dict,
        *,
        ctx_cwd: Optional[str] = None,
        session_id: str = "",
    ) -> AgentExecResult: ...
```

### 4.4 CodeGenerator（代码即行动）

复用 P3 `LLMClient`，让 AI 生成 Python 函数源码：

```python
@dataclass
class GeneratedFunction:
    ok: bool
    code: str = ""           # 完整函数源码
    func_name: str = ""
    description: str = ""
    error: str = ""

class CodeGenerator:
    def __init__(self, client: LLMClient, *, platform: str = ""): ...
    async def generate(self, intent: str, *, context_summary: str = "") -> GeneratedFunction: ...
```

生成流程：
1. 构建系统 prompt：要求生成 `def <name>(ctx, ...):` 签名函数，可用 `ctx.shell()`
2. LLM 返回代码块
3. 提取 ```python 代码块 + 函数名
4. AST 校验语法
5. 返回给 CLI，用户 yes 确认后 exec + 执行

### 4.5 code_runner 沙箱

```python
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "globals", "locals",
    "getattr", "setattr", "delattr",  # 反射类（限制）
}
FORBIDDEN_MODULES = {
    "subprocess", "shutil", "ctypes", "multiprocessing",
}  # 仅对 AI 生成代码限制；用户 functions.py 不限制

class CodeRunner:
    def check_safety(self, code: str, *, strict: bool = False) -> list[str]: ...  # 返回违规列表
    def exec_function_code(self, code: str, namespace: dict) -> tuple[Optional[Callable], str]: ...
```

- `strict=False`（用户函数）：仅做语法检查
- `strict=True`（AI 生成）：AST 遍历检查禁用 import / 危险内置

---

## 5. 用户函数约定

### 5.1 functions.py 示例

```python
# ~/.opendracocli/functions.py
# 用户自定义函数，函数名即命令名
# 约定签名：def name(ctx, ...) -> Any

async def deploy(ctx):
    """一键部署：拉代码 + 装依赖 + 重启服务"""
    ctx.log("开始部署...")
    r = await ctx.shell("git pull")
    if not r["success"]:
        return {"status": "failed", "reason": r["stderr"]}
    await ctx.shell("pip install -r requirements.txt")
    await ctx.shell("systemctl restart myapp")
    ctx.log("部署完成")
    return {"status": "deployed"}


def clean_logs(ctx, days=7):
    """清理 N 天前的日志"""
    ctx.print(f"清理 {days} 天前的日志")
    return {"cleaned": True, "days": days}


def greet(ctx, name="world"):
    """问候"""
    ctx.print(f"hello {name}")
    return name
```

### 5.2 调用语法

```
deploy                    # 无参
clean_logs days=3         # 关键字参数
clean_logs 3              # 位置参数（按签名顺序）
greet name=draco          # 关键字
greet draco               # 位置
```

参数解析由 `arg_parser.py` 处理：`key=value` → kwargs，否则 → args。

### 5.3 函数同步/异步兼容

- 函数可以是 `def` 或 `async def`
- `PythonChannelExecutor` 检测 `inspect.iscoroutinefunction`，异步则 await
- `ctx.shell()` 始终是 async（内部调 pipeline.run）

---

## 6. CLI 集成

### 6.1 /agent 命令族

```
/agent                    # 显示 Agent 状态
/agent on                 # 启用 Agent 通道
/agent off                # 禁用 Agent 通道
/agent list               # 列出已注册函数
/agent show <name>        # 查看函数源码
/agent reload             # 重新加载 functions.py
/agent gen <intent>       # AI 生成函数（代码即行动）
/agent run <name> [args]  # 显式调用函数（也可直接输入函数名）
/agent templates          # 列出可用模板
/agent apply <template>   # 应用模板到 functions.py
```

### 6.2 通道路由（cli._exec_command）

```python
async def _exec_command(self, text: str) -> None:
    # P4: 先判断是否命中已注册函数
    if self._agent_enabled and self._registry is not None:
        first_token = text.split()[0] if text.split() else ""
        reg = self._registry.match(first_token)
        if reg is not None:
            await self._exec_agent(text, reg)
            return
    # 否则走 shell 通道（P1-P3 不变）
    await self._exec_shell(text)
```

### 6.3 代码即行动流程（/agent gen）

```
用户: /agent gen 把所有 .pyc 文件删掉
  → CodeGenerator.generate(intent)
  → LLM 返回:
      def clean_pyc(ctx):
          """删除所有 .pyc 文件"""
          await ctx.shell("find . -name '*.pyc' -delete")
          return {"action": "done"}
  → 渲染代码给用户
  → ask_yes("执行此函数？")
  → 是: CodeRunner.exec + PythonChannelExecutor.execute
  → 否: 询问是否保存到 functions.py
  → 结果入历史 + 发 EVT_AGENT_EXECUTED
```

---

## 7. 配置扩展

```python
# P4 追加
agent_enabled: bool = False
functions_file: str = "~/.opendracocli/functions.py"
agent_code_sandbox: bool = True          # AI 生成代码做 AST 检查
agent_auto_confirm_generated: bool = False  # AI 生成代码默认需确认
agent_max_gen_tokens: int = 1024
```

---

## 8. 事件与错误码

### 8.1 事件

```python
EVT_AGENT_EXECUTED = "AgentExecuted"    # Python 通道执行（成功/失败）
EVT_FUNCTION_CALLED = "FunctionCalled"  # 用户函数被调用
EVT_AGENT_CODE_GENERATED = "AgentCodeGenerated"  # AI 生成代码
```

### 8.2 错误码

```python
ERR_AGENT_SYNTAX = "draco.agent.syntax"          # 用户函数语法错误
ERR_AGENT_SANDBOX = "draco.agent.sandbox"        # 沙箱拦截
ERR_FUNCTION_NOT_FOUND = "draco.function.not_found"
ERR_AGENT_ARGS = "draco.agent.args"              # 参数解析失败
ERR_AGENT_EXEC_FAILED = "draco.agent.exec_failed"
```

---

## 9. 安全约束

1. **用户 functions.py 信任**：用户自己写的代码，exec 时不做 AST 黑名单（仅语法检查）
2. **AI 生成代码确认**：`agent_auto_confirm_generated=False` 默认需 yes 确认
3. **AI 生成代码 AST 检查**：`agent_code_sandbox=True` 时禁用 `subprocess`/`ctypes`/`eval`/`exec` 等
4. **ctx.shell() 不绕过风控**：函数内调 shell 走完整 ShellPipeline，P2 RiskAssessmentHook + P3 AIRiskHook 全部生效
5. **agent_enabled 默认关闭**：需显式 `/agent on` 或配置开启
6. **函数执行超时**：复用 `exec_timeout`，避免死循环

---

## 10. 测试策略

| 测试文件 | 覆盖点 |
| --- | --- |
| `test_agent_context.py` | ctx.shell/log/print、env 快照、shell 复用管线 |
| `test_agent_arg_parser.py` | positional/keyword 混合、类型转换、引号 |
| `test_agent_function_registry.py` | 加载 functions.py、注册、match、reload、签名解析 |
| `test_agent_code_runner.py` | AST 黑名单、用户 vs AI 严格度、exec 命名空间 |
| `test_agent_python_executor.py` | 同步/异步函数、异常捕获、返回值序列化、历史写入 |
| `test_agent_code_generator.py` | LLM 调用、代码块提取、func_name 提取、AST 校验 |
| `test_agent_templates.py` | 模板加载、应用 |
| `test_p4_cli_integration.py` | /agent 命令路由、通道路由、启用/禁用 |
| `test_p4_dod_scenarios.py` | DoD 关键场景验收 |

---

## 11. 完成定义 (DoD)

1. 在 `~/.opendracocli/functions.py` 定义 `deploy()`，输入 `deploy` 执行
2. 函数内 `ctx.shell("ls")` 正常调 shell 通道（含 P2 风控）
3. Python 通道执行结果写入共享历史
4. `/agent gen <intent>` → AI 生成函数 → yes 确认后执行
5. AI 生成代码含 `import subprocess` 时被 AST 拦截
6. `agent_enabled=False` 时函数名输入回退到 shell 通道（无回归）
7. 异步函数 + 同步函数都能正确执行
8. 所有测试通过，P1-P3 无回归

---

## 12. 后续演进

- **P4.1**: 函数依赖图（函数 A 调函数 B）
- **P4.2**: 函数参数类型校验（基于 type hint）
- **P5**: 启动画面 / 主题 / 插件市场
- **P6**: 多 Agent 任务编排
