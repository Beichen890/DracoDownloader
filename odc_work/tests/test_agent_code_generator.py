"""agent/code_generator 测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from opendracocli.agent.code_generator import CodeGenerator, GeneratedFunction
from opendracocli.ai.client import LLMClient, LLMResponse
from opendracocli.config import DracoConfig


def _make_client(available=True):
    """构造 mock LLMClient"""
    client = MagicMock(spec=LLMClient)
    client.is_available.return_value = available
    return client


@pytest.mark.asyncio
async def test_generate_unavailable_client():
    client = _make_client(available=False)
    gen = CodeGenerator(client)
    result = await gen.generate("删掉 pyc")
    assert result.ok is False
    assert "不可用" in result.error


@pytest.mark.asyncio
async def test_generate_success_code_block():
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content=(
                "删除所有 pyc 文件\n"
                "```python\n"
                "async def clean_pyc(ctx):\n"
                "    \"\"\"删除 pyc\"\"\"\n"
                "    await ctx.shell(\"find . -name '*.pyc' -delete\")\n"
                "    return {\"done\": True}\n"
                "```"
            ),
        )
    )
    gen = CodeGenerator(client)
    result = await gen.generate("把所有 .pyc 文件删掉")

    assert result.ok is True
    assert result.func_name == "clean_pyc"
    assert "def clean_pyc" in result.code
    assert "ctx.shell" in result.code


@pytest.mark.asyncio
async def test_generate_llm_failure():
    client = _make_client(available=True)
    client.chat = AsyncMock(return_value=LLMResponse(ok=False, error="network error"))
    gen = CodeGenerator(client)
    result = await gen.generate("test")
    assert result.ok is False
    assert "network error" in result.error


@pytest.mark.asyncio
async def test_generate_no_code_block():
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(ok=True, content="我无法生成代码")
    )
    gen = CodeGenerator(client)
    result = await gen.generate("test")
    assert result.ok is False
    assert "未找到" in result.error or "代码" in result.error


@pytest.mark.asyncio
async def test_generate_syntax_error():
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content="```python\ndef bad(ctx:\n    return 1\n```",
        )
    )
    gen = CodeGenerator(client)
    result = await gen.generate("test")
    assert result.ok is False
    assert "语法错误" in result.error


@pytest.mark.asyncio
async def test_generate_subprocess_blocked():
    """AI 生成含 subprocess 的代码应被 AST 拦截"""
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content=(
                "```python\n"
                "import subprocess\n"
                "def f(ctx):\n"
                "    subprocess.run(['ls'])\n"
                "```"
            ),
        )
    )
    gen = CodeGenerator(client)
    result = await gen.generate("test")
    assert result.ok is False
    assert "安全" in result.error or "subprocess" in result.error


@pytest.mark.asyncio
async def test_generate_code_without_block_but_has_def():
    """无代码块但有 def 行时尝试提取"""
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content="说明文字\nasync def task(ctx):\n    return {\"ok\": True}\n",
        )
    )
    gen = CodeGenerator(client)
    result = await gen.generate("test")
    assert result.ok is True
    assert result.func_name == "task"


@pytest.mark.asyncio
async def test_generate_description_extracted():
    """代码块前的说明文字应作为 description"""
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content=(
                "这是一个清理函数\n"
                "```python\ndef clean(ctx):\n    return 1\n```"
            ),
        )
    )
    gen = CodeGenerator(client)
    result = await gen.generate("test")
    assert result.ok is True
    assert "清理函数" in result.description


def test_default_name_from_intent():
    client = _make_client(available=False)
    gen = CodeGenerator(client)
    assert gen._default_name_from_intent("delete all pyc files") == "delete_pyc"
    assert gen._default_name_from_intent("the quick brown fox") == "quick_brown_fox"
    # 纯数字开头加前缀
    name = gen._default_name_from_intent("123 abc")
    assert name.startswith("task_") or name[0].isalpha()


@pytest.mark.asyncio
async def test_generate_with_context_summary():
    client = _make_client(available=True)
    client.chat = AsyncMock(
        return_value=LLMResponse(
            ok=True,
            content="```python\ndef f(ctx):\n    return 1\n```",
        )
    )
    gen = CodeGenerator(client)
    await gen.generate("test", context_summary="最近常用 ls 命令")
    # 验证 chat 被调用
    assert client.chat.called
