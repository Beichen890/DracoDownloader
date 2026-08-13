"""OpenDracoCLI 事件总线

轻量同步事件总线：subscribe / publish。
P1 内置事件:
  - CommandExecuted  (成功执行)
  - CommandFailed    (解析/映射/别名阶段错误)
  - SessionStarted   (TUI 启动)
  - SessionEnded     (TUI 退出)

后续阶段通过订阅事件接入：P2 风控统计、P3 AI 感知、P4 Agent 通道共享历史。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


# === 事件类型常量 ===
EVT_COMMAND_EXECUTED = "CommandExecuted"
EVT_COMMAND_FAILED = "CommandFailed"
EVT_SESSION_STARTED = "SessionStarted"
EVT_SESSION_ENDED = "SessionEnded"

# P2 安全与风控
EVT_RISK_BLOCKED = "RiskBlocked"          # 高危命令被拦截
EVT_AUTH_REQUIRED = "AuthRequired"        # 触发身份验证
EVT_SANDBOX_VIOLATION = "SandboxViolation"  # 沙箱拦截

# P3 AI 智能层
EVT_AI_CORRECTION = "AICorrection"        # AI 纠错建议
EVT_AI_RISK_ASSESS = "AIRiskAssess"       # AI 风险评估
EVT_AI_PERCEPTION = "AIPerception"        # 感知建议
EVT_AI_EMOTION_UPDATE = "AIEmotionUpdate"  # 情感更新

# P4 Agent 自动化
EVT_AGENT_EXECUTED = "AgentExecuted"            # Python 通道执行（成功/失败）
EVT_FUNCTION_CALLED = "FunctionCalled"          # 用户函数被调用
EVT_AGENT_CODE_GENERATED = "AgentCodeGenerated"  # AI 生成代码


@dataclass
class Event:
    """事件基类

    Attributes:
        type: 事件类型字符串
        payload: 任意结构化数据
        timestamp: Unix 时间戳（发布时填充）
    """

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


# 事件处理器签名：接收 Event，无返回值（同步）
EventHandler = Callable[[Event], None]


class EventBus:
    """同步事件总线

    P1 单进程同步模型足够。P4 引入多通道若需异步可换 AsyncEventBus，
    接口（subscribe/publish）保持不变。
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """订阅事件"""
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """取消订阅"""
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def publish(self, event: Event) -> None:
        """发布事件

        同步调用所有订阅者。某个订阅者抛异常不影响其他订阅者，
        但会记录到日志（避免一个订阅者故障拖垮整个管线）。
        """
        import time

        if event.timestamp == 0.0:
            event.timestamp = time.time()

        handlers = self._handlers.get(event.type, [])
        for h in handlers:
            try:
                h(event)
            except Exception:
                # 订阅者故障不应影响主流程
                from .logger import get_logger

                get_logger("events").exception(
                    "event handler failed: type=%s", event.type
                )


_global_bus: EventBus | None = None


def get_global_bus() -> EventBus:
    """获取全局事件总线单例"""
    global _global_bus
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus


def set_global_bus(bus: EventBus) -> None:
    """注入全局事件总线（测试用）"""
    global _global_bus
    _global_bus = bus
