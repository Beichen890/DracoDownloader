//! 文件系统命令实现（纯 Rust，不依赖 pyo3）
//!
//! 设计原则：
//! - 大文件读取用 memmap2 内存映射（零拷贝）
//! - 跨平台路径用 PathBuf，不硬编码分隔符
//! - 错误转 (exit_code, stderr)，不 panic
//! - 命令语义对齐 GNU coreutils 常用子集，Windows/Linux 行为一致

use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use memmap2::Mmap;

/// 命令执行输出（Rust 原生，lib.rs 负责转 PyO3）
pub struct CmdOut {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    /// cd 等改 cwd 的命令返回新工作目录（绝对路径）
    pub new_cwd: Option<String>,
}

impl CmdOut {
    fn ok(stdout: String) -> Self {
        Self { exit_code: 0, stdout, stderr: String::new(), new_cwd: None }
    }
    fn ok_stream() -> Self {
        Self::ok(String::new())
    }
    fn err(msg: String) -> Self {
        Self { exit_code: 1, stdout: String::new(), stderr: msg, new_cwd: None }
    }
}

/// 解析相对路径为绝对路径（基于 cwd）
fn resolve(cwd: &str, p: &str) -> PathBuf {
    let path = Path::new(p);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        Path::new(cwd).join(path)
    }
}

/// 人类可读大小（1024 进制）
fn human_size(n: u64) -> String {
    const UNITS: &[&str] = &["B", "K", "M", "G", "T", "P"];
    if n < 1024 {
        return format!("{}", n);
    }
    let mut size = n as f64;
    let mut idx = 0;
    while size >= 1024.0 && idx < UNITS.len() - 1 {
        size /= 1024.0;
        idx += 1;
    }
    format!("{:.1}{}", size, UNITS[idx])
}

/// 拆分 flags 与位置参数：- 开头的归 flags，其余归 positional
fn split_args(args: &[String]) -> (Vec<char>, Vec<String>) {
    let mut flags = Vec::new();
    let mut pos = Vec::new();
    for a in args {
        if let Some(rest) = a.strip_prefix('-') {
            if a == "--" {
                continue;
            }
            for c in rest.chars() {
                flags.push(c);
            }
        } else {
            pos.push(a.clone());
        }
    }
    (flags, pos)
}

// ============================================================
// ls — 列目录（支持多路径，glob 展开后的多文件一并显示）
// ============================================================
pub fn ls(args: &[String], cwd: &str) -> CmdOut {
    let (flags, pos) = split_args(args);
    let long = flags.contains(&'l');
    let all = flags.contains(&'a') || flags.contains(&'A');
    let human = flags.contains(&'h');

    // 无位置参数：列出 cwd
    if pos.is_empty() {
        return ls_one_dir(cwd, cwd, long, all, human);
    }

    // 单一位置参数：目录列内容，文件单列其名（对齐 GNU 行为）
    if pos.len() == 1 {
        let target = &pos[0];
        let p = resolve(cwd, target);
        if !p.exists() {
            return CmdOut::err(format!("ls: {}: 没有那个文件或目录\n", target));
        }
        if p.is_dir() {
            return ls_one_dir(target, cwd, long, all, human);
        }
        // 文件：-l 显示长格式，否则单列文件名
        let mut out = String::new();
        if long {
            if let Ok(m) = fs::metadata(&p) {
                let size = m.len();
                let s = if human { human_size(size) } else { size.to_string() };
                out.push_str(&format!("- {:>10} {}\n", s, target));
            }
        } else {
            out.push_str(target);
            out.push('\n');
        }
        return CmdOut::ok(out);
    }

    // 多个位置参数：目录前显示标题，目录与其它目标之间补空行（对齐 GNU ls）
    let mut out = String::new();
    let mut err = String::new();
    let mut first = true;
    let mut prev_is_dir = false;
    for target in &pos {
        let p = resolve(cwd, target);
        let exists = p.exists();
        let is_dir = exists && p.is_dir();
        // 目录与任意目标之间补空行（GNU 行为）
        if !first && (is_dir || prev_is_dir) {
            out.push('\n');
        }
        first = false;
        prev_is_dir = is_dir;

        if !exists {
            err.push_str(&format!("ls: {}: 没有那个文件或目录\n", target));
            continue;
        }
        if is_dir {
            out.push_str(&format!("{}:\n", target));
            match ls_one_dir(target, cwd, long, all, human) {
                CmdOut { exit_code: 0, stdout, .. } => out.push_str(&stdout),
                CmdOut { stderr: sub_err, .. } => {
                    err.push_str(&sub_err);
                }
            }
        } else {
            // 文件：直接列名（-l 时显示该文件信息）
            if long {
                match fs::metadata(&p) {
                    Ok(m) => {
                        let size = m.len();
                        let s = if human { human_size(size) } else { size.to_string() };
                        out.push_str(&format!("- {:>10} {}\n", s, target));
                    }
                    Err(e) => {
                        err.push_str(&format!("ls: {}: {}\n", target, e));
                    }
                }
            } else {
                out.push_str(target);
                out.push('\n');
            }
        }
    }
    if !err.is_empty() {
        CmdOut {
            exit_code: 2,
            stdout: out,
            stderr: err,
            new_cwd: None,
        }
    } else {
        CmdOut::ok(out)
    }
}

