"""常用自动化模板 — 代码即行动的示例库

参考 DracoHub CodingAgent 的"模板即起点"思路：提供常用运维自动化函数模板，
用户通过 `/agent apply <template>` 写入 functions.py，作为自定义的起点。

模板都是合法的 Python 函数源码，可直接 exec。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class FunctionTemplate:
    """函数模板"""

    name: str          # 模板名（用于 /agent apply <name>）
    description: str   # 一句话描述
    code: str          # 完整函数源码


TEMPLATES: Dict[str, FunctionTemplate] = {}


def _register(t: FunctionTemplate) -> None:
    TEMPLATES[t.name] = t


_register(
    FunctionTemplate(
        name="deploy",
        description="一键部署：拉代码 + 装依赖 + 重启服务",
        code='''async def deploy(ctx):
    """一键部署：拉代码 + 装依赖 + 重启服务

    用法: deploy
    """
    ctx.log("开始部署...")
    r = await ctx.shell("git pull")
    if not r["success"]:
        ctx.log("git pull 失败: " + r["stderr"])
        return {"status": "failed", "step": "git pull"}
    await ctx.shell("pip install -r requirements.txt")
    await ctx.shell("systemctl restart myapp")
    ctx.log("部署完成")
    return {"status": "deployed"}
''',
    )
)

_register(
    FunctionTemplate(
        name="clean_logs",
        description="清理 N 天前的日志文件",
        code='''def clean_logs(ctx, days=7):
    """清理 N 天前的日志文件

    用法: clean_logs            # 默认 7 天
          clean_logs days=3     # 清理 3 天前的
          clean_logs 3          # 位置参数
    """
    ctx.print(f"清理 {days} 天前的日志")
    # 注意：实际执行会走 P2 风控，find -delete 可能触发确认
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        ctx.shell(f"find /var/log -mtime +{days} -type f")
    )
    ctx.print("找到的日志文件:\\n" + r["stdout"])
    return {"days": days, "found": r["stdout"]}
''',
    )
)

_register(
    FunctionTemplate(
        name="gitstat",
        description="git 状态摘要（统计变更文件数）",
        code='''async def gitstat(ctx):
    """git 状态摘要

    用法: gitstat
    """
    r = await ctx.shell("git status --porcelain")
    if not r["success"]:
        return {"error": r["stderr"]}
    lines = [l for l in r["stdout"].splitlines() if l.strip()]
    added = sum(1 for l in lines if l.startswith("A") or l.startswith("??"))
    modified = sum(1 for l in lines if l.startswith(" M"))
    deleted = sum(1 for l in lines if l.startswith(" D"))
    ctx.print(f"变更: +{added} ~{modified} -{deleted}（共 {len(lines)} 个文件）")
    return {"added": added, "modified": modified, "deleted": deleted, "total": len(lines)}
''',
    )
)

_register(
    FunctionTemplate(
        name="backup",
        description="备份文件到指定目录（带时间戳）",
        code='''async def backup(ctx, src=".", dst="./backup"):
    """备份文件到指定目录

    用法: backup src=important dst=/tmp/bak
    """
    import time
    ts = time.strftime("%Y%m%d_%H%M%S")
    target = f"{dst}/{ts}"
    ctx.log(f"备份 {src} → {target}")
    r = await ctx.shell(f"mkdir -p {target} && cp -r {src}/* {target}/ 2>/dev/null")
    if not r["success"]:
        ctx.log("备份失败: " + r["stderr"])
        return {"status": "failed", "target": target}
    ctx.print(f"已备份到 {target}")
    return {"status": "ok", "target": target}
''',
    )
)

_register(
    FunctionTemplate(
        name="ports",
        description="查看占用端口的进程",
        code='''async def ports(ctx, port=""):
    """查看占用端口的进程

    用法: ports              # 列出所有监听端口
          ports port=8080    # 查 8080
    """
    import sys
    if sys.platform.startswith("win"):
        cmd = f"netstat -ano | findstr {port}" if port else "netstat -ano | findstr LISTENING"
    else:
        cmd = f"lsof -i:{port}" if port else "lsof -i -P -n | grep LISTEN"
    r = await ctx.shell(cmd)
    ctx.print(r["stdout"] or "(无结果)")
    return {"raw": r["stdout"], "port": port}
''',
    )
)


def list_templates() -> List[FunctionTemplate]:
    """列出所有模板"""
    return [TEMPLATES[k] for k in sorted(TEMPLATES.keys())]


def get_template(name: str) -> FunctionTemplate:
    """获取指定模板

    Raises:
        KeyError: 模板不存在
    """
    return TEMPLATES[name]
