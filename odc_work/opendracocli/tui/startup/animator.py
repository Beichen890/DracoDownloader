"""启动动画生成器

提供"生成某一帧文本"的纯函数式 API, 不直接做终端 IO,
便于 textual Widget 按帧调用。另提供同步 fallback_play 方法用于非 textual 降级。
"""

from __future__ import annotations

import sys
import time
from typing import Callable, Optional


class StartupAnimator:
    """启动动画播放 (spinner/progress/typewriter)

    注意: P5 在 textual App 内显示, 实际动画由 App 驱动刷新。
    本类提供"生成某一帧文本"的纯函数式 API, 不直接做终端 IO,
    便于 textual Widget 按帧调用。也提供一个同步 fallback_play 方法用于非 textual 降级。
    """

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    @staticmethod
    def frame(animation: str, text: str, frame_idx: int, total_frames: int) -> str:
        """生成第 frame_idx 帧的文本。

        - spinner: 前置 SPINNER_FRAMES[frame_idx % len]
        - progress: 生成 [####    ] 25% 风格进度条 + text
        - typewriter: 返回 text 的前 frame_idx 个字符
        - none: 直接返回 text
        """
        if animation == "spinner":
            frames = StartupAnimator.SPINNER_FRAMES
            idx = frame_idx % len(frames)
            return f"{frames[idx]} {text}"
        if animation == "progress":
            total = total_frames if total_frames > 0 else 1
            idx = frame_idx if frame_idx >= 0 else 0
            if idx > total - 1:
                idx = total - 1
            bar_width = 20
            filled = int(bar_width * (idx + 1) / total)
            if filled < 0:
                filled = 0
            if filled > bar_width:
                filled = bar_width
            bar = "#" * filled + " " * (bar_width - filled)
            pct = int(100 * (idx + 1) / total)
            return f"[{bar}] {pct}% {text}"
        if animation == "typewriter":
            n = frame_idx if frame_idx >= 0 else 0
            return text[:n]
        # none 或未知动画: 直接返回文本
        return text

    @staticmethod
    def total_frames(animation: str, text: str, duration_ms: int) -> int:
        """计算总帧数。

        spinner 固定按 duration_ms/100 帧;
        progress 固定 20 帧; typewriter = len(text) (每帧出一个字符, 最少 1);
        none = 1。
        """
        if animation == "spinner":
            return max(1, duration_ms // 100)
        if animation == "progress":
            return 20
        if animation == "typewriter":
            return max(1, len(text))
        # none
        return 1

    @staticmethod
    def fallback_play(animation: str, text: str, duration_ms: int,
                      skip_on_key: bool = True,
                      sleep: Callable[[float], None] = time.sleep) -> None:
        """非 textual 降级同步播放: 在当前行用 \\r 覆盖刷新帧。

        用注入的 sleep 便于测试。skip_on_key 在非阻塞 stdin 不可用时忽略。
        此方法只在 SimpleRenderer 降级路径调用, textual App 不用它。
        """
        total = StartupAnimator.total_frames(animation, text, duration_ms)
        if total <= 0:
            total = 1
        # 每帧间隔 (秒); duration_ms 为 0 时间隔为 0
        interval = (duration_ms / 1000.0) / total if total > 0 else 0.0

        for i in range(total):
            frame_text = StartupAnimator.frame(animation, text, i, total)
            try:
                sys.stdout.write(f"\r{frame_text}")
                sys.stdout.flush()
            except Exception:
                # 终端不可写时静默放弃 (降级路径不应抛异常)
                return
            try:
                sleep(interval)
            except Exception:
                # 注入的 sleep 异常不应阻断播放
                pass
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            return
