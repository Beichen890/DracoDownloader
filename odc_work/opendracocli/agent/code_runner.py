"""沙箱化代码执行 — AST 黑名单 + 确认

参考 DracoHub sub_agent/security.py 的安全思路，但避免 RestrictedPython 重依赖。

两层安全:
  1. 用户 functions.py（用户自己写）→ 仅语法检查，信任
  2. AI 生成代码 → strict=True，AST 遍历检查禁用 import / 危险内置

禁用清单（strict 模式，仅对 AI 生成代码）:
  - import: subprocess / ctypes / multiprocessing / shutil / os.system / os.popen
  - 内置: eval / exec / compile / __import__ / globals / locals / breakpoint
  - 双下划线属性访问: __class__ / __bases__ / __subclasses__ 等（防逃逸）
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import ERR_AGENT_SANDBOX, ERR_AGENT_SYNTAX, make_error
from ..logger import get_logger

log = get_logger("agent.code_runner")


# strict 模式禁用的模块名
FORBIDDEN_MODULES: set[str] = {
    "subprocess",
    "ctypes",
    "multiprocessing",
    "shutil",
    "pty",
    "socketserver",
    "http",
    "ftplib",
    "telnetlib",
    "smtplib",
    "xmlrpc",
}

# strict 模式禁用的内置函数名
FORBIDDEN_BUILTINS: set[str] = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "breakpoint",
    "memoryview",
}

# 禁止访问的双下划线属性（防止类型逃逸）
FORBIDDEN_DUNDER_ATTRS: set[str] = {
    "__class__",
    "__bases__",
    "__subclasses__",
    "__mro__",
    "__globals__",
    "__builtins__",
    "__code__",
    "__dict__",
}


class SafetyViolation(Exception):
    """安全检查违规"""

    def __init__(self, violations: List[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass
class _ASTVisitor(ast.NodeVisitor):
    """AST 遍历器，收集违规"""

    violations: List[str]
    strict: bool

    def _add(self, msg: str) -> None:
        self.violations.append(msg)

    def visit_Import(self, node: ast.Import) -> None:
        if self.strict:
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    self._add(f"禁用 import: {alias.name}（行 {node.lineno}）")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.strict and node.module:
            root = node.module.split(".")[0]
            if root in FORBIDDEN_MODULES:
                self._add(f"禁用 from import: {node.module}（行 {node.lineno}）")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.strict:
            # 检查调用禁用内置
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
                self._add(
                    f"禁用调用内置函数: {func.id}（行 {node.lineno}）"
                )
            # 检查 os.system / os.popen
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr in ("system", "popen", "execv", "execve", "spawnl", "spawnle")
            ):
                self._add(
                    f"禁用 os.{func.attr}（行 {node.lineno}），请用 ctx.shell()"
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # 双下划线属性访问（防类型逃逸）— strict 和非 strict 都检查
        if node.attr in FORBIDDEN_DUNDER_ATTRS:
            self._add(
                f"禁用访问属性: {node.attr}（行 {node.lineno}）"
            )
        self.generic_visit(node)


class CodeRunner:
    """沙箱化代码执行器"""

    def check_safety(self, code: str, *, strict: bool = False) -> List[str]:
        """检查代码安全性

        Args:
            code: Python 源码
            strict: True=AI 生成代码（检查 import/内置）；False=用户函数（仅 dunder）

        Returns:
            违规列表（空列表表示通过）
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"语法错误: {e}"]

        visitor = _ASTVisitor(violations=[], strict=strict)
        visitor.visit(tree)
        return visitor.violations

    def exec_function_code(
        self,
        code: str,
        namespace: Optional[Dict[str, Any]] = None,
        *,
        strict: bool = False,
    ) -> Tuple[Optional[Callable], str, List[str]]:
        """执行函数源码，返回第一个定义的函数

        Args:
            code: Python 函数源码（可含 docstring）
            namespace: 执行命名空间（可注入 helper）
            strict: 是否严格安全检查

        Returns:
            (func, func_name, violations):
                func: 第一个定义的函数（None 表示失败）
                func_name: 函数名
                violations: 安全违规列表（非空时 func 为 None）

        Raises:
            DracoError: 语法错误（strict=False 时）或沙箱违规（strict=True 时）
        """
        # 1. 语法检查
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            raise make_error(ERR_AGENT_SYNTAX, reason=f"语法错误: {e}")

        # 2. 安全检查
        violations = self.check_safety(code, strict=strict)
        if violations:
            if strict:
                raise make_error(
                    ERR_AGENT_SANDBOX,
                    reason="; ".join(violations),
                )
            # 非 strict（用户函数）有 dunder 违规也拒绝
            raise make_error(
                ERR_AGENT_SANDBOX,
                reason="; ".join(violations),
            )

        # 3. 提取函数名
        func_name = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                break

        # 4. exec 到命名空间
        ns: Dict[str, Any] = dict(namespace or {})
        # 提供标准库（用户函数可用）
        import builtins

        ns.setdefault("__builtins__", builtins.__dict__)
        try:
            exec(compile(tree, "<functions>", "exec"), ns)
        except Exception as e:
            raise make_error(ERR_AGENT_SYNTAX, reason=f"执行失败: {e}")

        # 5. 取出函数
        func = ns.get(func_name) if func_name else None
        if not callable(func):
            return None, func_name, violations

        return func, func_name, violations

    def extract_functions(self, code: str) -> List[str]:
        """提取源码中所有函数名"""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        names: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
        return names


# 全局单例
_global_runner: Optional[CodeRunner] = None


def get_global_runner() -> CodeRunner:
    global _global_runner
    if _global_runner is None:
        _global_runner = CodeRunner()
    return _global_runner
