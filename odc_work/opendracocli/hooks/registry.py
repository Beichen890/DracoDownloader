"""钩子注册表 — 按优先级链式调用"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..logger import get_logger
from .base import BlockResult, ExecContext, ExecDecision, PostExecHook, PreExecHook

log = get_logger("hooks.registry")


class HookRegistry:
    """钩子注册表"""

    def __init__(self) -> None:
        self._pre_hooks: List[PreExecHook] = []
        self._post_hooks: List[PostExecHook] = []

    def register_pre(self, hook: PreExecHook) -> None:
        self._pre_hooks.append(hook)
        self._pre_hooks.sort(key=lambda h: h.priority)
        log.debug("registered pre-exec hook: %s (priority=%d)", type(hook).__name__, hook.priority)

    def register_post(self, hook: PostExecHook) -> None:
        self._post_hooks.append(hook)
        self._post_hooks.sort(key=lambda h: h.priority)
        log.debug("registered post-exec hook: %s (priority=%d)", type(hook).__name__, hook.priority)

    def clear(self) -> None:
        self._pre_hooks.clear()
        self._post_hooks.clear()

    async def run_pre(self, ctx: ExecContext) -> Tuple[ExecDecision, Optional[BlockResult]]:
        """按优先级运行所有 pre-exec 钩子

        任一返回 BLOCK 则停止并返回阻断结果。
        """
        for hook in self._pre_hooks:
            try:
                decision, block = await hook.pre_exec(ctx)
            except Exception:
                log.exception("pre-exec hook failed: %s", type(hook).__name__)
                # 钩子故障不阻断主流程，按 CONTINUE 处理
                continue
            if decision == ExecDecision.BLOCK:
                log.info("execution blocked by %s: %s", type(hook).__name__, block.reason if block else "")
                return decision, block
        return ExecDecision.CONTINUE, None

    async def run_post(self, ctx: ExecContext, result) -> None:
        """按优先级运行所有 post-exec 钩子（只读）"""
        for hook in self._post_hooks:
            try:
                await hook.post_exec(ctx, result)
            except Exception:
                log.exception("post-exec hook failed: %s", type(hook).__name__)


_global_registry: HookRegistry | None = None


def get_global_registry() -> HookRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = HookRegistry()
    return _global_registry


def set_global_registry(reg: HookRegistry) -> None:
    global _global_registry
    _global_registry = reg
