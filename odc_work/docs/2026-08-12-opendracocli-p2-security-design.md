# OpenDracoCLI — P2 安全与风控 设计文档

- **日期**: 2026-08-12
- **阶段**: P2（5 阶段拆分的第二阶段）
- **状态**: 待评审
- **前置**: P1 跨平台 Shell 内核已完成
- **关联**: [P1 设计文档](./2026-08-12-opendracocli-p1-kernel-design.md)、[落地路线图](./2026-08-12-opendracocli-落地路线图-P2-P5.md)

---

## 1. 目标

1. **沙箱隔离**：高危命令在受限环境执行（限制可写路径）
2. **yes 确认**：高危命令执行前需用户输入 yes 确认
3. **身份验证**：极高危操作（如删除系统目录）需验证身份（密码）
4. **风险可配置**：风险规则可通过 `rules.json` 自定义扩展

## 2. 非目标（推迟）

- 真正的 unshare/chroot 系统级沙箱（留 P2.1）
- 网络隔离（留 P2.1）
- 生物特征验证（Windows Hello / Touch ID，留 P3+）
- AI 辅助风险判断（P3 AI 层）

---

## 3. 架构

### 3.1 接入方式

P2 通过 P1 预留的 `PreExecHook` 扩展点接入，**不改动内核管线**：

```
用户输入 → P1 管线 (parse → normalize → alias → map)
  → PreExec 钩子链
      → RiskAssessmentHook (P2 新增, priority=10, 最先执行)
          → assess(ctx.ir) → RiskLevel
          → safe:           CONTINUE
          → caution:        confirmer.ask_yes() → CONTINUE / BLOCK
          → danger:         confirmer.ask_yes() → CONTINUE (标记沙箱) / BLOCK
          → critical:       authenticator.verify() + confirmer.ask_yes()
                            → CONTINUE (标记沙箱) / BLOCK
  → SubprocessExecutor (若 danger/critical, 经 SandboxExecutor 包装)
  → PostExec → 事件 → 历史
```

### 3.2 关键决策

**决策 1：风险评级在别名展开+平台映射之后。**
风险评评估作用于最终的 IR（含已展开的别名、已映射的平台命令）。否则别名 `myclean = rm -rf /tmp/*` 的风险会被别名名掩盖。

**决策 2：沙箱通过包装 Executor 实现，不改 pipeline。**
新增 `SandboxExecutor` 实现 `Executor` 协议，内部包装 `SubprocessExecutor`。`RiskAssessmentHook` 在 `ctx.metadata` 标记需要沙箱，pipeline 根据 metadata 选择 executor。这样沙箱能力可插拔，P1 pipeline 不动。

**决策 3：身份验证用密码哈希，不存明文。**
密码经 PBKDF2-HMAC-SHA256 哈希存储，加盐。首次 `opendracocli --setup-auth` 设置。

**决策 4：风险规则表与代码分离。**
`rules.json` 声明式定义规则，用户可扩展，无需改代码。

---

## 4. 模块拆分

```
opendracocli/
├── security/
│   ├── __init__.py
│   ├── risk_assessor.py     # 风险评估器
│   ├── rules.json           # 风险规则表（声明式）
│   ├── confirmer.py         # yes 确认交互
│   ├── authenticator.py     # 身份验证（密码哈希）
│   └── sandbox.py           # SandboxExecutor (路径白名单)
├── hooks/
│   └── risk_hook.py         # RiskAssessmentHook (PreExecHook)
```

### 4.1 各模块职责

| 模块 | 职责 |
|------|------|
| `risk_assessor.py` | 输入 IR → 输出 `RiskAssessment(level, matched_rules, reason)` |
| `rules.json` | 声明式规则表：cmd + args_contain 模式匹配 |
| `confirmer.py` | `ask_yes(prompt) -> bool`，prompt_toolkit 隐藏输入，支持测试注入 |
| `authenticator.py` | `verify() -> bool`，密码哈希比对；`set_password()`；首次设置向导 |
| `sandbox.py` | `SandboxExecutor` 实现 Executor 协议，执行前检查目标路径是否在白名单 |
| `risk_hook.py` | `RiskAssessmentHook(PreExecHook)`，编排 assess → confirm → auth → sandbox |

---

## 5. 风险分级

