"""ai/client.py 测试 — LLM 客户端"""
import json
from unittest.mock import patch

import pytest

from opendracocli.ai.client import LLMClient, LLMResponse, reset_global_client
from opendracocli.config import DracoConfig


@pytest.fixture
def disabled_client():
    """无 API key 的客户端（不可用）"""
    cfg = DracoConfig(ai_api_key="", ai_api_base="http://localhost:8000/v1")
    return LLMClient(cfg)


@pytest.fixture
def enabled_client():
    """有 API key 的客户端"""
    cfg = DracoConfig(ai_api_key="sk-test-key-12345", ai_api_base="http://localhost:8000/v1")
    return LLMClient(cfg)


def test_is_available_no_key(disabled_client):
    """无 key 不可用"""
    assert disabled_client.is_available() is False


def test_is_available_with_key(enabled_client):
    """有 key 可用"""
    assert enabled_client.is_available() is True


def test_is_available_placeholder_key():
    """占位符 key 视为不可用"""
    cfg = DracoConfig(ai_api_key="your_api_key", ai_api_base="http://localhost:8000/v1")
    client = LLMClient(cfg)
    assert client.is_available() is False


def test_is_available_empty_base():
    """空 base 不可用"""
    cfg = DracoConfig(ai_api_key="sk-test", ai_api_base="")
    client = LLMClient(cfg)
    assert client.is_available() is False


def test_update_key_strips_whitespace(enabled_client):
    """update_key 去除空白"""
    enabled_client.update_key("  sk-new-key  ")
    assert enabled_client._api_key == "sk-new-key"


def test_update_key_rejects_placeholder(enabled_client):
    """update_key 拒绝占位符"""
    enabled_client.update_key("your_api_key")
    assert enabled_client._api_key == ""
    assert enabled_client.is_available() is False


@pytest.mark.asyncio
async def test_chat_unavailable_returns_error(disabled_client):
    """不可用时 chat 返回 ok=False"""
    resp = await disabled_client.chat([{"role": "user", "content": "hi"}])
    assert resp.ok is False
    assert "未配置" in resp.error or "不可用" in resp.error


@pytest.mark.asyncio
async def test_chat_success(enabled_client):
    """成功调用返回 content"""
    fake_resp = {
        "choices": [{"message": {"content": "hello world"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    with patch.object(enabled_client, "_do_request", return_value=fake_resp):
        resp = await enabled_client.chat([{"role": "user", "content": "hi"}])
    assert resp.ok is True
    assert resp.content == "hello world"
    assert resp.usage["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_chat_http_error_returns_failure(enabled_client):
    """HTTP 错误时 ok=False，不抛异常"""
    with patch.object(
        enabled_client, "_do_request", side_effect=RuntimeError("HTTP 500: error")
    ):
        resp = await enabled_client.chat([{"role": "user", "content": "hi"}])
    assert resp.ok is False
    assert "HTTP 500" in resp.error


@pytest.mark.asyncio
async def test_chat_parse_failure_returns_failure(enabled_client):
    """响应解析失败时 ok=False"""
    with patch.object(
        enabled_client, "_do_request", return_value={"unexpected": "shape"}
    ):
        resp = await enabled_client.chat([{"role": "user", "content": "hi"}])
    assert resp.ok is False
    assert "解析" in resp.error


@pytest.mark.asyncio
async def test_chat_pseudo_stream(enabled_client):
    """伪流式回调"""
    fake_resp = {
        "choices": [{"message": {"content": "abc"}}],
        "usage": {},
    }
    chunks = []
    with patch.object(enabled_client, "_do_request", return_value=fake_resp):
        resp = await enabled_client.chat(
            [{"role": "user", "content": "hi"}],
            stream=True,
            on_chunk=lambda ch: chunks.append(ch),
        )
    assert resp.ok is True
    # 应收到逐字回调
    assert "".join(chunks) == "abc"


@pytest.mark.asyncio
async def test_chat_cooldown(enabled_client):
    """冷却期内不重复调用"""
    enabled_client._cooldown = 1.0
    enabled_client._last_call = float("inf")  # 设为很近的过去

    fake_resp = {
        "choices": [{"message": {"content": "x"}}],
        "usage": {},
    }
    call_count = 0

    def mock_request(body):
        nonlocal call_count
        call_count += 1
        return fake_resp

    import time

    enabled_client._last_call = time.time()  # 刚调用过

    with patch.object(enabled_client, "_do_request", side_effect=mock_request):
        # 第一次调用会等冷却
        await enabled_client.chat([{"role": "user", "content": "hi"}])
        # 冷却期内的第二次会被 sleep 阻塞，但仍调用（设计上不丢弃）
        await enabled_client.chat([{"role": "user", "content": "hi"}])
    assert call_count == 2


def test_llm_response_dataclass_defaults():
    """LLMResponse 默认值"""
    r = LLMResponse()
    assert r.content == ""
    assert r.ok is False
    assert r.error == ""
    assert r.usage == {}


def test_do_request_builds_correct_url(enabled_client):
    """_do_request 拼接 url"""
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = enabled_client._do_request({"model": "test", "messages": []})

    assert "chat/completions" in captured["url"]
    assert captured["method"] == "POST"
    assert result["choices"][0]["message"]["content"] == "x"


def test_global_client_singleton():
    """全局客户端单例"""
    from opendracocli.ai.client import get_global_client

    reset_global_client()
    cfg = DracoConfig()
    c1 = get_global_client(cfg)
    c2 = get_global_client(cfg)
    assert c1 is c2
    reset_global_client()
