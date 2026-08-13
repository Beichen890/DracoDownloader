"""OpenDracoCLI 集中化错误目录

稳定错误码字符串契约（不随版本变更）。P3 AI 据此决定如何帮用户。
参考 DracoDownloader errors.py 的哲学。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# === 稳定错误码常量（契约，不会随版本变更） ===
ERR_SHELL_PARSE_ERROR = "draco.shell.parse_error"
ERR_SHELL_UNSUPPORTED_SYNTAX = "draco.shell.unsupported_syntax"
ERR_SHELL_COMMAND_NOT_FOUND = "draco.shell.command_not_found"
ERR_ALIAS_UNDEFINED = "draco.alias.undefined"
ERR_ALIAS_RECURSIVE = "draco.alias.recursive"
ERR_EXEC_TIMEOUT = "draco.exec.timeout"
ERR_EXEC_FAILED = "draco.exec.failed"
ERR_EXEC_CANCELLED = "draco.exec.cancelled"
ERR_HISTORY_DB_ERROR = "draco.history.db_error"

# P2 安全与风控
ERR_RISK_BLOCKED = "draco.risk.blocked"           # 风控拦截（通用）
ERR_RISK_DENIED = "draco.risk.denied"             # 用户拒绝 yes 确认
ERR_AUTH_FAILED = "draco.auth.failed"             # 身份验证失败
ERR_SANDBOX_VIOLATION = "draco.sandbox.violation"  # 沙箱路径违规
ERR_AUTH_NOT_CONFIGURED = "draco.auth.not_configured"  # 未配置密码

# P3 AI 智能层
ERR_AI_UNAVAILABLE = "draco.ai.unavailable"        # AI 未配置/不可用
ERR_AI_TIMEOUT = "draco.ai.timeout"                # AI 调用超时
ERR_AI_PARSE_FAILED = "draco.ai.parse_failed"      # AI 输出解析失败
ERR_AI_RISK_DENIED = "draco.ai.risk_denied"        # AI 风控拒绝

# P4 Agent 自动化
ERR_AGENT_SYNTAX = "draco.agent.syntax"            # 用户函数语法错误
ERR_AGENT_SANDBOX = "draco.agent.sandbox"          # 沙箱拦截
ERR_FUNCTION_NOT_FOUND = "draco.function.not_found"
ERR_AGENT_ARGS = "draco.agent.args"                # 参数解析失败
ERR_AGENT_EXEC_FAILED = "draco.agent.exec_failed"  # 函数执行失败


# 可翻译消息表（key = 错误码, value = 默认中文消息模板）
# 注意：占位符名不能与 make_error 的 keyword-only 参数 detail 冲突。
_DEFAULT_MESSAGES: Dict[str, str] = {
    ERR_SHELL_PARSE_ERROR: "输入解析失败: {reason}",
    ERR_SHELL_UNSUPPORTED_SYNTAX: "不支持的 shell 语法: {reason}",
    ERR_SHELL_COMMAND_NOT_FOUND: "命令不存在: {command}",
    ERR_ALIAS_UNDEFINED: "未定义的别名: {alias}",
    ERR_ALIAS_RECURSIVE: "别名递归超过 {depth} 层: {alias}",
    ERR_EXEC_TIMEOUT: "执行超时（{timeout}s）: {command}",
    ERR_EXEC_FAILED: "执行失败（退出码 {exit_code}）: {command}",
    ERR_EXEC_CANCELLED: "用户取消: {command}",
    ERR_HISTORY_DB_ERROR: "历史数据库错误: {reason}",
    ERR_RISK_BLOCKED: "风控拦截: {reason}",
    ERR_RISK_DENIED: "用户拒绝执行: {command}",
    ERR_AUTH_FAILED: "身份验证失败: {reason}",
    ERR_SANDBOX_VIOLATION: "沙箱拦截: 写入路径 {path} 不在白名单内",
    ERR_AUTH_NOT_CONFIGURED: "未配置密码，请先运行 opendracocli --setup-auth",
    ERR_AI_UNAVAILABLE: "AI 不可用: {reason}",
    ERR_AI_TIMEOUT: "AI 调用超时（{timeout}s）",
    ERR_AI_PARSE_FAILED: "AI 输出解析失败: {reason}",
    ERR_AI_RISK_DENIED: "AI 风控拒绝: {reason}",
    ERR_AGENT_SYNTAX: "函数语法错误: {reason}",
    ERR_AGENT_SANDBOX: "Agent 沙箱拦截: {reason}",
    ERR_FUNCTION_NOT_FOUND: "函数不存在: {name}",
    ERR_AGENT_ARGS: "参数解析失败: {reason}",
    ERR_AGENT_EXEC_FAILED: "函数执行失败: {reason}",
}

# 可重试标记表
_RETRYABLE: Dict[str, bool] = {
    ERR_SHELL_PARSE_ERROR: False,
    ERR_SHELL_UNSUPPORTED_SYNTAX: False,
    ERR_SHELL_COMMAND_NOT_FOUND: False,
    ERR_ALIAS_UNDEFINED: False,
    ERR_ALIAS_RECURSIVE: False,
    ERR_EXEC_TIMEOUT: True,
    ERR_EXEC_FAILED: False,  # 视情况，默认不可重试
    ERR_EXEC_CANCELLED: False,
    ERR_HISTORY_DB_ERROR: True,
    ERR_RISK_BLOCKED: False,
    ERR_RISK_DENIED: False,
    ERR_AUTH_FAILED: False,
    ERR_SANDBOX_VIOLATION: False,
    ERR_AUTH_NOT_CONFIGURED: False,
    ERR_AI_UNAVAILABLE: False,
    ERR_AI_TIMEOUT: True,
    ERR_AI_PARSE_FAILED: False,
    ERR_AI_RISK_DENIED: False,
    ERR_AGENT_SYNTAX: False,
    ERR_AGENT_SANDBOX: False,
    ERR_FUNCTION_NOT_FOUND: False,
    ERR_AGENT_ARGS: False,
    ERR_AGENT_EXEC_FAILED: False,
}


@dataclass
class DracoError(Exception):
    """OpenDracoCLI 统一异常

    Attributes:
        code: 稳定错误码
        message: 默认中文消息（已格式化）
        detail: 结构化上下文（供 P3 AI 消费）
        retryable: 是否可重试
    """

    code: str
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        # 兼容 Exception 的消息机制
        super().__init__(self.message or self.code)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def make_error(
    code: str,
    *,
    detail: Optional[Dict[str, Any]] = None,
    **fmt_kwargs: Any,
) -> DracoError:
    """根据错误码构造 DracoError

    Args:
        code: 稳定错误码常量
        detail: 结构化上下文（合并 fmt_kwargs）
        **fmt_kwargs: 消息模板格式化参数 + detail 补充

    Returns:
        DracoError 实例
    """
    template = _DEFAULT_MESSAGES.get(code, code)
    try:
        message = template.format(**fmt_kwargs)
    except (KeyError, IndexError):
        message = template

    merged_detail = dict(detail or {})
    merged_detail.update(fmt_kwargs)

    return DracoError(
        code=code,
        message=message,
        detail=merged_detail,
        retryable=_RETRYABLE.get(code, False),
    )


def is_retryable(err: DracoError) -> bool:
    """判断错误是否可重试"""
    return err.retryable
