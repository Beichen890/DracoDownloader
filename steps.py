"""
DracoDownloader 任务步骤模型

将下载过程分解为最小执行单元（TaskStep），让 AI Agent 可以：
- 预览一个下载任务会经过哪些阶段
- 在任意阶段介入或观察
- 针对单步失败做精细重试

步骤生命周期: pending → running → completed / failed / skipped
"""

import asyncio
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Any, List, Dict
from .logger import get_logger
from .errors import DracoError

log = get_logger('steps')


class StepStatus(Enum):
    """步骤状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# 步骤名称常量（稳定契约）
STEP_PROBE = "probe"
STEP_DOWNLOAD = "download"
STEP_MERGE = "merge"
STEP_VERIFY = "verify"
STEP_SEED = "seed"


@dataclass
class StepResult:
    """单步执行结果

    Attributes:
        success: 是否成功
        data: 该步骤产出的数据（如 probe 得到的 metadata）
        error: 失败时的错误对象
        duration: 耗时（秒）
    """
    success: bool
    data: Any = None
    error: Optional[DracoError] = None
    duration: float = 0.0


@dataclass
class TaskStep:
    """单个执行步骤

    Attributes:
        name: 步骤名（稳定字符串，如 "probe"/"download"/"merge"）
        title: 人类可读标题
        coroutine_factory: 无参异步函数，返回步骤数据
        status: 当前状态
        result: 最近一次执行结果
        retryable: 失败后是否可重试（默认 True）
        skippable: 是否允许跳过（如 verify 步骤）
    """
    name: str
    title: str
    coroutine_factory: Callable[[], Awaitable[Any]]
    status: StepStatus = StepStatus.PENDING
    result: Optional[StepResult] = None
    retryable: bool = True
    skippable: bool = False

    async def run(self) -> StepResult:
        """执行该步骤，更新状态并返回结果"""
        self.status = StepStatus.RUNNING
        start = time.time()
        try:
            data = await self.coroutine_factory()
            duration = time.time() - start
            self.result = StepResult(success=True, data=data, duration=duration)
            self.status = StepStatus.COMPLETED
            log.debug(f"Step '{self.name}' completed in {duration:.2f}s")
            return self.result
        except asyncio.CancelledError:
            duration = time.time() - start
            self.result = StepResult(
                success=False,
                error=DracoError(code="draco.cancelled", message="步骤被取消"),
                duration=duration,
            )
            self.status = StepStatus.FAILED
            raise
        except DracoError as e:
            duration = time.time() - start
            self.result = StepResult(success=False, error=e, duration=duration)
            self.status = StepStatus.FAILED
            log.warning(f"Step '{self.name}' failed: {e.message}")
            return self.result
        except Exception as e:
            duration = time.time() - start
            err = DracoError(
                code="draco.step_failed",
                message=f"步骤 '{self.name}' 异常: {e}",
                retryable=False,
            )
            self.result = StepResult(success=False, error=err, duration=duration)
            self.status = StepStatus.FAILED
            log.warning(f"Step '{self.name}' failed: {e}")
            return self.result

    def skip(self, reason: str = ""):
        """跳过该步骤"""
        self.status = StepStatus.SKIPPED
        self.result = StepResult(success=True, data={"skipped": True, "reason": reason})

    @property
    def is_terminal(self) -> bool:
        return self.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED)


class StepPipeline:
    """步骤管线 - 按顺序执行一组 TaskStep

    特性:
    - 任一步骤失败默认终止后续（可通过 on_step_failed 回调改写）
    - 可重试步骤失败后允许手动 retry()
    - 支持预览（describe()）让 Agent 提前看到执行计划
    """

    def __init__(self, steps: Optional[List[TaskStep]] = None,
                 on_step_failed: Optional[Callable[[TaskStep, StepResult], bool]] = None):
        """
        Args:
            steps: 步骤列表
            on_step_failed: 步骤失败回调；返回 True 表示继续执行后续步骤，False 终止
        """
        self._steps: List[TaskStep] = list(steps) if steps else []
        self._on_failed = on_step_failed

    def add(self, step: TaskStep) -> 'StepPipeline':
        """追加步骤（链式）"""
        self._steps.append(step)
        return self

    def describe(self) -> List[Dict[str, str]]:
        """返回执行计划描述（供 Agent 预览）"""
        return [
            {"name": s.name, "title": s.title, "status": s.status.value}
            for s in self._steps
        ]

    async def execute(self) -> List[StepResult]:
        """顺序执行所有步骤

        Returns:
            每个步骤的结果列表
        """
        results: List[StepResult] = []
        for step in self._steps:
            if step.status == StepStatus.COMPLETED:
                # 已完成的步骤跳过（重试场景）
                results.append(step.result or StepResult(success=True))
                continue
            result = await step.run()
            results.append(result)
            if not result.success:
                should_continue = False
                if self._on_failed is not None:
                    try:
                        should_continue = self._on_failed(step, result)
                    except Exception:
                        should_continue = False
                if not should_continue:
                    break
        return results

    async def retry_step(self, name: str) -> Optional[StepResult]:
        """重试指定名称的步骤（仅对 retryable=True 的失败步骤有效）

        Args:
            name: 步骤名

        Returns:
            重试后的结果，若步骤不存在或不可重试则返回 None
        """
        for step in self._steps:
            if step.name != name:
                continue
            if not step.retryable or step.status != StepStatus.FAILED:
                return None
            step.status = StepStatus.PENDING
            return await step.run()
        return None

    @property
    def steps(self) -> List[TaskStep]:
        return list(self._steps)

    def get_step(self, name: str) -> Optional[TaskStep]:
        for step in self._steps:
            if step.name == name:
                return step
        return None


def build_standard_pipeline(
    probe_fn: Callable[[], Awaitable[Any]],
    download_fn: Callable[[], Awaitable[Any]],
    merge_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    verify_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    seed_fn: Optional[Callable[[], Awaitable[Any]]] = None,
) -> StepPipeline:
    """构造标准下载管线（probe → download → merge → verify → seed）

    Args:
        probe_fn: 探测元信息
        download_fn: 执行下载
        merge_fn: 合并分片（可选）
        verify_fn: 文件校验（可选）
        seed_fn: BT 做种（可选）

    Returns:
        StepPipeline 实例
    """
    pipeline = StepPipeline()
    pipeline.add(TaskStep(name=STEP_PROBE, title="探测", coroutine_factory=probe_fn))
    pipeline.add(TaskStep(name=STEP_DOWNLOAD, title="下载", coroutine_factory=download_fn))
    if merge_fn is not None:
        pipeline.add(TaskStep(name=STEP_MERGE, title="合并分片", coroutine_factory=merge_fn))
    if verify_fn is not None:
        pipeline.add(TaskStep(
            name=STEP_VERIFY, title="校验", coroutine_factory=verify_fn,
            skippable=True,
        ))
    if seed_fn is not None:
        pipeline.add(TaskStep(name=STEP_SEED, title="做种", coroutine_factory=seed_fn))
    return pipeline


__all__ = [
    "StepStatus",
    "StepResult",
    "TaskStep",
    "StepPipeline",
    "build_standard_pipeline",
    "STEP_PROBE",
    "STEP_DOWNLOAD",
    "STEP_MERGE",
    "STEP_VERIFY",
    "STEP_SEED",
]
