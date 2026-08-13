"""P4 DoD 验收测试 — 关键场景对照设计文档的完成定义

DoD:
1. 在 functions.py 定义 deploy()，输入 deploy 执行
2. 函数内 ctx.shell("ls") 正常调 shell 通道（含 P2 风控）
3. Python 通道执行结果写入共享历史
4. /agent gen <intent> → AI 生成函数 → yes 确认后执行
5. AI 生成代码含 import subprocess 时被 AST 拦截
6. agent_enabled=False 时函数名输入回退到 shell 通道（无回归）
7. 异步函数 + 同步函数都能正确执行
8. 所有测试通过，P1-P3 无回归
"""
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from opendracocli.config import DracoConfig, set_global_config
from opendracocli.hooks.registry import HookRegistry, set_global_registry
from opendracocli.events import EVT_AGENT_EXECUTED, EventBus, set_global_bus
from opendracocli.ai.client import LLMClient, LLMResponse


@pytest.fixture
def fresh_env(tmp_path):
    """全新隔离环境"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        functions_file=str(tmp_path / "functions.py"),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())
    set_global_bus(EventBus())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()
    from opendracocli.ai.client import reset_global_client
    reset_global_client()

    return cfg


# === DoD 1: 在 functions.py 定义 deploy()，输入 deploy 执行 ===


@pytest.mark.asyncio
async def test_dod1_deploy_function_executes(fresh_env, capsys):
    """DoD 1: 定义 deploy() → 输入 deploy 执行"""
    fresh_env.functions_file_resolved.write_text(
        'async def deploy(ctx):\n'
        '    """一键部署"""\n'
        '    r = await ctx.shell("echo deployed")\n'
        '    return {"status": "deployed", "shell_out": r["stdout"].strip()}\n',
        encoding="utf-8",
    )
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._func_registry.get("deploy") is not None

    await app._exec_command("deploy")
    captured = capsys.readouterr()
    assert "deployed" in captured.out


# === DoD 2: 函数内 ctx.shell("ls") 正常调 shell 通道（含 P2 风控）===


@pytest.mark.asyncio
async def test_dod2_ctx_shell_uses_pipeline(fresh_env, capsys):
    """DoD 2: ctx.shell 走 ShellPipeline（含 P2 风控）"""
    fresh_env.functions_file_resolved.write_text(
        'async def listfiles(ctx):\n'
        '    """列文件"""\n'
        '    r = await ctx.shell("echo hello_from_shell")\n'
        '    ctx.print("shell said: " + r["stdout"].strip())\n'
        '    return r["exit_code"]\n',
        encoding="utf-8",
    )
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("listfiles")
    captured = capsys.readouterr()
    assert "hello_from_shell" in captured.out
    assert "shell said" in captured.out


@pytest.mark.asyncio
async def test_dod2_ctx_shell_risk_control_still_works(fresh_env, capsys):
    """DoD 2: 函数内 ctx.shell 调危险命令仍触发风控"""
    fresh_env.functions_file_resolved.write_text(
        'async def danger(ctx):\n'
        '    """调危险命令"""\n'
        '    r = await ctx.shell("rm -rf /tmp/test_dod_p4")\n'
        '    return {"blocked": r["blocked"]}\n',
        encoding="utf-8",
    )
    from opendracocli.security.confirmer import Confirmer
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    # 拒绝确认 → 应被阻断
    app._confirmer.set_test_callback(lambda p, d: False)
    await app._exec_command("danger")
    captured = capsys.readouterr()
    # 阻断信息
    assert "阻断" in captured.err or "拒绝" in captured.err or "blocked" in captured.out.lower()


# === DoD 3: Python 通道执行结果写入共享历史 ===


@pytest.mark.asyncio
async def test_dod3_agent_writes_shared_history(fresh_env):
    """DoD 3: Python 通道结果写入共享 HistoryStore"""
    fresh_env.functions_file_resolved.write_text(
        'def task(ctx):\n    """任务"""\n    return {"ok": True}\n',
        encoding="utf-8",
    )
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("task")

    # 查历史
    recs = app._history.recent(limit=10, session_id=None)
    # 应包含 task() 记录
    agent_recs = [r for r in recs if "task()" in r.raw_input]
    assert len(agent_recs) >= 1


# === DoD 4: /agent gen → AI 生成 → yes 确认 → 执行 ===


@pytest.mark.asyncio
async def test_dod4_agent_gen_executes(fresh_env, capsys):
    """DoD 4: /agent gen → 生成 → 确认 → 执行"""
    fresh_env.agent_enabled = True
    fresh_env.ai_enabled = True
    fresh_env.ai_api_key = "sk-test-dod-12345"

    # mock LLMClient
    from opendracocli.ai.client import get_global_client

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()

    # 注入 mock client
    mock_client = MagicMock(spec=LLMClient)
    mock_client.is_available.return_value = True
    mock_client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content=(
                "删除 pyc 文件\n"
                "```python\n"
                "async def clean_pyc(ctx):\n"
                "    \"\"\"删除 pyc\"\"\"\n"
                "    await ctx.shell(\"echo cleaning_pyc\")\n"
                "    return {\"cleaned\": True}\n"
                "```"
            ),
        )
    )
    app._llm_client = mock_client
    if app._code_generator:
        app._code_generator._client = mock_client

    # 确认执行
    app._confirmer.set_test_callback(lambda p, d: True)

    await app._handle_agent_cmd("gen 删除所有 pyc 文件")
    captured = capsys.readouterr()
    # 应执行了生成的函数
    assert "cleaning_pyc" in captured.out


# === DoD 5: AI 生成代码含 import subprocess 时被 AST 拦截 ===


@pytest.mark.asyncio
async def test_dod5_subprocess_blocked(fresh_env, capsys):
    """DoD 5: AI 生成含 subprocess 的代码被拦截"""
    fresh_env.agent_enabled = True
    fresh_env.ai_enabled = True
    fresh_env.ai_api_key = "sk-test-dod-12345"

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()

    mock_client = MagicMock(spec=LLMClient)
    mock_client.is_available.return_value = True
    mock_client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content=(
                "```python\n"
                "import subprocess\n"
                "def f(ctx):\n"
                "    subprocess.run(['ls'])\n"
                "    return 1\n"
                "```"
            ),
        )
    )
    app._llm_client = mock_client
    if app._code_generator:
        app._code_generator._client = mock_client

    await app._handle_agent_cmd("gen test")
    captured = capsys.readouterr()
    # 生成阶段就应拦截（CodeGenerator 内部 check_safety strict）
    assert "失败" in captured.err or "安全" in captured.err or "subprocess" in captured.err


# === DoD 6: agent_enabled=False 时函数名输入回退 shell 通道 ===


@pytest.mark.asyncio
async def test_dod6_fallback_to_shell_when_disabled(tmp_path, capsys):
    """DoD 6: agent 关闭时函数名走 shell（无回归）"""
    func_path = tmp_path / "functions.py"
    func_path.write_text('def echo(ctx):\n    return "agent"\n', encoding="utf-8")
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=False,  # 关闭
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("echo from_shell_not_agent")
    captured = capsys.readouterr()
    assert "from_shell_not_agent" in captured.out


# === DoD 7: 异步函数 + 同步函数都能正确执行 ===


@pytest.mark.asyncio
async def test_dod7_sync_and_async(fresh_env, capsys):
    """DoD 7: 同步 + 异步函数都能执行"""
    fresh_env.functions_file_resolved.write_text(
        'def sync_fn(ctx):\n'
        '    """同步"""\n'
        '    ctx.print("sync_ran")\n'
        '    return "sync"\n\n'
        'async def async_fn(ctx):\n'
        '    """异步"""\n'
        '    r = await ctx.shell("echo async_shell")\n'
        '    ctx.print(r["stdout"])\n'
        '    ctx.print("async_ran")\n'
        '    return "async"\n',
        encoding="utf-8",
    )
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._func_registry.get("sync_fn").is_async is False
    assert app._func_registry.get("async_fn").is_async is True

    await app._exec_command("sync_fn")
    capsys.readouterr()  # 清空

    await app._exec_command("async_fn")
    captured = capsys.readouterr()
    assert "async_ran" in captured.out
    assert "async_shell" in captured.out


# === DoD 8: P1-P3 无回归（简化验证）===


@pytest.mark.asyncio
async def test_dod8_p1_shell_still_works(fresh_env, capsys):
    """DoD 8a: P1 shell 通道仍工作"""
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("echo p1_still_works")
    captured = capsys.readouterr()
    assert "p1_still_works" in captured.out


@pytest.mark.asyncio
async def test_dod8_p2_risk_still_works(tmp_path, capsys):
    """DoD 8b: P2 风控仍工作"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(tmp_path / "functions.py"),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    app._confirmer.set_test_callback(lambda p, d: False)
    await app._exec_command("rm /tmp/dod8_test_file")
    captured = capsys.readouterr()
    assert "阻断" in captured.err or "拒绝" in captured.err


