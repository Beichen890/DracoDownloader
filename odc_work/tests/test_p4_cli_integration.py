"""P4 CLI 集成测试 — 通道路由 + /agent 命令 + 启用/禁用"""
import pytest

from opendracocli.config import DracoConfig, set_global_config
from opendracocli.hooks.registry import HookRegistry, set_global_registry


@pytest.fixture
def isolated_cfg(tmp_path):
    """隔离配置（agent 默认关闭）"""
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        ai_context_file=str(tmp_path / "context.json"),
        functions_file=str(tmp_path / "functions.py"),
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())
    return cfg


def test_agent_disabled_by_default(isolated_cfg):
    assert isolated_cfg.agent_enabled is False


def test_agent_not_initialized_when_disabled(isolated_cfg):
    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._agent_enabled is False
    # Draco 函数默认注册（不依赖 agent_enabled）
    assert app._func_registry is not None
    assert app._py_executor is not None
    # code_generator 仅在 agent_enabled 时创建
    assert app._code_generator is None


def test_agent_enabled_initializes(tmp_path):
    """agent_enabled=True 时初始化组件"""
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
    assert app._agent_enabled is True
    assert app._func_registry is not None
    assert app._py_executor is not None
    assert app._code_generator is not None


def test_agent_loads_functions_file(tmp_path):
    """启动时加载 functions.py"""
    func_path = tmp_path / "functions.py"
    func_path.write_text(
        'def greet(ctx, name="world"):\n    """问候"""\n    return f"hi {name}"\n',
        encoding="utf-8",
    )
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._func_registry.get("greet") is not None


@pytest.mark.asyncio
async def test_route_to_agent_channel(tmp_path, capsys):
    """命中函数名时走 Python 通道"""
    func_path = tmp_path / "functions.py"
    func_path.write_text(
        'def greet(ctx, name="world"):\n    """问候"""\n    ctx.print(f"hello {name}")\n    return name\n',
        encoding="utf-8",
    )
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("greet name=draco")
    captured = capsys.readouterr()
    assert "hello draco" in captured.out


@pytest.mark.asyncio
async def test_route_to_shell_when_not_function(tmp_path, capsys):
    """非函数名时回退 shell 通道"""
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
    # echo 是 shell 命令，不是注册函数
    await app._exec_command("echo shell_channel")
    captured = capsys.readouterr()
    assert "shell_channel" in captured.out


@pytest.mark.asyncio
async def test_route_to_shell_when_agent_disabled(tmp_path, capsys):
    """agent_enabled=False 时函数名也走 shell（无回归）"""
    func_path = tmp_path / "functions.py"
    func_path.write_text(
        'def echo(ctx):\n    return "from_agent"\n', encoding="utf-8"
    )
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=False,  # 关键：关闭
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("echo test")
    captured = capsys.readouterr()
    # 走 shell，echo test 输出 "test"
    assert "test" in captured.out


@pytest.mark.asyncio
async def test_agent_on_off(isolated_cfg):
    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    assert app._agent_enabled is False

    app._agent_on()
    assert app._agent_enabled is True
    assert app._func_registry is not None

    app._agent_off()
    assert app._agent_enabled is False
    # 内置 Draco 函数保留（仅用户函数被卸载）
    assert app._func_registry is not None


@pytest.mark.asyncio
async def test_agent_list_shows_functions(tmp_path, capsys):
    func_path = tmp_path / "functions.py"
    func_path.write_text(
        'def greet(ctx):\n    """问候"""\n    return 1\n', encoding="utf-8"
    )
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    app._agent_list()
    captured = capsys.readouterr()
    assert "greet" in captured.out


@pytest.mark.asyncio
async def test_agent_show_function(tmp_path, capsys):
    func_path = tmp_path / "functions.py"
    func_path.write_text(
        'def greet(ctx):\n    """问候"""\n    return 1\n', encoding="utf-8"
    )
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    app._agent_show("greet")
    captured = capsys.readouterr()
    assert "def greet" in captured.out


@pytest.mark.asyncio
async def test_agent_templates_list(capsys):
    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    app._agent_templates()
    captured = capsys.readouterr()
    assert "deploy" in captured.out


@pytest.mark.asyncio
async def test_handle_slash_agent_routes(isolated_cfg):
    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    # /agent 应显示状态（不报错）
    await app._handle_slash("/agent")
    await app._handle_slash("/agent templates")


@pytest.mark.asyncio
async def test_agent_apply_template(tmp_path, capsys):
    """应用模板到 functions.py"""
    func_path = tmp_path / "functions.py"
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    app._agent_apply("greet" if False else "deploy")
    captured = capsys.readouterr()
    assert "已保存" in captured.out
    assert func_path.exists()
    content = func_path.read_text(encoding="utf-8")
    assert "def deploy" in content


@pytest.mark.asyncio
async def test_function_with_ctx_shell(tmp_path, capsys):
    """函数内 ctx.shell 调 shell 通道（集成测试）"""
    func_path = tmp_path / "functions.py"
    func_path.write_text(
        'async def echoit(ctx):\n'
        '    """调 shell"""\n'
        '    r = await ctx.shell("echo from_shell")\n'
        '    return {"stdout": r["stdout"].strip()}\n',
        encoding="utf-8",
    )
    cfg = DracoConfig(
        history_db_path=str(tmp_path / "history.db"),
        aliases_file=str(tmp_path / "aliases.json"),
        auth_hash_file=str(tmp_path / "auth.hash"),
        functions_file=str(func_path),
        agent_enabled=True,
    )
    set_global_config(cfg)
    set_global_registry(HookRegistry())

    from opendracocli.agent.function_registry import reset_global_registry
    reset_global_registry()

    from opendracocli.cli import OpenDracoCLI

    app = OpenDracoCLI()
    await app._exec_command("echoit")
    captured = capsys.readouterr()
    # shell echo 输出 + 函数返回值
    assert "from_shell" in captured.out