/// 列出单个目录的内容（不显示标题）
fn ls_one_dir(target: &str, cwd: &str, long: bool, all: bool, human: bool) -> CmdOut {
    let dir = resolve(cwd, target);
    let entries = match fs::read_dir(&dir) {
        Ok(e) => e,
        Err(e) => return CmdOut::err(format!("ls: {}: {}\n", target, e)),
    };

    let mut items: Vec<(String, bool, u64)> = Vec::new();
    for ent in entries.flatten() {
        let name = ent.file_name().to_string_lossy().to_string();
        if !all && name.starts_with('.') {
            continue;
        }
        let is_dir = ent.file_type().map(|t| t.is_dir()).unwrap_or(false);
        let size = ent.metadata().map(|m| m.len()).unwrap_or(0);
        items.push((name, is_dir, size));
    }
    items.sort_by(|a, b| a.0.cmp(&b.0));

    let mut out = String::new();
    if long {
        for (name, is_dir, size) in &items {
            let t = if *is_dir { 'd' } else { '-' };
            let s = if human { human_size(*size) } else { size.to_string() };
            out.push_str(&format!("{} {:>10} {}\n", t, s, name));
        }
    } else {
        // 紧凑：目录名加 /
        let mut line = Vec::new();
        for (name, is_dir, _) in &items {
            let n = if *is_dir { format!("{}/", name) } else { name.clone() };
            line.push(n);
        }
        out = line.join("  ");
        if !out.is_empty() {
            out.push('\n');
        }
    }
    CmdOut::ok(out)
}

// ============================================================
// cd — 改变工作目录（返回 new_cwd）
// ============================================================
pub fn cd(args: &[String], cwd: &str) -> CmdOut {
    let target = if args.is_empty() {
        // 无参数：回 home
        match std::env::var("HOME").or_else(|_| std::env::var("USERPROFILE")) {
            Ok(h) => h,
            Err(_) => return CmdOut::err("cd: 无法确定 HOME 目录".into()),
        }
    } else {
        args[0].clone()
    };
    let dir = resolve(cwd, &target);
    let canon = match fs::canonicalize(&dir) {
        Ok(p) => p,
        Err(e) => return CmdOut::err(format!("cd: {}: {}", target, e)),
    };
    if !canon.is_dir() {
        return CmdOut::err(format!("cd: {}: 不是目录", target));
    }
    CmdOut {
        exit_code: 0,
        stdout: canon.to_string_lossy().to_string(),
        stderr: String::new(),
        new_cwd: Some(canon.to_string_lossy().to_string()),
    }
}

// ============================================================
// pwd — 打印工作目录
// ============================================================
pub fn pwd(_args: &[String], cwd: &str) -> CmdOut {
    CmdOut::ok(format!("{}\n", cwd))
}

// ============================================================
// cat — 读文件（大文件用 mmap 零拷贝）；无参数时透传 stdin
// ============================================================
pub fn cat(args: &[String], cwd: &str, stdin: Option<&str>) -> CmdOut {
    let (_, pos) = split_args(args);
    if pos.is_empty() {
        return CmdOut::ok(stdin.unwrap_or("").to_string());
    }
    let mut out = String::new();
    for f in &pos {
        let path = resolve(cwd, f);
        if path.is_dir() {
            out.push_str(&format!("cat: {}: 是目录\n", f));
            continue;
        }
        match read_file_mmap(&path) {
            Ok(bytes) => out.push_str(&String::from_utf8_lossy(&bytes)),
            Err(e) => out.push_str(&format!("cat: {}: {}\n", f, e)),
        }
    }
    if out.is_empty() {
        CmdOut::ok_stream()
    } else {
        CmdOut::ok(out)
    }
}

