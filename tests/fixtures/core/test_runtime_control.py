from __future__ import annotations

import asyncio
import threading
import time

import pyarrow as pa
import pytest

from agentcicd.fixtures.core.function import AsyncRowFunction
from agentcicd.fixtures.core.runtime_control import (
    DriverLeaseManager,
    DriverPoolLeaseManager,
    _node_disposition_for_status,
    runtime_limiter,
    runtime_pool_lease,
    start_driver_rate_limit_server,
)


pytestmark = pytest.mark.essential


class _TrackedAsyncFunction(AsyncRowFunction):
    def __init__(self) -> None:
        self.running = 0
        self.max_running = 0

    async def transform(self, value):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0.01)
            return value
        finally:
            self.running -= 1


def test_async_row_function_observes_runtime_max_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCICD_FIXTURE_MAX_IN_FLIGHT", "2")
    fn = _TrackedAsyncFunction()

    result = list(fn.execute(pa.array([1, 2, 3, 4, 5])))[0].to_pylist()

    assert result == [1, 2, 3, 4, 5]
    assert fn.max_running == 2


def test_driver_lease_manager_enforces_keyed_global_in_flight_limit() -> None:
    manager = DriverLeaseManager(default_max_in_flight=1)
    first = manager.acquire(key="fixture.alpha", permits=1)
    blocked = True

    def _acquire_second() -> None:
        nonlocal blocked
        second = manager.acquire(key="fixture.alpha", permits=1)
        blocked = False
        manager.release(second.lease_id or "")

    thread = threading.Thread(target=_acquire_second)
    thread.start()
    time.sleep(0.05)

    assert blocked is True

    manager.release(first.lease_id or "")
    thread.join(timeout=1)
    assert blocked is False


def test_runtime_limiter_uses_driver_http_leases_when_base_url_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = start_driver_rate_limit_server(host="127.0.0.1", port=0, default_max_in_flight=1)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AGENTCICD_RATE_LIMITER_BASE_URL", base_url)
    monkeypatch.setenv("AGENTCICD_FIXTURE_MAX_IN_FLIGHT", "1")
    first_client = runtime_limiter(key="default")
    second_client = runtime_limiter(key="default")
    acquired_second = False

    try:
        with first_client.acquire_blocking():
            def _acquire_second() -> None:
                nonlocal acquired_second
                with second_client.acquire_blocking():
                    acquired_second = True

            thread = threading.Thread(target=_acquire_second)
            thread.start()
            time.sleep(0.05)
            assert acquired_second is False
        thread.join(timeout=1)
        assert acquired_second is True
    finally:
        server.shutdown()
        server.server_close()


def test_driver_pool_lease_manager_acquires_exclusive_session_node() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="browser_pool",
        pool_kind="session",
        node_id="node.1",
        address="http://fixture-1",
        capacity=1,
    )
    first = manager.acquire(pool_name="browser_pool", pool_kind="session", request_id="request.1")
    blocked = True

    def _acquire_second() -> None:
        nonlocal blocked
        second = manager.acquire(pool_name="browser_pool", pool_kind="session", request_id="request.2")
        blocked = False
        manager.return_lease(second.lease_id)

    thread = threading.Thread(target=_acquire_second)
    thread.start()
    time.sleep(0.05)

    assert first.address == "http://fixture-1"
    assert blocked is True

    manager.return_lease(first.lease_id)
    thread.join(timeout=1)
    assert blocked is False


def test_driver_pool_lease_manager_routes_by_fixture_id() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="session_pool",
        pool_kind="session",
        node_id="node.browser",
        address="http://browser",
        metadata={"fixture_ids": ["fixture.browser"]},
    )
    manager.register_node(
        pool_name="session_pool",
        pool_kind="session",
        node_id="node.db",
        address="http://db",
        metadata={"fixture_ids": ["fixture.db"]},
    )

    lease = manager.acquire(
        pool_name="session_pool",
        pool_kind="session",
        fixture_id="fixture.db",
        request_id="request.db",
    )

    assert lease.address == "http://db"
    assert lease.fixture_id == "fixture.db"
    assert lease.lease_decision == "reuse_compatible"


