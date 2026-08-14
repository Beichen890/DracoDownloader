"""SSH 到阿里云服务器，诊断并修复 TUI bug + MCP 500"""
import paramiko
import sys

HOST = "39.105.153.71"
USER = "root"
PASS = "-Aa199124159951"

def run(cmd: str, timeout: int = 30) -> tuple:
    """执行命令并返回 (exit_code, output)"""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out + (f"\n[STDERR]\n{err}" if err.strip() else "")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

print(f"=== 连接 {HOST} ===", flush=True)
try:
    client.connect(HOST, port=22, username=USER, password=PASS, timeout=15)
except Exception as e:
    print(f"SSH 连接失败: {e}", flush=True)
    sys.exit(1)
print("✓ SSH 已连接\n", flush=True)

# ========== 第 1 步：修复 TUI bug ==========
print("=" * 60, flush=True)
print("【1】修复 TUI _mapper 死代码 bug", flush=True)
print("=" * 60, flush=True)

TUI_FILE = "/opt/odc.venv/lib/python3.12/site-packages/opendracocli/tui/app.py"

code, out = run(f"grep -n '_mapper.map' {TUI_FILE}")
print(f"修复前 grep: {out.strip() or '(无匹配)'}", flush=True)

if "_mapper.map" in out:
    code, out = run(f"sed -i '/self\\._pipeline\\._mapper\\.map(ir)/d' {TUI_FILE}")
    print(f"sed 执行 exit={code}", flush=True)
    code, out = run(f"grep -n '_mapper' {TUI_FILE}")
    print(f"修复后 grep: {out.strip() or '(无匹配 — 已清除)'}", flush=True)
    if not out.strip():
        print("✓ TUI bug 已修复\n", flush=True)
    else:
        print("⚠ 仍有残留\n", flush=True)
else:
    print("✓ TUI bug 已不存在（可能已修过）\n", flush=True)

# ========== 第 2 步：诊断 MCP 500 ==========
print("=" * 60, flush=True)
print("【2】诊断 MCP 500 Internal Server Error", flush=True)
print("=" * 60, flush=True)

print("\n--- 2.1 MCP 相关进程 ---", flush=True)
code, out = run("ps -ef | grep -iE 'mcp|uvicorn|gunicorn|fastapi' | grep -v grep")
print(out.strip() or "(无 MCP 进程)", flush=True)

print("\n--- 2.2 8000 端口监听 ---", flush=True)
code, out = run("ss -tlnp | grep 8000 || echo '(无监听)'")
print(out.strip(), flush=True)

print("\n--- 2.3 本机 curl /mcp ---", flush=True)
code, out = run("curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://127.0.0.1:8000/mcp")
print(out.strip(), flush=True)

print("\n--- 2.4 查找 MCP server 文件 ---", flush=True)
code, out = run(
    "ls -la /opt/mcp* /opt/*mcp* /root/mcp* /root/*mcp* 2>/dev/null; "
    "find /opt /root -maxdepth 3 -name '*.py' 2>/dev/null | "
    "xargs grep -l 'mcp' 2>/dev/null | head -10"
)
print(out.strip() or "(未找到)", flush=True)

print("\n--- 2.5 systemd 服务 ---", flush=True)
code, out = run("systemctl list-units --type=service --all 2>/dev/null | grep -i mcp")
print(out.strip() or "(无 systemd 服务)", flush=True)

print("\n--- 2.6 最近系统日志 ---", flush=True)
code, out = run(
    "journalctl --no-pager -n 100 2>/dev/null | "
    "grep -iE 'mcp|uvicorn|8000|Traceback|Error' | tail -30"
)
print(out.strip() or "(无相关日志)", flush=True)

print("\n--- 2.7 screen/tmux 会话 ---", flush=True)
code, out = run("screen -ls 2>/dev/null; tmux ls 2>/dev/null")
print(out.strip() or "(无会话)", flush=True)

client.close()
print("\n=== 诊断完成 ===", flush=True)
