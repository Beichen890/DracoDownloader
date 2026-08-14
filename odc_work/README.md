# OpenDracoCLI

AI 时代的智能终端 — P6 Rust 原生内核，全面放弃外部 shell，跨平台一致执行。

## 架构概览

```
┌─────────────────────────────────────────────┐
│              用户输入 / TUI                   │
├─────────────────────────────────────────────┤
│   解析器 → ComposeEngine（组合语义引擎）       │
│   管道 |  重定向 > >> <  逻辑 && || ;          │
├─────────────────────────────────────────────┤
│            NativeDispatcher（三层路由）        │
│  ┌─────────┐ ┌───────────┐ ┌──────────────┐ │
│  │Rust 内核│→│Draco 函数 │→│ exec 兜底    │ │
│  │(PyO3)   │ │(Python)   │ │(直接 exec)   │ │
│  └─────────┘ └───────────┘ └──────────────┘ │
├─────────────────────────────────────────────┤
│   安全层：四级风险 + 沙箱白名单 + 身份验证      │
├─────────────────────────────────────────────┤
│   事件总线 / SQLite 历史 / 别名 / 钩子         │
└─────────────────────────────────────────────┘
```

**核心设计**：不调用任何系统 shell（`/bin/sh`、`cmd.exe` 都不用），基础命令用 Rust 原生实现，复杂命令用 Python 标准库/第三方包实现，未覆盖命令直接 `exec` 程序。跨平台行为完全一致。

## 安装

### 方式一：从源码构建（含 Rust 编译）

```bash
# 1. 安装 Rust 工具链
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# 2. 安装 maturin（Rust-Python 混合构建工具）
pip install maturin

# 3. 克隆并构建
git clone https://github.com/Beichen890/OpenDracoCLI.git
cd OpenDracoCLI
maturin develop --release    # 开发模式，编译并安装到当前 Python

# 或构建 wheel 分发
maturin build --release      # 产出 wheel 到 target/wheels/
```

### 方式二：直接安装预编译 wheel

```bash
# 无需 Rust 工具链，wheel 已含编译产物（abi3，Py≥3.10 通用）
pip install opendracocli-0.1.0-cp310-abi3-manylinux_2_34_x86_64.whl
```

### 依赖

```
rich>=13.7.0          # 彩色输出
prompt_toolkit>=3.0   # 输入行（历史/补全/多行）
textual>=0.47,<0.50   # TUI 引擎
pygments>=2.16        # 语法高亮
```

## 启动

```bash
opendracocli                          # 默认启动（TTY 用 Textual TUI）
opendracocli --no-startup             # 跳过启动界面
opendracocli --renderer simple        # 用 prompt_toolkit 简单循环
opendracocli --renderer textual       # 强制 Textual TUI
opendracocli --theme dark             # 指定主题
opendracocli --setup-auth             # 设置 critical 级操作密码
```

非 TTY 环境（管道/脚本/MCP）自动降级到 `simple` 渲染器。

## 内置 Slash 命令

```
/alias <name> <expansion>    添加/更新别名
/aliases                     列出所有别名
/history [N]                 显示最近 N 条历史
/risk                        显示当前风险规则表
/risk test <command>         模拟评估命令风险等级（不执行）
/help                        显示帮助
/quit                        退出
```

## Rust 内核命令（16 个）

进程内调用，零 IPC 开销，跨平台行为一致：

| 命令 | 说明 | 示例 |
|------|------|------|
| `ls` | 列目录 | `ls -l -a *.py` |
| `cd` | 改变目录（透传 new_cwd） | `cd sub/dir` |
| `pwd` | 当前目录 | `pwd` |
| `cat` | 查看文件 | `cat a.py b.py` |
| `head` | 前几行 | `head -n 10 file` |
| `tail` | 后几行 | `tail -n 5 file` |
| `cp` | 复制 | `cp -r src dst` |
| `mv` | 移动/重命名 | `mv old new` |
| `rm` | 删除 | `rm -r dir` |
| `mkdir` | 创建目录 | `mkdir -p a/b/c` |
| `rmdir` | 删除空目录 | `rmdir emptydir` |
| `touch` | 创建/更新时间戳 | `touch newfile` |
| `stat` | 文件信息 | `stat a.py` |
| `ln` | 链接 | `ln -s target link` |
| `echo` | 输出 | `echo -n hello` |
| `wc` | 统计 | `echo hi \| wc` |

### ls 多路径与 glob

```bash
ls                          # 列当前目录
ls *.py                     # glob 展开后列出所有 .py
ls a.py b.py c.txt          # 多文件并列
ls sub1 sub2                # 多目录（每个前显 dir: 标题，空行分隔）
ls -l a.py                  # 单文件长格式
ls -a                       # 含隐藏文件
ls -h                       # 人类可读大小
```

## Draco 函数（Python 实现）

复杂命令用 Python 标准库/第三方包实现，跨平台无系统依赖：

### 文件操作

| 函数 | 说明 | 示例 |
|------|------|------|
| `ffind` | 按名/模式/时间查找 | `ffind pattern=*.py path=src` |
| `frename` | 批量重命名 | `frename pattern=*.old replace=.new` |
| `ftree` | 树形显示目录 | `ftree path=.` |

