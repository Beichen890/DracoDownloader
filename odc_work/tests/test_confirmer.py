"""confirmer 测试"""
import pytest

from opendracocli.security.confirmer import Confirmer


@pytest.mark.asyncio
async def test_yes_confirmed():
    c = Confirmer()
    c.set_test_callback(lambda prompt, default: True)
    assert await c.ask_yes("确认？") is True


@pytest.mark.asyncio
async def test_yes_denied():
    c = Confirmer()
    c.set_test_callback(lambda prompt, default: False)
    assert await c.ask_yes("确认？") is False


@pytest.mark.asyncio
async def test_default_value():
    """测试回调收到的 default 参数"""
    received = []
    c = Confirmer()
    c.set_test_callback(lambda prompt, default: (received.append(default), True)[1])
    await c.ask_yes("p1", default=True)
    await c.ask_yes("p2", default=False)
    assert received == [True, False]


@pytest.mark.asyncio
async def test_prompt_text_passed():
    """测试回调收到的 prompt 文本"""
    received = []
    c = Confirmer()
    c.set_test_callback(lambda prompt, default: (received.append(prompt), True)[1])
    await c.ask_yes("高危操作确认", default=False)
    assert "高危操作确认" in received[0]


@pytest.mark.asyncio
async def test_clear_callback():
    c = Confirmer()
    c.set_test_callback(lambda prompt, default: True)
    c.clear_test_callback()
    # 清除后 _test_callback 应为 None
    assert c._test_callback is None
