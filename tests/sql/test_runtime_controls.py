from __future__ import annotations

from agentcicd.sql.runtime.udf_compat.function import AsyncRowFunction, RowFunction
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.runtime import controls
from agentcicd.sql.runtime.udf_compat import runtime_control
from agentcicd.sql.runtime.udf_compat.runtime_control import (
    DriverPoolLeaseManager,
    DriverLeaseManager,
    RateLimitUnavailable,
    runtime_pool_lease,
    start_driver_rate_limit_server,
    _node_disposition_for_status,
)


class DummyAsyncFunction(AsyncRowFunction):
    async def transform(self, value):
        return value


class DummyRowFunction(RowFunction):
    def transform(self, value):
        return value


def test_async_local_function_runtime_limit_returns_context_manager(monkeypatch):
    def _unexpected_runtime_limiter(*args, **kwargs):
        raise AssertionError("async row functions should use their internal async limiter")

    monkeypatch.setattr(controls, "runtime_limiter", _unexpected_runtime_limiter)

    with controls._runtime_limit_for_local_function(DummyAsyncFunction(), "default", None):
        pass


def test_sync_local_function_runtime_limit_uses_runtime_limiter(monkeypatch):
    calls = []

    class _Limiter:
        def acquire_blocking(self, *, permits):
            calls.append(permits)
            return self

        def __enter__(self):
            calls.append("enter")

        def __exit__(self, exc_type, exc, tb):
            calls.append("exit")

    monkeypatch.setattr(controls, "runtime_limiter", lambda default=None, *, key="default": _Limiter())

    with controls._runtime_limit_for_local_function(DummyRowFunction(), "default", 2):
        pass

    assert calls == [1, "enter", "exit"]


def test_pool_request_id_separates_run_and_task_attempt(monkeypatch):
    monkeypatch.setenv("AGENTCICD_RUN_ID", "run.1")
    monkeypatch.setenv("AGENTCICD_RUN_ATTEMPT", "2")
    monkeypatch.setenv("AGENTCICD_POOL_STAGE_ID", "stage.3")
    monkeypatch.setenv("AGENTCICD_POOL_PARTITION_ID", "partition.4")
    monkeypatch.setenv("AGENTCICD_POOL_TASK_ATTEMPT", "task.5")
    monkeypatch.setenv("AGENTCICD_POOL_ROW_ID", "row.6")
    monkeypatch.setenv("AGENTCICD_POOL_FUNCTION_CALL_ID", "call.7")
    definition = FunctionDefinitionIR(
        canonical_name="browser.check",
        kind="remote",
        surface_names=["browser.check"],
        runtime_alias="browser_check",
        parameters=[],
    )

    request_id = controls._pool_request_id(definition, {"task": "abc"})

    assert request_id.startswith("run.1:2:stage.3:partition.4:task.5:row.6:browser.check:call.7:")


def test_sandbox_pool_node_is_reused_after_manager_cleanup():
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


def test_driver_pool_lease_manager_validates_active_lease():
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


def test_sandbox_pool_runtime_return_uses_discard_disposition():
    assert _node_disposition_for_status("sandbox", "completed") == "discard"


def test_driver_pool_lease_manager_routes_by_grouped_fixture_ids():
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="session_pool",
        pool_kind="session",
        node_id="node.grouped",
        address="http://grouped",
        metadata={"fixture_ids": ["fixture.browser", "fixture.db"], "runtime_group_key": "group.a"},
    )

    lease = manager.acquire(
        pool_name="session_pool",
        pool_kind="session",
        fixture_id="fixture.db",
        request_id="request.db",
    )

    assert lease.address == "http://grouped"
    assert lease.fixture_id == "fixture.db"
    assert lease.lease_decision == "reuse_compatible"


def test_driver_pool_lease_manager_does_not_reuse_request_lease_for_different_fixture():
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


def test_driver_pool_lease_manager_reuses_service_worker_slots_above_worker_capacity():
    manager = DriverPoolLeaseManager()
    manager.register_node(
        pool_name="service_pool",
        pool_kind="service",
        node_id="node.service",
        address="http://service",
        capacity=9,
        metadata={"fixture_ids": ["fixture.agent"], "worker_capacity": 3},
    )

    leases = [
        manager.acquire(
            pool_name="service_pool",
            pool_kind="service",
            fixture_id="fixture.agent",
            request_id=f"request.{index}",
        )
        for index in range(9)
    ]

    assert [lease.worker_slot_id for lease in leases] == [
        "node.service.slot-1",
        "node.service.slot-2",
        "node.service.slot-3",
        "node.service.slot-1",
        "node.service.slot-2",
        "node.service.slot-3",
        "node.service.slot-1",
        "node.service.slot-2",
        "node.service.slot-3",
    ]


def test_driver_rate_limit_try_only_fails_immediately_when_busy():
    manager = DriverLeaseManager(default_max_in_flight=1)
    lease = manager.acquire(key="agent_ratelimit", wait_timeout_seconds=30)
    try:
        try:
            manager.acquire(key="agent_ratelimit", wait_timeout_seconds=30, try_only=True)
        except RateLimitUnavailable as exc:
            assert "agent_ratelimit" in str(exc)
        else:
            raise AssertionError("expected non-blocking rate-limit acquire to fail")
    finally:
        manager.release(lease.lease_id or "")


def test_driver_rate_limit_client_polls_try_only_until_available(monkeypatch):
    calls = []

    def _fake_post_json(url, payload, *, timeout_seconds):
        calls.append((url, dict(payload), timeout_seconds))
        if len(calls) < 3:
            raise runtime_control.RateLimitUnavailable("busy")
        return {
            "limiter_name": "driver",
            "key": payload["key"],
            "lease_id": "lease.ready",
            "permits": payload["permits"],
        }

    monkeypatch.setenv("AGENTCICD_RATE_LIMITER_ACQUIRE_POLL_SECONDS", "0.01")
    monkeypatch.setattr(runtime_control, "_post_json", _fake_post_json)
    monkeypatch.setattr(runtime_control, "sleep", lambda seconds: None)

    client = runtime_control._DriverRuntimeLimiterClient(
        "http://driver",
        key="agent_ratelimit",
        default_max_in_flight=9,
        lease_ttl_seconds=90000,
        wait_timeout_seconds=90000,
        http_timeout_seconds=90000,
    )

    lease = client._acquire_blocking(permits=1)

    assert lease.lease_id == "lease.ready"
    assert len(calls) == 3
    assert all(call[1]["try_only"] is True for call in calls)
    assert all(call[1]["wait_timeout_seconds"] == 0 for call in calls)
    assert all(call[2] == 10.0 for call in calls)


def test_runtime_pool_lease_prefers_pool_fixture_id_over_explicit_fallback(monkeypatch):
    server = start_driver_rate_limit_server(
        host="127.0.0.1",
        port=0,
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


def test_driver_pool_runtime_config_validation_requires_fixture_ids():
    try:
        DriverPoolLeaseManager.validate_pool_nodes(
            [{"pool_name": "service_pool", "pool_kind": "service", "node_id": "node.bad", "metadata": {}}]
        )
    except ValueError as exc:
        assert "metadata.fixture_ids" in str(exc)
    else:
        raise AssertionError("expected grouped pool node validation to fail")
