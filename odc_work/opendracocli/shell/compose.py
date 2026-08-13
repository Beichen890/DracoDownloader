"""ComposeEngine — 组合语义引擎

放弃外部 shell 后，管道/重定向/逻辑操作符不再透传 shell，由本引擎自行编排：

  - 管道 |   ：上游 stdout 作为下游 stdin（内存传递，零进程开销）
  - 重定向   ：> / >> 写文件，< 从文件读 stdin，2> 写 stderr
  - 逻辑符   ：&& 上游成功才执行下游，|| 上游失败才执行下游，; 顺序执行
  - cwd 跟踪 ：cd 等命令返回 new_cwd，后续节点使用新 cwd（支持 cd dir && ls）

单命令节点委托 NativeDispatcher 路由（Rust→Python→exec）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from ..errors import DracoError
from ..logger import get_logger
from .dispatcher import NativeDispatcher, _resolve
from .executor import ExecResult, serialize_ir
from .ir import OP_AND, OP_OR, OP_PIPE, OP_SEQ, CommandIR

log = get_logger("shell.compose")


class ComposeEngine:
    """组合语义引擎 — 编排多命令节点"""

    def __init__(self, dispatcher: NativeDispatcher) -> None:
        self._dispatcher = dispatcher

    async def execute(
        self,
        ir: CommandIR,
        cwd: str,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> ExecResult:
        """编排整条 IR 的执行

        Args:
            ir: 解析后的命令 IR
            cwd: 起始工作目录
            timeout: 单命令超时秒数
            session_id: 会话 ID

        Returns:
            ExecResult（最终节点结果 + 重定向应用）
        """
        started = time.monotonic()

        # --- stdin 重定向（<）：读文件作为首个节点的 stdin ---
        stdin_data: Optional[str] = None
        for r in ir.redirects:
            if r.kind == "stdin":
                path = _resolve(cwd, r.target)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        stdin_data = fh.read()
                except OSError as e:
                    return ExecResult(
                        exit_code=1,
                        stderr=f"重定向失败: {r.target}: {e}",
                        mapped_command=ir.raw_input,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

        # --- 遍历节点，按操作符决策 ---
        current_cwd = cwd
        current_stdin = stdin_data
        last_result = ExecResult(exit_code=0, mapped_command=ir.raw_input)
        last_exit = 0
        # 非管道节点的 stdout/stderr 累积输出（管道上游 stdout 喂给下游，不直接输出）
        out_buffer = ""
        err_buffer = ""

        for i, node in enumerate(ir.nodes):
            # 逻辑操作符决策（operators[i-1] 是 node 与前一节点间的操作符）
            if i > 0:
                op = ir.operators[i - 1]
                if op == OP_AND and last_exit != 0:
                    # && 上游失败：跳过此节点（短路）
                    continue
                if op == OP_OR and last_exit == 0:
                    # || 上游成功：跳过此节点（短路）
                    continue
                # OP_PIPE / OP_SEQ：继续（PIPE 的 stdin 已由上游设置）

            # 执行单节点
            res = await self._dispatcher.execute_node(
                node,
                cwd=current_cwd,
                stdin=current_stdin,
                timeout=timeout,
                session_id=session_id,
            )

            last_result = res
            last_exit = res.exit_code if res.exit_code is not None else 0

            # cd 等命令更新 cwd，后续节点用新目录（支持 cd dir && ls）
            if res.new_cwd:
                current_cwd = res.new_cwd

            # 管道：下一节点的 stdin = 本节点 stdout（不直接输出）
            # 非管道：本节点 stdout/stderr 累积到输出缓冲
            if i < len(ir.operators) and ir.operators[i] == OP_PIPE:
                current_stdin = res.stdout
            else:
                out_buffer += res.stdout
                err_buffer += res.stderr
                current_stdin = None

        # --- stdout / stderr 重定向（作用于累积输出）---
        for r in ir.redirects:
            if r.kind in ("stdout", "append"):
                path = _resolve(current_cwd, r.target)
                try:
                    mode = "a" if r.kind == "append" else "w"
                    with open(path, mode, encoding="utf-8") as fh:
                        fh.write(out_buffer)
                    out_buffer = ""  # 已重定向，不再输出到终端
                except OSError as e:
                    err_buffer += f"\n重定向失败: {r.target}: {e}"
                    if last_exit == 0:
                        last_exit = 1
            elif r.kind == "stderr":
                path = _resolve(current_cwd, r.target)
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(err_buffer)
                    err_buffer = ""
                except OSError as e:
                    err_buffer += f"\n重定向失败: {r.target}: {e}"

        last_result.stdout = out_buffer
        last_result.stderr = err_buffer
        last_result.exit_code = last_exit
        # mapped_command 反映展开后的 IR 结构（含 alias 展开）
        last_result.mapped_command = serialize_ir(ir)
        last_result.duration_ms = int((time.monotonic() - started) * 1000)
        # cd 的 new_cwd 透传给上层（pipeline 会应用到 session）
        if current_cwd != cwd:
            last_result.new_cwd = current_cwd
        return last_result
