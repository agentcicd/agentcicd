from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from agentcicd.sql.runtime.invocation_executor import RuntimeInvocation, RuntimeInvocationPoolExecutor


def test_runtime_invocation_pool_executor_schedules_lazily() -> None:
    lock = threading.Lock()
    active = 0
    max_active = 0
    started = 0

    def _task(index: int) -> int:
        nonlocal active, max_active, started
        with lock:
            active += 1
            started += 1
            max_active = max(max_active, active)
            assert started <= 2 or active <= 2
        time.sleep(0.05)
        with lock:
            active -= 1
        return index

    invocations = [RuntimeInvocation(invoke=lambda _lease, index=index: _task(index)) for index in range(5)]

    results = RuntimeInvocationPoolExecutor(max_concurrency=2).run(invocations)

    assert results == [0, 1, 2, 3, 4]
    assert max_active == 2


def test_runtime_invocation_pool_executor_owns_invocation_lifecycle() -> None:
    events: list[str] = []

    def _invocation(index: int) -> RuntimeInvocation:
        @contextmanager
        def _pool():
            events.append(f"pool-acquire-{index}")
            try:
                yield f"lease-{index}"
            finally:
                events.append(f"pool-release-{index}")

        @contextmanager
        def _limiter():
            events.append(f"limit-acquire-{index}")
            try:
                yield None
            finally:
                events.append(f"limit-release-{index}")

        return RuntimeInvocation(
            acquire_pool=_pool,
            acquire_limiter=_limiter,
            invoke=lambda lease: events.append(f"invoke-{lease}") or index,
        )

    results = RuntimeInvocationPoolExecutor(max_concurrency=1).run([_invocation(1), _invocation(2)])

    assert results == [1, 2]
    assert events == [
        "pool-acquire-1",
        "limit-acquire-1",
        "invoke-lease-1",
        "limit-release-1",
        "pool-release-1",
        "pool-acquire-2",
        "limit-acquire-2",
        "invoke-lease-2",
        "limit-release-2",
        "pool-release-2",
    ]
