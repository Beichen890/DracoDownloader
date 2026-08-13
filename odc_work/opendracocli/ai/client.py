"""LLM 客户端 — OpenAI 兼容协议

复用 DracoHub 的设计:
  - llm-gateway/adapters.py: Adapter 抽象，统一 OpenAI 格式
  - agent/core.py:_call_api: 三元组返回，失败降级不抛异常

P3 简化:
  - 用标准库 urllib（零额外依赖），async 通过 run_in_executor
  - 伪流式（一次性获取后逐字回调，参考 agent/core.py chat_stream 模式）
  - 冷却防抖（参考 cooldown_seconds）
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..config import DracoConfig
from ..logger import get_logger

log = get_logger("ai.client")


@dataclass
class LLMResponse:
    """LLM 调用结果（复用 _call_api 三元组契约，失败不抛异常）

    Attributes:
        content: 模型回复文本
        usage: token 用量 {prompt_tokens, completion_tokens}
        ok: 是否成功
        error: 失败原因
    """

    content: str = ""
    usage: dict = field(default_factory=dict)
    ok: bool = False
    error: str = ""


class LLMClient:
    """LLM 客户端

    OpenAI Chat Completions 兼容协议。失败时返回 LLMResponse(ok=False)，
    绝不抛异常（复用 DracoHub _call_api 异常吞噬策略）。
    """

    def __init__(self, config: DracoConfig) -> None:
        self._api_base = config.ai_api_base.rstrip("/")
        self._api_key = config.ai_api_key_effective
        self._model = config.ai_model
        self._max_tokens = config.ai_max_tokens
        self._temperature = config.ai_temperature
        self._timeout = config.ai_timeout
        self._cooldown = config.ai_cooldown
        self._last_call = 0.0

    def is_available(self) -> bool:
        """AI 是否可用（有有效 key + base）"""
        return bool(self._api_key) and bool(self._api_base)

    def update_key(self, api_key: str) -> None:
        """运行时更新 key（/ai on 时从环境变量刷新）"""
        self._api_key = api_key.strip()
        placeholders = {"", "your_api_key", "sk-your", "your-key", "placeholder"}
        if self._api_key.lower() in placeholders:
            self._api_key = ""

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        stream: bool = False,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """调用 LLM

        Args:
            messages: OpenAI 格式消息列表
            stream: 是否伪流式输出（逐字回调 on_chunk）
            on_chunk: 流式回调（每个字符块调用一次）

        Returns:
            LLMResponse（失败时 ok=False，不抛异常）
        """
        if not self.is_available():
            return LLMResponse(ok=False, error="AI 未配置 API key 或 api_base")

        # 冷却防抖
        now = time.time()
        wait = self._cooldown - (now - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.time()

        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": False,  # 始终非流式请求，伪流式靠 on_chunk 模拟
        }

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, self._do_request, body)
        except Exception as e:
            log.warning("LLM 请求失败: %s", e)
            return LLMResponse(ok=False, error=str(e))

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            log.warning("LLM 响应解析失败: %s, raw=%s", e, str(data)[:200])
            return LLMResponse(ok=False, error=f"响应解析失败: {e}")

        usage = data.get("usage", {})

        # 伪流式：逐字回调
        if stream and on_chunk and content:
            for ch in content:
                on_chunk(ch)
                await asyncio.sleep(0.008)

        return LLMResponse(content=content, usage=usage, ok=True)

    def _do_request(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """同步 HTTP 请求（在 executor 线程执行）

        复用 DracoHub agent/core.py:_call_api 的 urllib 模式。
        """
        url = f"{self._api_base}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}") from e


# 全局单例
_global_client: Optional[LLMClient] = None


def get_global_client(config: Optional[DracoConfig] = None) -> LLMClient:
    """获取全局 LLMClient 单例"""
    global _global_client
    if _global_client is None:
        from ..config import get_global_config

        _global_client = LLMClient(config or get_global_config())
    return _global_client


def reset_global_client() -> None:
    """重置全局客户端（测试用）"""
    global _global_client
    _global_client = None