| 等级 | 触发条件（示例） | 处理流程 |
|------|------------------|----------|
| `safe` | 默认；`ls`, `cat`, `echo`, `pwd` | 直接执行 |
| `caution` | `rm`（单文件）, `mv`（可能覆盖）, `kill` | yes 确认 |
| `danger` | `rm -rf`, `format`, `chmod 777` | yes 确认 + 沙箱 |
| `critical` | `rm -rf /`, `mkfs`, `dd of=/dev/`, 写系统目录 | 身份验证 + yes 确认 + 沙箱 |

### 5.1 规则匹配逻辑

```
对 IR 中每个 CommandNode:
  遍历 rules.json 中所有等级的 patterns:
    if pattern.cmd == node.name (大小写不敏感):
      if pattern.args_contain 为空: 匹配（命令名命中即匹配）
      elif pattern.args_contain 中任一 token 在 node.args 中: 匹配
  取命中的最高等级作为该节点的风险等级
整个 IR 的风险等级 = 所有节点中的最高等级
```

### 5.2 rules.json 结构

```json
{
  "_comment": "风险规则表。等级从高到低: critical > danger > caution。匹配取最高等级。",
  "critical": {
    "patterns": [
      {"cmd": "rm", "args_contain": ["-rf", "/"], "desc": "删除根目录"},
      {"cmd": "rm", "args_contain": ["-rf", "/"], "desc": "递归强删根"},
      {"cmd": "rm", "args_contain": ["-fr", "/"], "desc": "递归强删根"},
      {"cmd": "mkfs", "desc": "格式化文件系统"},
      {"cmd": "dd", "args_contain": ["of=/dev/"], "desc": "写裸设备"},
      {"cmd": "shutdown", "desc": "关机"},
      {"cmd": "reboot", "desc": "重启"}
    ]
  },
  "danger": {
    "patterns": [
      {"cmd": "rm", "args_contain": ["-rf"], "desc": "递归强删"},
      {"cmd": "rm", "args_contain": ["-fr"], "desc": "递归强删"},
      {"cmd": "rmdir", "args_contain": ["/s"], "desc": "Windows 递归删目录"},
      {"cmd": "format", "desc": "格式化"},
      {"cmd": "chmod", "args_contain": ["777"], "desc": "全权限"},
      {"cmd": "chown", "desc": "改所有者"},
      {"cmd": "kill", "args_contain": ["-9"], "desc": "强杀进程"},
      {"cmd": "taskkill", "args_contain": ["/f"], "desc": "Windows 强杀"}
    ]
  },
  "caution": {
    "patterns": [
      {"cmd": "rm", "desc": "删除文件"},
      {"cmd": "del", "desc": "删除文件"},
      {"cmd": "mv", "desc": "移动/覆盖"},
      {"cmd": "move", "desc": "移动/覆盖"},
      {"cmd": "kill", "desc": "杀进程"},
      {"cmd": "taskkill", "desc": "杀进程"},
      {"cmd": "cp", "args_contain": ["-f"], "desc": "强制覆盖复制"},
      {"cmd": "copy", "args_contain": ["/y"], "desc": "Windows 强制覆盖"}
    ]
  }
}
```

---

## 6. 数据模型

### 6.1 RiskAssessment

```python
@dataclass
class RiskAssessment:
    level: RiskLevel              # SAFE / CAUTION / DANGER / CRITICAL
    matched_rules: list[MatchedRule]  # 命中的规则列表
    reason: str                   # 人类可读的汇总原因

@dataclass
class MatchedRule:
    node_index: int               # 命中的 IR 节点序号
    cmd: str
    level: RiskLevel
    desc: str

class RiskLevel(Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGER = "danger"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return {"safe": 0, "caution": 1, "danger": 2, "critical": 3}[self.value]
```

### 6.2 ExecContext.metadata 扩展

P2 在 `ctx.metadata` 中写入沙箱标记，pipeline 据此选择 executor：

```python
ctx.metadata["risk_level"] = "danger"
ctx.metadata["use_sandbox"] = True
ctx.metadata["sandbox_writable_paths"] = ["/tmp", "~"]
```

---

## 7. 各模块详细设计

### 7.1 RiskAssessor (risk_assessor.py)

```python
class RiskAssessor:
    def __init__(self, rules_path: Path | None = None): ...
    def assess(self, ir: CommandIR) -> RiskAssessment: ...
```

- 加载 `rules.json`（包内默认 + 用户 `~/.opendracocli/rules.json` 覆盖扩展）
- 对 IR 每个节点匹配规则，取最高等级
- 未命中任何规则 → SAFE

