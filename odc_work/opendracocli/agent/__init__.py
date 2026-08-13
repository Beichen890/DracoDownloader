"""P4 Agent 自动化 — 代码即行动

参考 DracoHub CodingAgent 的"代码即行动"原则：用户描述意图 → AI 生成 Python 函数 →
确认后执行，直接调 Python 库完成，不经过 shell。

模块组成:
  - context.py            AgentContext：用户函数的执行上下文（ctx.shell/log/print）
  - arg_parser.py         函数参数解析（positional + keyword）
  - function_registry.py  FunctionRegistry：加载用户 functions.py
  - code_runner.py        沙箱化执行（AST 黑名单 + 确认）
  - python_executor.py    PythonChannelExecutor：独立通道，共享历史与事件
  - code_generator.py     CodeGenerator：AI 生成函数（复用 P3 LLMClient）
  - templates.py          常用自动化模板
"""

from .context import AgentContext
from .arg_parser import parse_call_args, ArgParseError
from .function_registry import FunctionRegistry, RegisteredFunction, get_global_registry
from .code_runner import CodeRunner, SafetyViolation
from .python_executor import PythonChannelExecutor, AgentExecResult
from .code_generator import CodeGenerator, GeneratedFunction
from .templates import list_templates, get_template, TEMPLATES

__all__ = [
    "AgentContext",
    "parse_call_args",
    "ArgParseError",
    "FunctionRegistry",
    "RegisteredFunction",
    "get_global_registry",
    "CodeRunner",
    "SafetyViolation",
    "PythonChannelExecutor",
    "AgentExecResult",
    "CodeGenerator",
    "GeneratedFunction",
    "list_templates",
    "get_template",
    "TEMPLATES",
]
