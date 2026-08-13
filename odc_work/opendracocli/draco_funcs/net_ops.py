"""网络操作类 Draco 函数

跨平台的端口检查、HTTP 请求——用 Python 标准库实现，
不依赖 curl/netstat/telnet 等命令（Windows 语法不同）。
"""

from __future__ import annotations

import json as _json
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def pcheck(ctx, host: str, port: int = 80, timeout: float = 3.0) -> bool:
    """检查端口是否开放（替代 telnet/nc 命令）

    用 socket 实现，跨平台一致。

    用法:
        pcheck example.com 80
        pcheck 192.168.1.1 port=22 timeout=5

    Args:
        ctx: AgentContext
        host: 主机名或 IP
        port: 端口号
        timeout: 超时秒数

    Returns:
        True=开放，False=关闭
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            print(f"✓ {host}:{port} 开放")
            return True
    except (OSError, socket.timeout) as e:
        print(f"✗ {host}:{port} 关闭 ({type(e).__name__})")
        return False


def http(ctx, url: str, method: str = "GET", headers: str = "", body: str = "", timeout: float = 30.0) -> Dict[str, Any]:
    """HTTP 请求（替代 curl 命令）

    用 urllib 实现，跨平台。支持自定义方法和请求头。

    用法:
        http https://api.github.com
        http https://httpbin.org/post method=POST body='{"k":"v"}'
        http https://api.example.com headers="Authorization:Bearer xxx"

    Args:
        ctx: AgentContext
        url: 请求 URL
        method: HTTP 方法（GET/POST/PUT/DELETE 等）
        headers: 请求头，格式 "Key:Value,Key2:Value2"
        body: 请求体
        timeout: 超时秒数

    Returns:
        {"status": int, "headers": dict, "body": str}
    """
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    # 解析请求头
    header_dict: Dict[str, str] = {}
    if headers:
        for pair in headers.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                header_dict[k.strip()] = v.strip()

    data = body.encode("utf-8") if body else None
    req = Request(url, data=data, method=method, headers=header_dict)

    try:
        with urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            resp_headers = dict(resp.headers.items())
            status = resp.status
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        resp_headers = dict(e.headers.items()) if e.headers else {}
        status = e.code
    except URLError as e:
        print(f"✗ 请求失败: {e.reason}")
        return {"status": 0, "headers": {}, "body": str(e.reason)}

    result = {"status": status, "headers": resp_headers, "body": resp_body}
    print(f"HTTP {method} {url} -> {status}")
    # 尝试格式化 JSON 响应
    try:
        parsed = _json.loads(resp_body)
        print(_json.dumps(parsed, ensure_ascii=False, indent=2))
    except (ValueError, TypeError):
        print(resp_body[:500] + ("..." if len(resp_body) > 500 else ""))
    return result


def dns(ctx, name: str) -> Dict[str, Any]:
    """DNS 解析（替代 nslookup/dig 命令）

    用 socket.getaddrinfo 实现，跨平台。

    用法:
        dns example.com

    Args:
        ctx: AgentContext
        name: 域名

    Returns:
        {"name": str, "addresses": [str]}
    """
    try:
        infos = socket.getaddrinfo(name, None)
        addrs = sorted({info[4][0] for info in infos})
        print(f"{name} -> {', '.join(addrs)}")
        return {"name": name, "addresses": addrs}
    except socket.gaierror as e:
        print(f"✗ 解析失败: {e}")
        return {"name": name, "addresses": [], "error": str(e)}