### 7.2 Confirmer (confirmer.py)

```python
class Confirmer:
    async def ask_yes(self, prompt: str, default: bool = False) -> bool: ...
```

- 用 prompt_toolkit 异步输入（支持 y/n/yes/no）
- 测试可通过注入回调函数绕过交互
- `default` 决定空回车的默认值

### 7.3 Authenticator (authenticator.py)

```python
class Authenticator:
    def __init__(self, hash_path: Path): ...
    def is_configured(self) -> bool: ...
    def set_password(self, password: str) -> None: ...
    async def verify(self) -> bool: ...
```

- PBKDF2-HMAC-SHA256，迭代 100000 次，随机盐
- 哈希文件 `~/.opendracocli/auth.hash`，格式 `pbkdf2$<iterations>$<salt_hex>$<hash_hex>`
- `verify()` 用 prompt_toolkit 隐藏输入密码，比对哈希
- 未配置时（首次）：critical 命令一律 BLOCK，提示用户先 `--setup-auth`

### 7.4 SandboxExecutor (sandbox.py)

```python
class SandboxExecutor:
    def __init__(self, inner: SubprocessExecutor, writable_paths: list[str]): ...
    async def execute(self, ir, *, cwd=None, timeout=None) -> ExecResult: ...
```

- 实现 `Executor` 协议，内部包装 `SubprocessExecutor`
- 执行前检查 IR 中所有重定向目标 + 显式路径参数是否在可写白名单内
- 违规 → 直接返回失败 `ExecResult(exit_code=-1, stderr="draco.sandbox.violation: ...")`，不调 subprocess
- 路径展开：`~` → home，相对路径 → 相对 cwd
- 白名单匹配：目标路径是否在任一白名单目录下（`Path.resolve()` 后 `is_relative_to`）

### 7.5 RiskAssessmentHook (risk_hook.py)

```python
class RiskAssessmentHook(PreExecHook):
    priority = 10  # 最先执行

    def __init__(self, assessor, confirmer, authenticator, config): ...

    async def pre_exec(self, ctx) -> tuple[ExecDecision, BlockResult | None]:
        assessment = self._assessor.assess(ctx.ir)
        ctx.metadata["risk_level"] = assessment.level.value

        if assessment.level == RiskLevel.SAFE:
            return CONTINUE, None

        if assessment.level == RiskLevel.CAUTION:
            ok = await self._confirmer.ask_yes(self._prompt(assessment))
            return (CONTINUE, None) if ok else (BLOCK, BlockResult(reason="用户拒绝", code=ERR_RISK_DENIED))

        if assessment.level == RiskLevel.DANGER:
            ok = await self._confirmer.ask_yes(self._prompt(assessment))
            if not ok:
                return BLOCK, BlockResult(reason="用户拒绝", code=ERR_RISK_DENIED)
            ctx.metadata["use_sandbox"] = True
            return CONTINUE, None

        # CRITICAL
        if not self._authenticator.is_configured():
            return BLOCK, BlockResult(reason="需先设置密码 (opendracocli --setup-auth)", code=ERR_AUTH_FAILED)
        if not await self._authenticator.verify():
            return BLOCK, BlockResult(reason="身份验证失败", code=ERR_AUTH_FAILED)
        ok = await self._confirmer.ask_yes(self._prompt(assessment))
        if not ok:
            return BLOCK, BlockResult(reason="用户拒绝", code=ERR_RISK_DENIED)
        ctx.metadata["use_sandbox"] = True
        return CONTINUE, None
```

### 7.6 Pipeline 改动（最小化）

`shell/pipeline.py` 的 `run()` 方法在 pre_exec 之后，根据 `ctx.metadata.get("use_sandbox")` 选择 executor：

```python
# 伪代码
if ctx.metadata.get("use_sandbox"):
    executor = self._sandbox_executor
else:
    executor = self._executor
exec_result = await executor.execute(ctx.ir, ...)
```

同时 `result.is_high_risk = ctx.metadata.get("risk_level") in ("danger", "critical")`，写入历史。

---

## 8. 配置扩展 (config.py)

```python
# P2 追加字段
security_rules_file: str = "~/.opendracocli/rules.json"  # 用户规则覆盖
auth_hash_file: str = "~/.opendracocli/auth.hash"
sandbox_writable_paths: list[str] = field(default_factory=lambda: ["~", "/tmp"])
require_auth_for: str = "critical"  # critical / danger / off
```

