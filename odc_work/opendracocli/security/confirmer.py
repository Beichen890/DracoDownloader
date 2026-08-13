"""yes 确认交互

prompt_toolkit 异步输入，支持 y/n/yes/no。
测试可通过注入回调函数绕过交互。
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from ..logger import get_logger

log = get_logger("security.confirmer")


class Confirmer:
    """yes 确认器

    生产环境用 prompt_toolkit 异步输入。
    测试环境通过 set_test_callback 注入回调绕过交互。
    """

    def __init__(self) -> None:
        self._test_callback: Optional[Callable[[str, bool], bool]] = None

    def set_test_callback(
        self, cb: Callable[[str, bool], bool]
    ) -> None:
        """注入测试回调：cb(prompt, default) -> bool

        设置后 ask_yes 不再走 prompt_toolkit，直接调 cb。
        """
        self._test_callback = cb

    def clear_test_callback(self) -> None:
        self._test_callback = None

    async def ask_yes(
        self,
        prompt: str,
        default: bool = False,
    ) -> bool:
        """询问 yes/no

        Args:
            prompt: 提示文本
            default: 空回车的默认值

        Returns:
            True=确认, False=拒绝
        """
        if self._test_callback is not None:
            return self._test_callback(prompt, default)

        hint = "[Y/n]" if default else "[y/N]"
        full_prompt = f"{prompt} {hint} "

        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.patch_stdout import patch_stdout

            session = PromptSession()
            with patch_stdout():
                raw = await session.prompt_async(full_prompt)
        except ImportError:
            # 回退到 input（同步，但 asyncio 环境下勉强可用）
            raw = input(full_prompt)

        raw = raw.strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")