def test_driver_pool_lease_manager_does_not_reuse_request_lease_for_different_fixture() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="service_pool",
        pool_kind="service",
        node_id="node.generate",
        address="http://generate",
        capacity=10,
        metadata={"fixture_ids": ["fixture.generate"], "worker_capacity": 1},
    )
    manager.register_node(
        pool_name="service_pool",
        pool_kind="service",
        node_id="node.episode",
        address="http://episode",
        capacity=10,
        metadata={"fixture_ids": ["fixture.episode"], "worker_capacity": 1},
    )

    first = manager.acquire(
        pool_name="service_pool",
        pool_kind="service",
        fixture_id="fixture.episode",
        request_id="request.shared",
    )
    second = manager.acquire(
        pool_name="service_pool",
        pool_kind="service",
        fixture_id="fixture.generate",
        request_id="request.shared",
    )

    assert first.fixture_id == "fixture.episode"
    assert second.fixture_id == "fixture.generate"
    assert second.address == "http://generate"


def test_driver_pool_lease_manager_allows_shared_service_capacity() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="service_pool",
        pool_kind="service",
        node_id="node.shared",
        address="http://shared",
        capacity=9,
        metadata={"worker_capacity": 3},
    )

    leases = [
        manager.acquire(pool_name="service_pool", pool_kind="service", request_id=f"request.{index}")
        for index in range(9)
    ]

    assert {lease.node_id for lease in leases} == {"node.shared"}
    assert [lease.worker_slot_id for lease in leases] == [
        "node.shared.slot-1",
        "node.shared.slot-2",
        "node.shared.slot-3",
        "node.shared.slot-1",
        "node.shared.slot-2",
        "node.shared.slot-3",
        "node.shared.slot-1",
        "node.shared.slot-2",
        "node.shared.slot-3",
    ]


def test_driver_pool_lease_manager_reuses_sandbox_node_after_manager_cleanup() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="sandbox_pool",
        pool_kind="sandbox",
        node_id="node.sandbox",
        address="http://sandbox",
        metadata={"replacement_delay_seconds": 0},
    )
    lease = manager.acquire(pool_name="sandbox_pool", pool_kind="sandbox", request_id="request.1")

    assert manager.return_lease(lease.lease_id, terminal_status="completed", node_disposition="discard") is True

    snapshot = manager.snapshot()
    assert snapshot["nodes"]["node.sandbox"]["status"] == "available"
    replacement = manager.acquire(pool_name="sandbox_pool", pool_kind="sandbox", request_id="request.2")
    assert replacement.node_id == "node.sandbox"
    assert replacement.worker_slot_id == "node.sandbox.slot-1"


def test_driver_pool_lease_manager_validates_active_lease() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="sandbox_pool",
        pool_kind="sandbox",
        node_id="node.sandbox",
        address="http://sandbox",
        metadata={"fixture_ids": ["fixture.browser"], "generation": 3},
    )
    lease = manager.acquire(pool_name="sandbox_pool", pool_kind="sandbox", fixture_id="fixture.browser")

    valid, reason = manager.validate_lease(
        lease_id=lease.lease_id,
        pool_name="sandbox_pool",
        pool_kind="sandbox",
        fixture_id="fixture.browser",
        manager_id="node.sandbox",
        worker_slot_id=lease.worker_slot_id or "",
        generation=3,
    )
    invalid, invalid_reason = manager.validate_lease(
        lease_id=lease.lease_id,
        pool_name="sandbox_pool",
        pool_kind="sandbox",
        fixture_id="fixture.browser",
        manager_id="node.sandbox",
        worker_slot_id="node.sandbox.slot-2",
        generation=3,
    )

    assert valid is True
    assert reason == ""
    assert invalid is False
    assert invalid_reason == "lease worker_slot_id mismatch"


def test_sandbox_pool_kind_returns_discard_disposition() -> None:
    assert _node_disposition_for_status("sandbox", "completed") == "discard"


def test_driver_pool_lease_manager_keeps_timed_out_session_down_until_registration() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="session_pool",
        pool_kind="session",
        node_id="node.session",
        address="http://session",
    )
    lease = manager.acquire(pool_name="session_pool", pool_kind="session", request_id="request.1")

    assert manager.return_lease(lease.lease_id, terminal_status="timed_out", node_disposition="unhealthy") is True

    snapshot = manager.snapshot()
    assert snapshot["nodes"]["node.session"]["status"] == "unhealthy"

    with pytest.raises(TimeoutError, match="Timed out acquiring pool lease"):
        manager.acquire(
            pool_name="session_pool",
            pool_kind="session",
            request_id="request.2",
            wait_timeout_seconds=0.1,
        )

    manager.register_node(
        pool_name="session_pool",
        pool_kind="session",
        node_id="node.session",
        address="http://session",
    )
    replacement_lease = manager.acquire(pool_name="session_pool", pool_kind="session", request_id="request.3")
    assert replacement_lease.node_id == "node.session"