`DRACO_SANDBOX_WRITABLE_PATHS` 环境变量用 `:` 分隔（Unix）/ `;` 分隔（Windows）。

---

## 9. 错误码扩展 (errors.py)

```python
ERR_RISK_BLOCKED = "draco.risk.blocked"      # 风控拦截（通用）
ERR_RISK_DENIED = "draco.risk.denied"        # 用户拒绝 yes
ERR_AUTH_FAILED = "draco.auth.failed"        # 身份验证失败
ERR_SANDBOX_VIOLATION = "draco.sandbox.violation"  # 沙箱路径违规
```

---

## 10. 事件扩展 (events.py)

```python
EVT_RISK_BLOCKED = "RiskBlocked"     # 高危命令被拦截
EVT_AUTH_REQUIRED = "AuthRequired"   # 触发身份验证
EVT_SANDBOX_VIOLATION = "SandboxViolation"  # 沙箱拦截
```

`RiskAssessmentHook` 在 BLOCK 时发布对应事件。

---

## 11. CLI 扩展

### 11.1 `--setup-auth` 子命令

```bash
opendracocli --setup-auth
# 交互式设置密码（输入两次确认）
```

### 11.2 TUI 渲染

- yes 确认提示用 rich 黄色面板
- 身份验证用 prompt_toolkit 隐藏输入
- 阻断结果用红色面板（P1 已有 `_render_block`）
- 沙箱违规用红色警告

### 11.3 `/risk` slash 命令

```
/risk                    # 显示当前规则表
/risk test <command>     # 模拟评估某命令的风险等级（不执行）
```

---

## 12. 测试策略

| 测试文件 | 覆盖范围 |
|----------|----------|
| `test_risk_assessor.py` | 各等级规则匹配/不匹配、多节点取最高、用户规则覆盖扩展 |
| `test_confirmer.py` | yes/no 输入、默认值、回调注入 |
| `test_authenticator.py` | 密码设置→验证 round-trip、错误密码、未配置场景 |
| `test_sandbox.py` | 路径白名单匹配/违规、`~` 展开、相对路径、重定向目标检查 |
| `test_risk_hook.py` | 四级流程集成（mock confirmer/authenticator）、metadata 标记、事件发布 |
| `test_pipeline_security.py` | 端到端：`rm -rf /tmp/test` 触发确认、`rm` 单文件触发确认、safe 直接执行 |

**关键测试场景**：
- `rm -rf /` → critical → 未配置密码 → BLOCK + 提示 setup-auth
- `rm -rf /` → critical → 已配置密码 → 密码错误 → BLOCK
- `rm -rf /` → critical → 密码正确 + yes → CONTINUE + 沙箱
- `rm file.txt` → caution → yes → CONTINUE（无沙箱）
- `rm file.txt` → caution → no → BLOCK
- `echo hi` → safe → 直接执行
- 沙箱：`echo x > /etc/test` → 路径违规 → 失败

---

## 13. 完成定义 (Definition of Done)

1. `rm -rf /tmp/test` 触发 yes 确认，输入 no 则 BLOCK，历史记录 `is_high_risk=True`
2. `rm -rf /` 触发身份验证；未配置密码时 BLOCK 并提示 `--setup-auth`
3. 配置密码后，`rm -rf /` 密码错误则 BLOCK，密码正确 + yes 则执行（经沙箱）
4. 沙箱拦截对 `/etc/` 的写操作（`echo x > /etc/foo` 失败）
5. `echo hi` 不触发任何确认，直接执行
6. 风险规则可通过 `~/.opendracocli/rules.json` 自定义扩展
7. `/risk test <cmd>` 能预览风险等级
8. 全部测试通过，P1 的 71 个测试无回归

---

## 14. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 路径白名单沙箱可被绕过（如 `cd /etc && echo x > foo`） | P2 接受这一限制，真正系统级沙箱留 P2.1；文档明确说明 |
| 规则匹配误报（`rm` 匹配所有 rm） | caution 级别只是确认，不阻断；用户可 no 跳过 |
| 密码哈希文件权限泄露 | 创建时 `chmod 600`，仅 owner 可读 |
| 跨平台路径差异（`/etc` vs `C:\Windows`） | 白名单用 `Path.resolve()` 统一处理；Windows 系统目录加入默认禁止列表 |