@pytest.mark.asyncio
async def test_dod8_p3_ai_disabled_no_crash(fresh_env, capsys):
    """DoD 8c: P3 AI 未启用时不崩溃"""
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._ai_enabled is False
    await app._exec_command("echo p3_ok")
    captured = capsys.readouterr()
    assert "p3_ok" in captured.out


# === 额外: 事件发布验证 ===


@pytest.mark.asyncio
async def test_agent_events_published(fresh_env):
    """Python 通道执行时发布 EVT_AGENT_EXECUTED 事件"""
    fresh_env.functions_file_resolved.write_text(
        'def f(ctx):\n    return 1\n', encoding="utf-8"
    )
    bus = EventBus()
    set_global_bus(bus)
    events = []
    bus.subscribe(EVT_AGENT_EXECUTED, lambda e: events.append(e))

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("f")
    assert len(events) >= 1
    assert events[0].payload["func_name"] == "f"
    assert events[0].payload["success"] is True


# === 额外: 参数传递验证 ===


@pytest.mark.asyncio
async def test_function_args_positional(fresh_env, capsys):
    """位置参数绑定"""
    fresh_env.functions_file_resolved.write_text(
        'def add(ctx, a, b):\n    """加法"""\n    return int(a) + int(b)\n',
        encoding="utf-8",
    )
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("add 3 5")
    captured = capsys.readouterr()
    assert "8" in captured.out


@pytest.mark.asyncio
async def test_function_args_keyword(fresh_env, capsys):
    """关键字参数绑定"""
    fresh_env.functions_file_resolved.write_text(
        'def greet(ctx, name="world", times=1):\n'
        '    """问候"""\n'
        '    return name * int(times)\n',
        encoding="utf-8",
    )
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("greet name=hi times=3")
    captured = capsys.readouterr()
    assert "hihihi" in captured.out
