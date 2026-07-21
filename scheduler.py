"""
任务调度器 - 管理并发下载任务
支持队列、并发控制、重试、超时和取消传播
"""

import asyncio
import uuid
import time
from typing import Optional, Dict, Any, Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum

from .logger import get_logger

log = get_logger('scheduler')

# 默认超时（秒）
DEFAULT_TASK_TIMEOUT = 3600  # 1 小时


class TaskStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadTask:
    """下载任务"""
    id: str
    handle: Any
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    timeout: float = DEFAULT_TASK_TIMEOUT
    bytes_downloaded: int = 0
    bytes_total: int = 0


class Scheduler:
    """
    下载任务调度器 - 带队列、并发控制、重试和超时

    用法：
        scheduler = Scheduler(max_concurrent=5)

        # 注册执行器（core 提供实际的 driver.download 调用）
        scheduler.set_executor(my_executor)

        # 添加任务
        task_id = scheduler.add(handle)

        # 等待结果
        result = await scheduler.wait_for(task_id)
    """

    def __init__(self, max_concurrent: int = 5):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.max_concurrent = max_concurrent
        self._tasks: Dict[str, DownloadTask] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running: Dict[str, asyncio.Task] = {}
        self._futures: Dict[str, asyncio.Future] = {}  # task_id → Future[DownloadResult]
        self._worker_task: Optional[asyncio.Task] = None

        # 外部执行器：async def executor(handle, task_id, progress_callback) -> DownloadResult
        self._executor: Optional[Callable] = None

        # 取消信号桥
        self._cancel_events: Dict[str, asyncio.Event] = {}

    def set_executor(self, executor: Callable):
        """设置任务执行器（由 core 注入）"""
        self._executor = executor

    def add(self, handle, timeout: float = DEFAULT_TASK_TIMEOUT) -> str:
        """添加下载任务到队列，返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(id=task_id, handle=handle, timeout=timeout)
        self._tasks[task_id] = task
        self._futures[task_id] = asyncio.get_running_loop().create_future()
        self._cancel_events[task_id] = asyncio.Event()
        self._queue.put_nowait(task)
        log.debug(f"Task {task_id} queued (timeout={timeout}s)")

        # 确保 worker 在运行
        loop = asyncio.get_running_loop()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = loop.create_task(self._worker())

        return task_id

    async def wait_for(self, task_id: str,
                       timeout: Optional[float] = None) -> Any:
        """等待任务完成，返回执行结果"""
        future = self._futures.get(task_id)
        if future is None:
            raise KeyError(f"Unknown task: {task_id}")
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.cancel(task_id)
            raise

    async def _worker(self):
        """工作循环：从队列取任务，在并发限制内执行"""
        while True:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=2)
            except asyncio.TimeoutError:
                # 无待处理任务且无运行中任务 → 退出 worker
                # 需要检查是否还有 PAUSED 或 QUEUED 任务，避免丢失
                has_pending = any(
                    t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED)
                    for t in self._tasks.values()
                )
                if not self._running and not has_pending:
                    break
                continue

            # 已取消的任务跳过
            if task.status == TaskStatus.CANCELLED:
                self._resolve_future(task.id, None)
                self._cleanup_task(task.id)
                continue

            # 已暂停的任务重新入队
            if task.status == TaskStatus.PAUSED:
                await asyncio.sleep(0.5)
                self._queue.put_nowait(task)
                continue

            # 达到并发上限 → 重新入队等待
            if len(self._running) >= self.max_concurrent:
                await asyncio.sleep(0.2)
                self._queue.put_nowait(task)
                continue

            # 启动执行
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            run_task = asyncio.create_task(
                self._run_with_timeout(task)
            )
            self._running[task.id] = run_task
            log.info(f"Task {task.id} started (running={len(self._running)}/{self.max_concurrent})")

    def _cleanup_task(self, task_id: str):
        """清理任务相关资源"""
        self._cancel_events.pop(task_id, None)
        future = self._futures.pop(task_id, None)
        if future and not future.done():
            future.cancel()

    async def _run_with_timeout(self, task: DownloadTask):
        """执行单个任务（支持超时和取消）"""
        cancel_event = self._cancel_events.get(task.id)
        try:
            async def run():
                if self._executor is None:
                    raise RuntimeError("Scheduler executor not set. Call set_executor() first.")
                return await self._executor(task.handle, task.id)

            # 同时等待执行和取消信号
            async def wait_cancel():
                if cancel_event:
                    await cancel_event.wait()
                raise asyncio.CancelledError("Task cancelled via scheduler")

            done, pending = await asyncio.wait(
                [asyncio.create_task(run()),
                 asyncio.create_task(wait_cancel())],
                timeout=task.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 取消其他 pending 任务
            for p in pending:
                p.cancel()

            # 检查结果
            for d in done:
                exc = d.exception()
                if exc:
                    raise exc
                result = d.result()
                # 成功
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                self._running.pop(task.id, None)
                self._resolve_future(task.id, result)
                self._cleanup_task(task.id)
                log.info(f"Task {task.id} completed")
                return

            # timeout 分支
            self._running.pop(task.id, None)
            task.error = f"Timeout after {task.timeout}s"
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = TaskStatus.QUEUED
                task.error = None
                self._queue.put_nowait(task)
                log.info(f"Task {task.id} re-queued (attempt {task.retry_count}/{task.max_retries})")
            else:
                task.status = TaskStatus.FAILED
                task.completed_at = time.time()
                self._resolve_future_ex(task.id, TimeoutError(task.error))
                self._cleanup_task(task.id)

        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            task.completed_at = time.time()
            self._running.pop(task.id, None)
            self._resolve_future_ex(task.id, asyncio.CancelledError("Task cancelled"))
            self._cleanup_task(task.id)
            log.info(f"Task {task.id} cancelled")

        except Exception as e:
            self._running.pop(task.id, None)
            task.error = str(e)
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = TaskStatus.QUEUED
                task.error = None
                self._queue.put_nowait(task)
                log.info(f"Task {task.id} re-queued (attempt {task.retry_count}/{task.max_retries}): {e}")
            else:
                task.status = TaskStatus.FAILED
                task.completed_at = time.time()
                self._resolve_future_ex(task.id, e)
                self._cleanup_task(task.id)
                log.warning(f"Task {task.id} failed after {task.retry_count} retries: {e}")

    def _resolve_future(self, task_id: str, result):
        future = self._futures.get(task_id)
        if future and not future.done():
            future.set_result(result)

    def _resolve_future_ex(self, task_id: str, exception: Exception):
        future = self._futures.get(task_id)
        if future and not future.done():
            future.set_exception(exception)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        log.info(f"Cancelling task {task_id}")
        task.status = TaskStatus.CANCELLED
        # 触发取消事件，通知正在执行的 worker
        cancel_event = self._cancel_events.get(task_id)
        if cancel_event:
            cancel_event.set()
        return True

    def pause(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.PAUSED
            log.debug(f"Task {task_id} paused")
            return True
        return False

    def resume(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.PAUSED:
            task.status = TaskStatus.QUEUED
            self._queue.put_nowait(task)
            log.debug(f"Task {task_id} resumed")
            return True
        return False

    # backward-compat: 新架构下 complete/fail 由 _run_with_timeout 自动处理
    def complete(self, task_id: str):
        pass

    def fail(self, task_id: str, error: str):
        pass

    def active_count(self) -> int:
        return len(self._running)

    def queued_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.QUEUED)

    def completed_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.COMPLETED)

    def failed_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.FAILED)

    def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        return {
            'id': task.id,
            'status': task.status.value,
            'error': task.error,
            'retry_count': task.retry_count,
            'created_at': task.created_at,
            'started_at': task.started_at,
            'completed_at': task.completed_at,
        }
