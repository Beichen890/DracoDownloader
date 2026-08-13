"""FunctionRegistry — 用户函数注册表

加载用户的 ~/.opendracocli/functions.py，把其中的顶层函数注册为"命令"。
函数名即命令名，输入函数名时走 Python 通道。

加载策略:
  - exec functions.py 到隔离命名空间
  - 提取所有顶层 callable（排除 ctx 参数约定不符的）
  - 函数签名约定：def name(ctx, ...) -> Any（首参数为 ctx）

用户函数信任（用户自己写），不做 AST 黑名单，仅 dunder 检查（code_runner 默认）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..errors import ERR_AGENT_SYNTAX, make_error
from ..logger import get_logger
from .code_runner import CodeRunner, get_global_runner

log = get_logger("agent.function_registry")


@dataclass
class RegisteredFunction:
    """已注册的函数"""

    name: str
    func: Callable
    signature: inspect.Signature
    doc: str
    source: str
    is_builtin: bool = False
    is_async: bool = False

    @property
    def param_names_no_ctx(self) -> List[str]:
        """参数名列表（去掉首参 ctx）"""
        names = list(self.signature.parameters.keys())
        if names and names[0] == "ctx":
            return names[1:]
        return names

    @property
    def param_types(self) -> Dict[str, Any]:
        """参数名 → type hint"""
        result: Dict[str, Any] = {}
        for name, p in self.signature.parameters.items():
            if name == "ctx":
                continue
            if p.annotation is not inspect.Parameter.empty:
                result[name] = p.annotation
            else:
                result[name] = str
        return result

    @property
    def defaults(self) -> Dict[str, Any]:
        """参数名 → 默认值（仅有默认值的）"""
        result: Dict[str, Any] = {}
        for name, p in self.signature.parameters.items():
            if name == "ctx":
                continue
            if p.default is not inspect.Parameter.empty:
                result[name] = p.default
        return result


class FunctionRegistry:
    """函数注册表"""

    def __init__(self, runner: Optional[CodeRunner] = None) -> None:
        self._runner = runner or get_global_runner()
        self._functions: Dict[str, RegisteredFunction] = {}
        self._user_file: Optional[Path] = None

    def register(
        self,
        name: str,
        func: Callable,
        *,
        builtin: bool = False,
    ) -> None:
        """注册一个函数

        Args:
            name: 函数名（命令名）
            func: 可调用对象
            builtin: 是否内置
        """
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError) as e:
            log.warning("无法获取函数签名 %s: %s", name, e)
            return

        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            source = f"# {name}（源码不可用）"

        is_async = inspect.iscoroutinefunction(func)
        doc = inspect.getdoc(func) or ""

        self._functions[name] = RegisteredFunction(
            name=name,
            func=func,
            signature=sig,
            doc=doc,
            source=source,
            is_builtin=builtin,
            is_async=is_async,
        )
        log.debug("registered function: %s (async=%s)", name, is_async)

    def load_user_file(self, path: Path) -> int:
        """从用户 functions.py 加载函数

        Args:
            path: functions.py 路径

        Returns:
            加载的函数数量

        Raises:
            DracoError: 文件不存在 / 语法错误 / 加载异常
        """
        if not path.exists():
            log.info("functions file not exist: %s", path)
            return 0

        try:
            code = path.read_text(encoding="utf-8")
        except OSError as e:
            raise make_error(ERR_AGENT_SYNTAX, reason=f"读取失败: {e}")

        # exec 到隔离命名空间（用户函数信任，仅 dunder 检查）
        import builtins

        ns: Dict[str, Any] = {"__builtins__": builtins.__dict__, "__name__": "opendracocli_functions"}
        try:
            exec(compile(code, str(path), "exec"), ns)
        except SyntaxError as e:
            raise make_error(ERR_AGENT_SYNTAX, reason=f"语法错误: {e}")
        except Exception as e:
            raise make_error(ERR_AGENT_SYNTAX, reason=f"加载失败: {e}")

        # 提取顶层函数（排除模块/类/导入）
        count = 0
        for name, obj in ns.items():
            if name.startswith("_"):
                continue
            if not callable(obj):
                continue
            # 只接受函数（排除类、模块）
            if not (inspect.isfunction(obj) or inspect.iscoroutinefunction(obj)):
                continue
            # 签名约定：首参数为 ctx
            try:
                sig = inspect.signature(obj)
                params = list(sig.parameters.keys())
                if not params or params[0] != "ctx":
                    log.debug("skip %s: 首参数不是 ctx", name)
                    continue
            except (ValueError, TypeError):
                continue

            self.register(name, obj, builtin=False)
            count += 1

        self._user_file = path
        log.info("loaded %d functions from %s", count, path)
        return count

    def reload(self) -> int:
        """重新加载用户函数文件

        Returns:
            加载的函数数量

        Raises:
            DracoError: 未设置文件 / 加载失败
        """
        if self._user_file is None:
            log.warning("reload called but no user file set")
            return 0
        self.clear_user_functions()
        return self.load_user_file(self._user_file)

    def clear_user_functions(self) -> int:
        """清空用户函数（保留 builtin）

        Returns:
            被清除的用户函数数量
        """
        user_names = [k for k, v in self._functions.items() if not v.is_builtin]
        self._functions = {
            k: v for k, v in self._functions.items() if v.is_builtin
        }
        if user_names:
            log.info("cleared %d user functions", len(user_names))
        return len(user_names)

    def get(self, name: str) -> Optional[RegisteredFunction]:
        """按名获取函数"""
        return self._functions.get(name)

    def match(self, first_token: str) -> Optional[RegisteredFunction]:
        """路由判断：首 token 是否命中已注册函数

        Args:
            first_token: 用户输入的第一个 token

        Returns:
            RegisteredFunction 或 None
        """
        if not first_token:
            return None
        return self._functions.get(first_token)

    def list(self) -> Dict[str, RegisteredFunction]:
        """列出所有已注册函数"""
        return dict(self._functions)

    def list_user(self) -> Dict[str, RegisteredFunction]:
        """仅列出用户函数（非内置）"""
        return {k: v for k, v in self._functions.items() if not v.is_builtin}

    def set_user_file(self, path: Path) -> None:
        """设置用户函数文件路径（不立即加载）"""
        self._user_file = path


# 全局单例
_global_registry: Optional[FunctionRegistry] = None


def get_global_registry() -> FunctionRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = FunctionRegistry()
    return _global_registry


def reset_global_registry() -> None:
    """重置全局注册表（测试用 / /agent reload）"""
    global _global_registry
    _global_registry = None