/// 内存映射读文件（大文件零拷贝）；失败回退到普通读
fn read_file_mmap(path: &Path) -> std::io::Result<Vec<u8>> {
    let file = File::open(path)?;
    let meta = file.metadata()?;
    if meta.len() > 64 * 1024 {
        // 大文件：mmap 零拷贝
        match unsafe { Mmap::map(&file) } {
            Ok(m) => Ok(m.to_vec()),
            Err(_) => {
                // mmap 失败（如特殊文件/管道），回退普通读
                let mut buf = Vec::new();
                File::open(path)?.read_to_end(&mut buf)?;
                Ok(buf)
            }
        }
    } else {
        let mut buf = Vec::new();
        File::open(path)?.read_to_end(&mut buf)?;
        Ok(buf)
    }
}

// ============================================================
// head — 前 N 行（默认 10）
// ============================================================
pub fn head(args: &[String], cwd: &str, stdin: Option<&str>) -> CmdOut {
    let (n, files) = parse_head_tail_n(args, 10);
    let mut out = String::new();
    if files.is_empty() {
        let s = stdin.unwrap_or("");
        for line in s.lines().take(n) {
            out.push_str(line);
            out.push('\n');
        }
        return CmdOut::ok(out);
    }
    for (i, f) in files.iter().enumerate() {
        if files.len() > 1 {
            if i > 0 {
                out.push('\n');
            }
            out.push_str(&format!("==> {} <==\n", f));
        }
        let path = resolve(cwd, f);
        match read_file_mmap(&path) {
            Ok(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                for line in text.lines().take(n) {
                    out.push_str(line);
                    out.push('\n');
                }
            }
            Err(e) => out.push_str(&format!("head: {}: {}\n", f, e)),
        }
    }
    CmdOut::ok(out)
}

// ============================================================
// tail — 后 N 行（默认 10）
// ============================================================
pub fn tail(args: &[String], cwd: &str, stdin: Option<&str>) -> CmdOut {
    let (n, files) = parse_head_tail_n(args, 10);
    let mut out = String::new();
    if files.is_empty() {
        let s = stdin.unwrap_or("");
        let lines: Vec<&str> = s.lines().collect();
        let start = if lines.len() > n { lines.len() - n } else { 0 };
        for line in &lines[start..] {
            out.push_str(line);
            out.push('\n');
        }
        return CmdOut::ok(out);
    }
    for (i, f) in files.iter().enumerate() {
        if files.len() > 1 {
            if i > 0 {
                out.push('\n');
            }
            out.push_str(&format!("==> {} <==\n", f));
        }
        let path = resolve(cwd, f);
        match read_file_mmap(&path) {
            Ok(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                let lines: Vec<&str> = text.lines().collect();
                let start = if lines.len() > n { lines.len() - n } else { 0 };
                for line in &lines[start..] {
                    out.push_str(line);
                    out.push('\n');
                }
            }
            Err(e) => out.push_str(&format!("tail: {}: {}\n", f, e)),
        }
    }
    CmdOut::ok(out)
}

/// 解析 -n NUM / -NUM 形式的行数参数
fn parse_head_tail_n(args: &[String], default: usize) -> (usize, Vec<String>) {
    let mut n = default;
    let mut files = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "-n" && i + 1 < args.len() {
            if let Ok(v) = args[i + 1].parse::<usize>() {
                n = v;
            }
            i += 2;
            continue;
        }
        if let Some(rest) = a.strip_prefix('-') {
            if let Ok(v) = rest.parse::<usize>() {
                n = v;
                i += 1;
                continue;
            }
        }
        files.push(a.clone());
        i += 1;
    }
    (n, files)
}

// ============================================================
// cp — 复制文件/目录
// ============================================================
pub fn cp(args: &[String], cwd: &str) -> CmdOut {
    let (flags, pos) = split_args(args);
    let recursive = flags.contains(&'r') || flags.contains(&'R');
    if pos.len() < 2 {
        return CmdOut::err("cp: 用法: cp [-r] 源 目标".into());
    }
    let dst = resolve(cwd, pos.last().unwrap());
    let is_dst_dir = dst.is_dir();
    for src in &pos[..pos.len() - 1] {
        let sp = resolve(cwd, src);
        let target = if is_dst_dir {
            dst.join(sp.file_name().unwrap_or_default())
        } else {
            dst.clone()
        };
        if let Err(e) = copy_one(&sp, &target, recursive) {
            return CmdOut::err(format!("cp: {}: {}", src, e));
        }
    }
    CmdOut::ok_stream()
}

