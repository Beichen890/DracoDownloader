"""Draco 内置函数 — 用 Python 库实现跨平台一致的复杂/常用任务

简单命令直接交给原生 shell（bash/cmd），不映射。
这里只放"shell 不好写/跨平台不一致/需要组合多步"的任务。

函数签名约定：def name(ctx, ...) -> Any
  ctx: AgentContext，提供 ctx.shell() 调原生命令、ctx.cwd、ctx.config
  其余参数由 arg_parser 解析（支持 key=value 和位置参数）
"""
