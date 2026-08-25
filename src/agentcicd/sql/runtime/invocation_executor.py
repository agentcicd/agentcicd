from __future__ import annotations

from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Iterable


@dataclass(frozen=True)
class RuntimeInvocation:
    invoke: Callable[[Any], Any]
    acquire_pool: Callable[[], ContextManager[Any]] = field(default=lambda: nullcontext(None))
    acquire_limiter: Callable[[], ContextManager[Any]] = field(default=lambda: nullcontext(None))
    on_pool_acquired: Callable[[Any], None] | None = None
    on_error: Callable[[BaseException], Any] | None = None


class RuntimeInvocationPoolExecutor:
    """Runs row invocations with bounded fan-out.

    Each invocation owns its own acquire/invoke/release lifecycle. The executor
    only schedules up to max_concurrency invocations, so queued rows do not hold
    leases or rate-limit permits.
    """

    def __init__(self, *, max_concurrency: int) -> None:
        self.max_concurrency = max(1, int(max_concurrency or 1))

    def run(self, invocations: Iterable[RuntimeInvocation]) -> list[Any]:
        tasks = list(invocations)
        if not tasks:
            return []
        max_workers = min(len(tasks), self.max_concurrency)
        if max_workers <= 1:
            return [self._run_one(task) for task in tasks]
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agentcicd-runtime-invoke") as executor:
            return list(executor.map(self._run_one, tasks))

    @staticmethod
    def _run_one(task: RuntimeInvocation) -> Any:
        try:
            with task.acquire_pool() as pool_lease:
                if task.on_pool_acquired is not None:
                    task.on_pool_acquired(pool_lease)
                with task.acquire_limiter():
                    return task.invoke(pool_lease)
        except BaseException as exc:
            if task.on_error is None:
                raise
            return task.on_error(exc)