fn copy_one(src: &Path, dst: &Path, recursive: bool) -> std::io::Result<()> {
    if src.is_dir() {
        if !recursive {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "省略了 -r",
            ));
        }
        fs::create_dir_all(dst)?;
        for ent in fs::read_dir(src)? {
            let ent = ent?;
            copy_one(&ent.path(), &dst.join(ent.file_name()), true)?;
        }
        Ok(())
    } else {
        fs::copy(src, dst).map(|_| ())
    }
}

// ============================================================
// mv — 移动/重命名
// ============================================================
pub fn mv(args: &[String], cwd: &str) -> CmdOut {
    let (_, pos) = split_args(args);
    if pos.len() < 2 {
        return CmdOut::err("mv: 用法: mv 源 目标".into());
    }
    let dst = resolve(cwd, pos.last().unwrap());
    let is_dst_dir = dst.is_dir();
    for src in &pos[..pos.len() - 1] {
        let sp = resolve(cwd, src);
        let target = if is_dst_dir {
            dst.join(sp.file_name().unwrap_or_default())
        } else {
            dst.clone()
        };
        if let Err(e) = fs::rename(&sp, &target) {
            // 跨设备：先 cp 再 rm
            if e.raw_os_error() == Some(18) {
                if let Err(e2) = copy_one(&sp, &target, true) {
                    return CmdOut::err(format!("mv: {}: {}", src, e2));
                }
                let _ = fs::remove_dir_all(&sp).or_else(|_| fs::remove_file(&sp));
            } else {
                return CmdOut::err(format!("mv: {}: {}", src, e));
            }
        }
    }
    CmdOut::ok_stream()
}

// ============================================================
// rm — 删除文件/目录（-r 递归，-f 强制）
// ============================================================
pub fn rm(args: &[String], cwd: &str) -> CmdOut {
    let (flags, pos) = split_args(args);
    let recursive = flags.contains(&'r') || flags.contains(&'R');
    let force = flags.contains(&'f');
    let mut had_err = false;
    let mut msgs = String::new();
    for f in &pos {
        let p = resolve(cwd, f);
        if !p.exists() {
            if !force {
                msgs.push_str(&format!("rm: {}: 没有那个文件或目录\n", f));
                had_err = true;
            }
            continue;
        }
        let res = if p.is_dir() {
            if recursive {
                fs::remove_dir_all(&p)
            } else {
                msgs.push_str(&format!("rm: {}: 是目录\n", f));
                had_err = true;
                continue;
            }
        } else {
            fs::remove_file(&p)
        };
        if let Err(e) = res {
            if !force {
                msgs.push_str(&format!("rm: {}: {}\n", f, e));
                had_err = true;
            }
        }
    }
    if had_err {
        CmdOut::err(msgs)
    } else {
        CmdOut::ok_stream()
    }
}

// ============================================================
// mkdir — 创建目录（-p 递归）
// ============================================================
pub fn mkdir(args: &[String], cwd: &str) -> CmdOut {
    let (flags, pos) = split_args(args);
    let parents = flags.contains(&'p');
    if pos.is_empty() {
        return CmdOut::err("mkdir: 用法: mkdir [-p] 目录".into());
    }
    for d in &pos {
        let p = resolve(cwd, d);
        let res = if parents {
            fs::create_dir_all(&p)
        } else {
            fs::create_dir(&p)
        };
        if let Err(e) = res {
            return CmdOut::err(format!("mkdir: {}: {}", d, e));
        }
    }
    CmdOut::ok_stream()
}

// ============================================================
// rmdir — 删除空目录
// ============================================================
pub fn rmdir(args: &[String], cwd: &str) -> CmdOut {
    let (_, pos) = split_args(args);
    for d in &pos {
        let p = resolve(cwd, d);
        if let Err(e) = fs::remove_dir(&p) {
            return CmdOut::err(format!("rmdir: {}: {}", d, e));
        }
    }
    CmdOut::ok_stream()
}

// ============================================================
// touch — 创建空文件/更新时间戳
// ============================================================
pub fn touch(args: &[String], cwd: &str) -> CmdOut {
    let (_, pos) = split_args(args);
    if pos.is_empty() {
        return CmdOut::err("touch: 用法: touch 文件".into());
    }
    for f in &pos {
        let p = resolve(cwd, f);
        if p.exists() {
            // 更新时间戳：以写方式打开（truncate 不动内容）
            if let Ok(mut fh) = OpenOptions::new().write(true).open(&p) {
                let _ = fh.flush();
            }
        } else {
            if let Err(e) = File::create(&p) {
                return CmdOut::err(format!("touch: {}: {}", f, e));
            }
        }
    }
    CmdOut::ok_stream()
}

