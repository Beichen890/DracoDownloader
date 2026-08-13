# OpenDracoCLI

AI 时代的智能终端 — P1 跨平台 Shell 内核 + P2 安全与风控。

兼容 cmd / Linux 双语法自动映射，纯 subprocess 调原生 shell 执行，rich + prompt_toolkit TUI，SQLite 历史持久化。P2 通过 PreExec 钩子接入风控：四级风险评估（safe/caution/danger/critical）、yes 确认、PBKDF2 身份验证、路径白名单沙箱。预留事件总线供后续 P3/P4/P5 阶段接入。

## 安装

```bash
pip install -e .
```

## 运行

```bash
opendracocli                # 启动终端
opendracocli --setup-auth   # 设置 critical 级操作的身份验证密码
```

## 内置命令

```
/alias <name> <expansion>   添加/更新别名
/aliases                    列出所有别名
/history [N]                显示最近 N 条历史
/risk                       显示当前风险规则表
/risk test <command>        模拟评估命令风险等级（不执行）
/help                       显示帮助
/quit                       退出
```

## 阶段说明

- **P1（已完成）**：跨平台 Shell 内核 — 双语法映射 + 别名/宏 + subprocess 执行 + TUI + SQLite 历史
- **P2（已完成）**：安全与风控 — 四级风险评估 + yes 确认 + PBKDF2 身份验证 + 路径白名单沙箱
- P3：AI 智能层（纠错 + 角色化 + 感知自互动）
- P4：Agent 自动化（代码即行动 + Python 函数包装）
- P5：打磨（可定制启动界面 + 炫酷界面细化）

## 设计文档

- [P1 跨平台 Shell 内核设计](docs/2026-08-12-opendracocli-p1-kernel-design.md)
- [P2 安全与风控设计](docs/2026-08-12-opendracocli-p2-security-design.md)
- [P2-P5 落地路线图](docs/2026-08-12-opendracocli-落地路线图-P2-P5.md)