def test_driver_pool_lease_manager_try_only_acquire_returns_immediately_when_full() -> None:
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="session_pool",
        pool_kind="session",
        node_id="node.session",
        address="http://session",
    )
    manager.acquire(pool_name="session_pool", pool_kind="session", request_id="request.1")

    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="Timed out acquiring pool lease"):
        manager.acquire(
            pool_name="session_pool",
            pool_kind="session",
            request_id="request.2",
            wait_timeout_seconds=30,
            try_only=True,
        )

    assert time.monotonic() - started_at < 0.5


def test_runtime_pool_lease_uses_driver_http_service(monkeypatch: pytest.MonkeyPatch) -> None:
    server = start_driver_rate_limit_server(
        host="127.0.0.1",
        port=0,
        default_max_in_flight=1,
        pool_nodes=[
            {
                "pool_name": "browser_pool",
                "pool_kind": "session",
                "node_id": "node.1",
                "address": "http://fixture-1",
                "metadata": {"fixture_ids": ["fixture.browser"]},
            }
        ],
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AGENTCICD_RATE_LIMITER_BASE_URL", base_url)
    try:
        with runtime_pool_lease(
            {
                "key": "browser_pool",
                "config": {"kind": "session", "max_instances": 1},
            },
            fixture_id="fixture.browser",
        ) as lease:
            assert lease is not None
            assert lease.pool_name == "browser_pool"
            assert lease.pool_kind == "session"
            assert lease.address == "http://fixture-1"
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_pool_lease_prefers_pool_fixture_id_over_explicit_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    server = start_driver_rate_limit_server(
        host="127.0.0.1",
        port=0,
        default_max_in_flight=1,
        pool_nodes=[
            {
                "pool_name": "service_pool",
                "pool_kind": "service",
                "node_id": "node.generate",
                "address": "http://generate",
                "capacity": 10,
                "metadata": {"fixture_ids": ["fixture.generate"], "worker_capacity": 1},
            },
            {
                "pool_name": "service_pool",
                "pool_kind": "service",
                "node_id": "node.episode",
                "address": "http://episode",
                "capacity": 10,
                "metadata": {"fixture_ids": ["fixture.episode"], "worker_capacity": 1},
            },
        ],
    )
    monkeypatch.setenv("AGENTCICD_RATE_LIMITER_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        with runtime_pool_lease(
            {"key": "service_pool", "config": {"kind": "service"}, "fixture_id": "fixture.generate"},
            fixture_id="fixture.episode",
        ) as lease:
            assert lease is not None
            assert lease.fixture_id == "fixture.generate"
            assert lease.address == "http://generate"
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_pool_lease_polls_until_pool_slot_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    server = start_driver_rate_limit_server(
        host="127.0.0.1",
        port=0,
        default_max_in_flight=1,
        pool_nodes=[
            {
                "pool_name": "browser_pool",
                "pool_kind": "session",
                "node_id": "node.1",
                "address": "http://fixture-1",
                "metadata": {"fixture_ids": ["fixture.browser"]},
            }
        ],
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AGENTCICD_RATE_LIMITER_BASE_URL", base_url)
    monkeypatch.setenv("AGENTCICD_POOL_ACQUIRE_POLL_SECONDS", "0.05")
    acquired: list[str] = []
    error: list[BaseException] = []

    def _wait_for_lease() -> None:
        try:
            with runtime_pool_lease(
                {
                    "key": "browser_pool",
                    "config": {"kind": "session", "max_instances": 1, "timeout_seconds": 2},
                },
                request_id="waiter",
                fixture_id="fixture.browser",
            ) as lease:
                acquired.append(lease.lease_id if lease is not None else "")
        except BaseException as exc:
            error.append(exc)

    try:
        with runtime_pool_lease(
            {
                "key": "browser_pool",
                "config": {"kind": "session", "max_instances": 1},
            },
            request_id="holder",
            fixture_id="fixture.browser",
        ):
            thread = threading.Thread(target=_wait_for_lease)
            thread.start()
            time.sleep(0.15)
            assert thread.is_alive()
        thread.join(timeout=2)

        assert not error
        assert acquired
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_pool_lease_does_not_synthesize_unregistered_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    server = start_driver_rate_limit_server(host="127.0.0.1", port=0, default_max_in_flight=1)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AGENTCICD_RATE_LIMITER_BASE_URL", base_url)
    try:
        with pytest.raises(TimeoutError, match="Timed out acquiring pool lease"):
            with runtime_pool_lease(
                {
                    "key": "browser_pool",
                    "config": {"kind": "session", "max_instances": 1},
                    "wait_timeout_seconds": 0.1,
                },
                fallback_address="http://fixture-1",
            ):
                pass
    finally:
        server.shutdown()
        server.server_close()
