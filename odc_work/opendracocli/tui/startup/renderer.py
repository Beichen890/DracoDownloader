"""启动画面渲染器: 调用模板函数生成文本

纯文本生成逻辑, 不依赖 textual / rich 运行时。
模板函数抛任何异常都回退到 minimal, 绝不抛异常。
"""

from __future__ import annotations

from .loader import StartupContext, StartupLoader, StartupTemplateFunc


class StartupRenderer:
    """调用模板函数渲染启动画面文本"""

    def __init__(self, loader: StartupLoader | None = None) -> None:
        self._loader = loader or StartupLoader()

    def _try_render(self, name: str, ctx: StartupContext) -> str:
        """尝试渲染指定模板, 任何异常或空结果都返回空串 (不抛)"""
        try:
            func = self._loader.get(name)
            result = func(ctx)
            return result if isinstance(result, str) else ""
        except Exception:
            return ""

    def render(self, template_name: str, ctx: StartupContext) -> str:
        """渲染指定模板。

        模板函数抛任何异常都回退到 minimal 模板。
        minimal 也失败则返回一个硬编码的最小字符串。
        返回纯文本 (可含 ANSI 颜色码, 由模板自行决定)。
        """
        # 1. 尝试指定模板
        result = self._try_render(template_name, ctx)
        if result:
            return result

        # 2. 回退到 minimal
        if template_name != "minimal":
            result = self._try_render("minimal", ctx)
            if result:
                return result

        # 3. 硬编码最小字符串 (最终兜底)
        head = f"OpenDracoCLI v{ctx.version}"
        if ctx.platform:
            head += f" ({ctx.platform})"
        return head