use std::fs::OpenOptions;

// ============================================================
// stat — 文件信息
// ============================================================
pub fn stat(args: &[String], cwd: &str) -> CmdOut {
    let (_, pos) = split_args(args);
    if pos.is_empty() {
        return CmdOut::err("stat: 用法: stat 文件".into());
    }
    let mut out = String::new();
    for f in &pos {
        let p = resolve(cwd, f);
        match fs::metadata(&p) {
            Ok(m) => {
                let kind = if m.is_dir() { "目录" } else { "文件" };
                let size = m.len();
                let mtime = m.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok());
                let mt = mtime.map(|d| d.as_secs()).unwrap_or(0);
                out.push_str(&format!("  文件: {}\n", f));
                out.push_str(&format!("  大小: {}\n", size));
                out.push_str(&format!("  类型: {}\n", kind));
                out.push_str(&format!("  修改: {}\n", mt));
            }
            Err(e) => out.push_str(&format!("stat: {}: {}\n", f, e)),
        }
    }
    CmdOut::ok(out)
}

// ============================================================
// ln — 创建链接（默认硬链接，-s 符号链接）
// ============================================================
pub fn ln(args: &[String], cwd: &str) -> CmdOut {
    let (flags, pos) = split_args(args);
    let symbolic = flags.contains(&'s');
    if pos.len() != 2 {
        return CmdOut::err("ln: 用法: ln [-s] 源 目标".into());
    }
    let src = resolve(cwd, &pos[0]);
    let dst = resolve(cwd, &pos[1]);
    let res = if symbolic {
        #[cfg(unix)]
        { std::os::unix::fs::symlink(&src, &dst) }
        #[cfg(windows)]
        {
            if src.is_dir() {
                std::os::windows::fs::symlink_dir(&src, &dst)
            } else {
                std::os::windows::fs::symlink_file(&src, &dst)
            }
        }
        #[cfg(not(any(unix, windows)))]
        { Err(std::io::Error::new(std::io::ErrorKind::Unsupported, "符号链接不支持")) }
    } else {
        fs::hard_link(&src, &dst)
    };
    match res {
        Ok(_) => CmdOut::ok_stream(),
        Err(e) => CmdOut::err(format!("ln: {}: {}", pos[0], e)),
    }
}

// ============================================================
// echo — 输出文本（-n 不换行）
// ============================================================
pub fn echo(args: &[String], _cwd: &str, _stdin: Option<&str>) -> CmdOut {
    let (flags, pos) = split_args(args);
    let no_newline = flags.contains(&'n');
    let mut out = pos.join(" ");
    if !no_newline {
        out.push('\n');
    }
    CmdOut::ok(out)
}

// ============================================================
// wc — 统计行/词/字节（高频，放内核）
// ============================================================
pub fn wc(args: &[String], cwd: &str, stdin: Option<&str>) -> CmdOut {
    let (_, pos) = split_args(args);
    let mut out = String::new();
    let mut t_lines = 0usize;
    let mut t_words = 0usize;
    let mut t_bytes = 0usize;
    let multi = pos.len() > 1;

    let count = |text: &str| -> (usize, usize, usize) {
        let lines = text.lines().count();
        let words = text.split_whitespace().count();
        let bytes = text.len();
        (lines, words, bytes)
    };

    if pos.is_empty() {
        let s = stdin.unwrap_or("");
        let (l, w, b) = count(s);
        out.push_str(&format!("{:>7} {:>7} {:>7}\n", l, w, b));
        return CmdOut::ok(out);
    }
    for f in &pos {
        let text = if f == "-" {
            stdin.unwrap_or("").to_string()
        } else {
            let path = resolve(cwd, f);
            match read_file_mmap(&path) {
                Ok(bytes) => String::from_utf8_lossy(&bytes).to_string(),
                Err(e) => {
                    out.push_str(&format!("wc: {}: {}\n", f, e));
                    continue;
                }
            }
        };
        let (l, w, b) = count(&text);
        t_lines += l;
        t_words += w;
        t_bytes += b;
        if multi {
            out.push_str(&format!("{:>7} {:>7} {:>7} {}\n", l, w, b, f));
        } else {
            out.push_str(&format!("{:>7} {:>7} {:>7}\n", l, w, b));
        }
    }
    if multi {
        out.push_str(&format!("{:>7} {:>7} {:>7} 总计\n", t_lines, t_words, t_bytes));
    }
    CmdOut::ok(out)
}
