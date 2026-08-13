"""CodeGenerator — AI 生成 Python 函数（代码即行动）

参考 DracoHub CodingAgent 的"代码即行动"原则：用户描述意图 → AI 生成 Python 函数 →
用户确认后执行。

复用 P3 的 LLMClient（OpenAI 兼容协议），构建代码生成 prompt，让 LLM 返回符合
签名的 Python 函数源码。

生成约定:
  - 函数签名：def <name>(ctx, ...) -> Any
  - 可用 ctx.shell(cmd) 调 shell（走 P2 风控）
  - 可用 ctx.log(msg) / ctx.print(msg)
  - 可 import 标准库（除禁用模块）
  - 返回 dict 或 Any（序列化显示）
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..ai.client import LLMClient
from ..ai.prompt import build_code_gen_prompt
from ..errors import ERR_AI_PARSE_FAILED, make_error
from ..logger import get_logger
from .code_runner import CodeRunner

log = get_logger("agent.code_generator")


@dataclass
class GeneratedFunction:
    """AI 生成的函数结果"""

    ok: bool
    code: str = ""           # 完整函数源码
    func_name: str = ""
    description: str = ""
    error: str = ""
    raw: str = ""            # LLM 原始输出


# 匹配 ```python ... ``` 或 ``` ... ``` 代码块
_CODE_BLOCK_RE = re.compile(
    r"```(?:python)?\s*\n(.*?)```",
    re.DOTALL,
)

# 匹配 def name(
_DEF_RE = re.compile(r"(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


class CodeGenerator:
    """AI 代码生成器"""

    def __init__(
        self,
        client: LLMClient,
        *,
        platform: str = "",
        max_tokens: int = 1024,
        runner: Optional[CodeRunner] = None,
    ) -> None:
        self._client = client
        self._platform = platform
        self._max_tokens = max_tokens
        self._runner = runner or CodeRunner()

    async def generate(
        self,
        intent: str,
        *,
        context_summary: str = "",
    ) -> GeneratedFunction:
        """根据用户意图生成 Python 函数

        Args:
            intent: 用户自然语言意图（如 "把所有 .pyc 文件删掉"）
            context_summary: 感知上下文摘要（可选）

        Returns:
            GeneratedFunction
        """
        if not self._client.is_available():
            return GeneratedFunction(
                ok=False, error="AI 不可用（未配置 API key 或 api_base 不可达）"
            )

        sys_prompt = build_code_gen_prompt(
            platform=self._platform, context_summary=context_summary
        )

        try:
            resp = await self._client.chat(
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": intent},
                ]
            )
        except Exception as e:
            log.warning("代码生成 LLM 调用失败: %s", e)
            return GeneratedFunction(ok=False, error=str(e))

        if not resp.ok:
            return GeneratedFunction(ok=False, error=resp.error or "LLM 调用失败")

        return self._parse_response(resp.content, intent)

    def _parse_response(self, content: str, intent: str) -> GeneratedFunction:
        """解析 LLM 响应，提取代码块 + 函数名

        Args:
            content: LLM 原始输出
            intent: 用户意图（用于生成默认函数名）

        Returns:
            GeneratedFunction
        """
        raw = content

        # 1. 提取代码块
        code = ""
        match = _CODE_BLOCK_RE.search(content)
        if match:
            code = match.group(1).strip()
        else:
            # 无代码块，尝试整体当代码（去前后说明文字的粗略处理）
            # 找第一个 def 行到末尾
            lines = content.splitlines()
            start_idx = -1
            for i, line in enumerate(lines):
                if _DEF_RE.search(line):
                    start_idx = i
                    break
            if start_idx >= 0:
                # 从 def 行开始，到代码缩进结束
                code_lines = lines[start_idx:]
                # 简单去尾部空行
                while code_lines and not code_lines[-1].strip():
                    code_lines.pop()
                code = "\n".join(code_lines)

        if not code:
            return GeneratedFunction(
                ok=False, error="LLM 响应中未找到 Python 函数代码", raw=raw
            )

        # 2. 提取函数名
        func_name = ""
        m = _DEF_RE.search(code)
        if m:
            func_name = m.group(1)
        if not func_name:
            # 从意图生成默认名
            func_name = self._default_name_from_intent(intent)

        # 3. AST 校验语法
        try:
            ast.parse(code)
        except SyntaxError as e:
            return GeneratedFunction(
                ok=False,
                error=f"生成的代码语法错误: {e}",
                code=code,
                func_name=func_name,
                raw=raw,
            )

        # 4. 安全检查（strict，因为是 AI 生成）
        violations = self._runner.check_safety(code, strict=True)
        if violations:
            return GeneratedFunction(
                ok=False,
                error=f"生成的代码违反安全策略: {'; '.join(violations)}",
                code=code,
                func_name=func_name,
                raw=raw,
            )

        # 5. 提取描述（代码块前的文字）
        desc = ""
        if match:
            before = content[: match.start()].strip()
            if before:
                # 取第一段
                desc = before.split("\n\n")[0].strip()[:200]

        return GeneratedFunction(
            ok=True,
            code=code,
            func_name=func_name,
            description=desc,
            raw=raw,
        )

    @staticmethod
    def _default_name_from_intent(intent: str) -> str:
        """从意图生成默认函数名"""
        # 取前几个词，下划线连接，小写
        words = re.findall(r"[a-zA-Z]+", intent.lower())
        if not words:
            return "task"
        # 过滤停用词
        stop = {"the", "a", "an", "to", "of", "and", "or", "all", "files", "file", "把", "的"}
        meaningful = [w for w in words if w not in stop][:3]
        if not meaningful:
            meaningful = words[:2]
        name = "_".join(meaningful)
        # 确保合法标识符
        if not name[0].isalpha():
            name = "task_" + name
        return name

    def make_draco_error(self, gen: GeneratedFunction) -> Any:
        """把生成失败转为 DracoError"""
        return make_error(ERR_AI_PARSE_FAILED, reason=gen.error)
