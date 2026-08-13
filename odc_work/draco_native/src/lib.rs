//! Draco 原生内核 — PyO3 绑定层
//!
//! 暴露给 Python 的扩展模块 `opendracocli._native`：
//! - execute(cmd, args, cwd, stdin) -> CommandResult  统一执行入口
//! - supported_commands() -> list[str]  内核支持的命令清单
//!
//! Rust 内部用 match 分发到 fs_cmds 的实现。
//! 命令未命中返回 exit_code=127（与 shell 一致），由上层 NativeDispatcher 决定走 Draco 函数或 exec 兜底。

use pyo3::prelude::*;

mod fs_cmds;

/// 命令执行结果（映射到 Python 的 opendracocli._native.CommandResult）
#[pyclass(get_all)]
struct CommandResult {
    /// 进程退出码（0 成功）
    exit_code: i32,
    /// 标准输出
    stdout: String,
    /// 标准错误
    stderr: String,
    /// cd 等命令返回的新工作目录（绝对路径）；None 表示不变
    new_cwd: Option<String>,
}

/// 统一执行入口：按命令名分发到 Rust 实现
///
/// Args:
///     cmd: 命令名（已归一化小写）
///     args: 参数列表（glob 已在 Python 侧展开）
///     cwd: 当前工作目录（绝对路径）
///     stdin: 上游管道输入（无则为 None）
///
/// Returns:
///     CommandResult；未覆盖命令 exit_code=127
#[pyfunction(signature = (cmd, args, cwd, stdin=None))]
fn execute(cmd: &str, args: Vec<String>, cwd: &str, stdin: Option<String>) -> CommandResult {
    let stdin_ref = stdin.as_deref();
    let out = match cmd {
        "ls" => fs_cmds::ls(&args, cwd),
        "cd" => fs_cmds::cd(&args, cwd),
        "pwd" => fs_cmds::pwd(&args, cwd),
        "cat" => fs_cmds::cat(&args, cwd, stdin_ref),
        "head" => fs_cmds::head(&args, cwd, stdin_ref),
        "tail" => fs_cmds::tail(&args, cwd, stdin_ref),
        "cp" => fs_cmds::cp(&args, cwd),
        "mv" => fs_cmds::mv(&args, cwd),
        "rm" => fs_cmds::rm(&args, cwd),
        "mkdir" => fs_cmds::mkdir(&args, cwd),
        "rmdir" => fs_cmds::rmdir(&args, cwd),
        "touch" => fs_cmds::touch(&args, cwd),
        "stat" => fs_cmds::stat(&args, cwd),
        "ln" => fs_cmds::ln(&args, cwd),
        "echo" => fs_cmds::echo(&args, cwd, stdin_ref),
        "wc" => fs_cmds::wc(&args, cwd, stdin_ref),
        _ => {
            return CommandResult {
                exit_code: 127,
                stdout: String::new(),
                stderr: format!("draco_native: {}: 命令未在内核中实现", cmd),
                new_cwd: None,
            }
        }
    };
    CommandResult {
        exit_code: out.exit_code,
        stdout: out.stdout,
        stderr: out.stderr,
        new_cwd: out.new_cwd,
    }
}

/// 返回内核支持的命令清单（供 NativeDispatcher 判断是否走 Rust 通道）
#[pyfunction]
fn supported_commands() -> Vec<String> {
    vec![
        "ls", "cd", "pwd", "cat", "head", "tail", "cp", "mv", "rm",
        "mkdir", "rmdir", "touch", "stat", "ln", "echo", "wc",
    ]
    .into_iter()
    .map(String::from)
    .collect()
}

/// Python 模块入口：opendracocli._native
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(execute, m)?)?;
    m.add_function(wrap_pyfunction!(supported_commands, m)?)?;
    m.add_class::<CommandResult>()?;
    Ok(())
}
