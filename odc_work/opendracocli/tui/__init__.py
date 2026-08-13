"""OpenDracoCLI TUI 渲染层 (P5)

P5 把渲染层从 prompt_toolkit+rich 迁移到 textual，内核 (shell/hooks/history/events) 零改动。
- tui/theme.py          主题系统 (纯数据 + textual CSS 生成)
- tui/context.py        RenderContext 渲染数据模型
- tui/renderer.py       Renderer 协议 + SimpleRenderer 降级实现
- tui/keymap.py         快捷键绑定
- tui/startup/          可定制启动界面
- tui/widgets/          textual Widget 集
- tui/app.py            DracoApp 主应用
"""

__version__ = "0.1.0"