### 归档操作

| 函数 | 说明 | 示例 |
|------|------|------|
| `ftar` | tar 打包/解压 | `ftar out.tar "a.py b.py"` |
| `ftar` | gzip 压缩 | `ftar out.tar.gz src gzip=true` |
| `ftar` | 解压 | `ftar out.tar extract=true dest=./out` |
| `fzip` | zip 压缩/解压 | `fzip out.zip src` |

### 文本处理

| 函数 | 说明 | 示例 |
|------|------|------|
| `fgrep` | 文本搜索（替代 grep） | `fgrep TODO path=src line_numbers=true` |
| `fgrep` | 从 stdin 搜索 | `echo hello \| fgrep hel` |
| `fgrep` | 正则 + 忽略大小写 | `fgrep "^def " path=src regex=true` |
| `fhash` | 文件哈希 | `fhash file.txt algo=sha256` |
| `fjson` | JSON 查询（替代 jq） | `fjson data.json query=users.0.name` |
| `fjson` | 从 stdin 查询 | `echo '{"a":1}' \| fjson query=a` |

### 网络操作

| 函数 | 说明 | 示例 |
|------|------|------|
| `pcheck` | 端口检查 | `pcheck host=example.com port=443` |
| `http` | HTTP 请求 | `http url=https://api.example.com` |
| `dns` | DNS 解析 | `dns host=example.com` |

## 组合语义

自实现 shell 组合语义，不依赖系统 shell：

### 管道 `|`

上游 stdout 作为下游 stdin（内存传递）：

```bash
echo hello world | wc              # 统计词数
echo piped | cat                   # 透传
cat a.py | fgrep def               # 搜索文件内容
echo '{"k":42}' | fjson query=k    # 管道查询 JSON
```

### 重定向

```bash
echo hello > out.txt               # 覆盖写入
echo more >> out.txt               # 追加
cat < input.txt                    # 从文件读 stdin
echo err 2> error.log              # stderr 重定向
```

### 逻辑操作符

```bash
echo first && echo second          # 上游成功才执行下游
false_xyz || echo fallback         # 上游失败才执行下游
echo a ; echo b                    # 顺序执行（无论成败）
mkdir dir && cd dir                # 组合：创建后进入
```

### glob 展开

```bash
ls *.py                            # 展开为 ls a.py b.py
cat *.txt                          # 拼接所有 txt
rm *.tmp                           # 删除所有 tmp
```

无匹配时保留字面量（由命令自行报错）。

## 安全特性

### 四级风险评估

| 级别 | 说明 | 处理 |
|------|------|------|
| safe | 读操作、ls 等 | 直接执行 |
| caution | 写操作、网络 | yes 确认 |
| danger | rm -rf、格式化 | yes 确认 + 警告 |
| critical | 系统级危险操作 | PBKDF2 身份验证 |

### 路径白名单沙箱

写操作路径必须在白名单内，否则拦截：

```bash
/risk                    # 查看规则
/risk test rm -rf /      # 模拟评估（不执行）
```

### 身份验证

```bash
opendracocli --setup-auth    # 设置密码（PBKDF2 存储）
```

critical 级操作需输入密码确认。

## 会话管理

- **历史持久化**：所有命令记录到 SQLite，`/history` 查看
- **别名**：`/alias ll=ls -la`，后续 `ll` 自动展开
- **CWD 跟踪**：`cd` 后续命令在新目录执行（session 级）
- **事件总线**：钩子可订阅 `EVT_COMMAND_EXECUTED` 等事件

## 部署到服务器（无 Rust 工具链）

内存受限的服务器（如 1GB VPS）可避免本地编译：

```bash
# 1. 在开发机构建 wheel
maturin build --release
# 产出：target/wheels/opendracocli-0.1.0-cp310-abi3-manylinux_2_34_x86_64.whl

# 2. 上传到服务器
scp target/wheels/*.whl server:/tmp/

# 3. 服务器安装（秒级，零编译）
python3 -m venv /opt/odc.venv
/opt/odc.venv/bin/pip install /tmp/opendracocli-*.whl

# 4. 启动
/opt/odc.venv/bin/opendracocli
```

abi3 wheel 兼容 Python ≥3.10，manylinux_2_34 兼容主流 Linux 发行版。

## 阶段演进

- **P1（已完成）**：跨平台 Shell 内核
- **P2（已完成）**：安全与风控
- **P3（已完成）**：AI 智能层（纠错 + 角色化 + 感知）
- **P4（已完成）**：Agent 自动化（Python 函数包装）
- **P5（已完成）**：打磨（启动界面 + Textual TUI）
- **P6（当前）**：Rust 原生内核 — 放弃外部 shell，三层路由 + 组合语义引擎

## 设计文档

- [P1 跨平台 Shell 内核设计](docs/2026-08-12-opendracocli-p1-kernel-design.md)
- [P2 安全与风控设计](docs/2026-08-12-opendracocli-p2-security-design.md)
- [P2-P5 落地路线图](docs/2026-08-12-opendracocli-落地路线图-P2-P5.md)

## 许可证

MIT
