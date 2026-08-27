from __future__ import annotations

import sys
import time
from dataclasses import replace
from typing import Any
from unittest.mock import patch

from agentcicd.sandbox.manager import (
    DockerWorkerConfig,
    DockerWorkerLifecycle,
    GVisorHelperWorkerLifecycle,
    ManagerConfig,
    SandboxManager,
    SubprocessFunctionWorkerLifecycle,
    WorkerRecord,
    manager_config_from_env,
)
from agentcicd.sandbox import manager as sandbox_manager


class RecordingLifecycle:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.stopped: list[str] = []
        self.cleared: list[str] = []

    def create(self, slot_id: str, *, session_key: str = "", fixture_id: str = "", image: str = "") -> WorkerRecord:
        worker = WorkerRecord(
            worker_id=f"worker-{len(self.created) + 1}",
            slot_id=slot_id,
            session_key=session_key,
            fixture_id=fixture_id,
            image=image,
        )
        self.created.append(worker.worker_id)
        return worker

    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        worker.invocation_count += 1
        return {"result": {"worker_id": worker.worker_id, "value": arguments.get("value")}}

    def stop(self, worker: WorkerRecord, *, reason: str) -> None:
        self.stopped.append(worker.worker_id)
        worker.healthy = False

    def clear(self, worker: WorkerRecord, *, reason: str) -> None:
        self.cleared.append(worker.worker_id)
        worker.session_key = ""
        worker.last_used_at = time.monotonic()

    def status(self, worker: WorkerRecord) -> dict[str, Any]:
        return {"healthy": worker.healthy}


class SlowLifecycle(RecordingLifecycle):
    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        time.sleep(0.2)
        return super().invoke(worker, function_name, arguments, trace=trace, secrets_payload=secrets_payload)


class TracedSlowLifecycle(RecordingLifecycle):
    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        from agentcicd.fixtures.core.tracing import runtime_trace_span
        from agentcicd.sandbox import function_runner

        with function_runner._remote_runtime_trace_context(trace):
            with runtime_trace_span("fixture.long_step", {"phase": "agent_run"}):
                time.sleep(0.2)
        return super().invoke(worker, function_name, arguments, trace=trace, secrets_payload=secrets_payload)


def _config(pool_kind: str) -> ManagerConfig:
    return ManagerConfig(
        fixture_id="fixture.browser",
        function_name="check",
        pool_name="fixture_pool",
        pool_kind=pool_kind,
        manager_id="manager.1",
        generation=7,
        address="http://manager.1:8080",
        max_workers=2,
        require_lease=True,
        debug=True,
    )


def _lease(*, slot_id: str = "manager.1.slot-1", request_id: str = "request.1") -> dict[str, Any]:
    return {
        "lease_id": "lease.1",
        "pool_name": "fixture_pool",
        "pool_kind": "service",
        "manager_id": "manager.1",
        "worker_slot_id": slot_id,
        "fixture_id": "fixture.browser",
        "generation": 7,
        "request_id": request_id,
    }


def test_service_pool_reuses_worker_for_same_slot() -> None:
    lifecycle = RecordingLifecycle()
    manager = SandboxManager(_config("service"), lifecycle)
    lease = _lease()

    first_status, first = manager.invoke("check", {"args": {"value": "a"}, "lease": lease})
    second_status, second = manager.invoke("check", {"args": {"value": "b"}, "lease": lease})

    assert first_status == second_status == 200
    assert first["result"]["worker_id"] == second["result"]["worker_id"]
    assert lifecycle.created == ["worker-1"]
    assert lifecycle.stopped == []


def test_service_pool_without_lease_spreads_calls_across_local_slots() -> None:
    lifecycle = RecordingLifecycle()
    config = replace(_config("service"), require_lease=False, max_workers=2)
    manager = SandboxManager(config, lifecycle)

    first_status, first = manager.invoke("check", {"args": {"value": "a"}})
    second_status, second = manager.invoke("check", {"args": {"value": "b"}})
    third_status, third = manager.invoke("check", {"args": {"value": "c"}})

    assert first_status == second_status == third_status == 200
    assert first["result"]["worker_id"] == "worker-1"
    assert second["result"]["worker_id"] == "worker-2"
    assert third["result"]["worker_id"] == "worker-1"
    assert lifecycle.created == ["worker-1", "worker-2"]


def test_subprocess_function_worker_reuses_process_for_service_invocations(tmp_path, monkeypatch) -> None:
    source = tmp_path / "counter_fixture.py"
    source.write_text(
        "\n".join(
            [
                "from agentcicd import Int, function",
                "_counter = 0",
                "@function",
                "def counter(value: Int) -> Int:",
                "    global _counter",
                "    _counter += 1",
                "    return _counter",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATHS", f'["{source}"]')
    lifecycle = SubprocessFunctionWorkerLifecycle()
    worker = lifecycle.create("manager.1.slot-1")

    try:
        first = lifecycle.invoke(worker, "counter", {"value": 1})
        second = lifecycle.invoke(worker, "counter", {"value": 1})
    finally:
        lifecycle.stop(worker, reason="test_complete")

    assert first["result"] == 1
    assert second["result"] == 2
    assert worker.invocation_count == 2


def test_worker_exit_fallback_includes_returncode_and_output_tail() -> None:
    message = sandbox_manager._worker_exit_fallback_error(
        returncode=137,
        stdout="partial stdout",
        stderr="killed by oom",
    )

    assert "returncode=137" in message
    assert "stderr=killed by oom" in message
    assert "stdout=partial stdout" in message


def test_service_pool_replaces_worker_when_requested_fixture_image_changes() -> None:
    lifecycle = RecordingLifecycle()
    config = replace(
        _config("service"),
        fixture_ids=("fixture.browser", "fixture.db"),
        function_names=("check",),
        fixture_worker_images={
            "fixture.browser": "image/browser:latest",
            "fixture.db": "image/db:latest",
        },
    )
    manager = SandboxManager(config, lifecycle)

    browser_status, browser = manager.invoke("check", {"args": {"value": "a"}, "lease": _lease()})
    db_status, db = manager.invoke(
        "check",
        {
            "args": {"value": "b"},
            "lease": {**_lease(request_id="request.2"), "fixture_id": "fixture.db"},
        },
    )

    assert browser_status == db_status == 200
    assert browser["result"]["worker_id"] == "worker-1"
    assert db["result"]["worker_id"] == "worker-2"
    assert browser["debug"]["lease_decision"] == "start_compatible"
    assert db["debug"]["lease_decision"] == "replace_idle_incompatible"
    assert db["debug"]["worker_image"] == "image/db:latest"
    assert lifecycle.created == ["worker-1", "worker-2"]
    assert lifecycle.stopped == ["worker-1"]


def test_capacity_registration_includes_runtime_metadata(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return {}

    monkeypatch.setenv("AGENTCICD_FIXTURE_RUNTIME_PROVIDER", "sandbox_manager_gvisor")
    monkeypatch.setenv("AGENTCICD_SANDBOX_WORKER_SUBSTRATE", "gvisor")
    monkeypatch.setenv("AGENTCICD_FIXTURE_MANIFEST_SCHEMA_VERSION", "agentcicd.fixture_manifest.v2")
    monkeypatch.setenv("AGENTCICD_SANDBOX_RUNTIME_IMAGE_VERSION", "sandbox-runtime:test")
    monkeypatch.setenv("AGENTCICD_FIXTURES_VERSION", "fixtures:test")
    with patch.object(sandbox_manager, "_post_json", fake_post_json):
        SandboxManager(_config("service"), RecordingLifecycle()).register_capacity(
            driver_base_url="http://driver.local",
        )

    metadata = captured["payload"]["metadata"]
    assert captured["url"] == "http://driver.local/pools/nodes/register"
    assert metadata["runtime_provider"] == "sandbox_manager_gvisor"
    assert metadata["worker_substrate"] == "gvisor"
    assert metadata["runtime_protocol_version"] == "agentcicd.sandbox_runtime.v1"
    assert metadata["fixture_manifest_schema_version"] == "agentcicd.fixture_manifest.v2"
    assert metadata["sandbox_runtime_image_version"] == "sandbox-runtime:test"
    assert metadata["agentcicd_fixtures_version"] == "fixtures:test"


def test_session_pool_clears_and_reuses_worker_by_slot() -> None:
    lifecycle = RecordingLifecycle()
    manager = SandboxManager(_config("session"), lifecycle)
    lease = {**_lease(), "pool_kind": "session"}

    first_status, first = manager.invoke("check", {"args": {"session_key": "s1"}, "lease": lease})
    second_status, second = manager.invoke("check", {"args": {"session_key": "s1"}, "lease": lease})
    third_status, third = manager.invoke("check", {"args": {"session_key": "s2"}, "lease": lease})

    assert first_status == second_status == third_status == 200
    assert first["result"]["worker_id"] == second["result"]["worker_id"]
    assert third["result"]["worker_id"] == first["result"]["worker_id"]
    assert lifecycle.created == ["worker-1"]
    assert lifecycle.cleared == ["worker-1", "worker-1", "worker-1"]


def test_session_pool_expires_idle_workers() -> None:
    lifecycle = RecordingLifecycle()
    config = replace(_config("session"), session_idle_ttl_seconds=0.01)
    manager = SandboxManager(config, lifecycle)
    lease = {**_lease(), "pool_kind": "session"}

    first_status, first = manager.invoke("check", {"args": {"session_key": "s1"}, "lease": lease})
    time.sleep(0.02)
    second_status, second = manager.invoke("check", {"args": {"session_key": "s1"}, "lease": lease})

    assert first_status == second_status == 200
    assert first["result"]["worker_id"] == "worker-1"
    assert second["result"]["worker_id"] == "worker-2"
    assert lifecycle.stopped == ["worker-1"]


def test_session_pool_recreates_worker_when_clear_fails() -> None:
    class FailingClearLifecycle(RecordingLifecycle):
        def clear(self, worker: WorkerRecord, *, reason: str) -> None:
            self.cleared.append(worker.worker_id)
            raise RuntimeError("clear failed")

    lifecycle = FailingClearLifecycle()
    manager = SandboxManager(_config("session"), lifecycle)
    lease = {**_lease(), "pool_kind": "session"}

    first_status, first = manager.invoke("check", {"args": {"session_key": "s1"}, "lease": lease})
    second_status, second = manager.invoke("check", {"args": {"session_key": "s2"}, "lease": lease})

    assert first_status == second_status == 200
    assert first["result"]["worker_id"] == "worker-1"
    assert second["result"]["worker_id"] == "worker-2"
    assert lifecycle.stopped == ["worker-1", "worker-2"]
    assert manager.status()["cleanup_failures"] == 2


def test_sandbox_pool_discards_worker_after_each_invocation() -> None:
    lifecycle = RecordingLifecycle()
    manager = SandboxManager(_config("sandbox"), lifecycle)
    lease = {**_lease(), "pool_kind": "sandbox"}

    first_status, first = manager.invoke("check", {"args": {"value": "a"}, "lease": lease})
    second_status, second = manager.invoke("check", {"args": {"value": "b"}, "lease": lease})

    assert first_status == second_status == 200
    assert first["result"]["worker_id"] != second["result"]["worker_id"]
    assert lifecycle.created == ["worker-1", "worker-2"]
    assert lifecycle.stopped == ["worker-1", "worker-2"]


def test_sandbox_pool_uses_and_replaces_warm_worker() -> None:
    lifecycle = RecordingLifecycle()
    config = replace(_config("sandbox"), min_warm=1)
    manager = SandboxManager(config, lifecycle)
    lease = {**_lease(), "pool_kind": "sandbox"}

    status, payload = manager.invoke("check", {"args": {"value": "warm"}, "lease": lease})

    assert status == 200
    assert payload["result"]["worker_id"] == "worker-1"
    assert lifecycle.created == ["worker-1", "worker-2"]
    assert lifecycle.stopped == ["worker-1"]
    assert manager.status()["warm_workers"] == 1


def test_sandbox_pool_replaces_warm_worker_for_requested_fixture_image() -> None:
    lifecycle = RecordingLifecycle()
    config = replace(
        _config("sandbox"),
        min_warm=1,
        fixture_ids=("fixture.browser", "fixture.db"),
        fixture_worker_images={
            "fixture.browser": "image/browser:latest",
            "fixture.db": "image/db:latest",
        },
    )
    manager = SandboxManager(config, lifecycle)
    lease = {**_lease(), "pool_kind": "sandbox", "fixture_id": "fixture.db"}

    status, payload = manager.invoke("check", {"args": {"value": "db"}, "lease": lease})

    assert status == 200
    assert payload["result"]["worker_id"] == "worker-2"
    assert payload["debug"]["lease_decision"] == "replace_idle_incompatible"
    assert payload["debug"]["worker_image"] == "image/db:latest"
    assert lifecycle.created == ["worker-1", "worker-2", "worker-3"]
    assert lifecycle.stopped == ["worker-1", "worker-2"]
    assert manager.status()["warm_workers"] == 1


def test_manager_rejects_stale_generation_lease() -> None:
    manager = SandboxManager(_config("sandbox"), RecordingLifecycle())
    lease = {**_lease(), "pool_kind": "sandbox", "generation": 6}

    status, payload = manager.invoke("check", {"args": {}, "lease": lease})

    assert status == 409
    assert payload["error"] == "invalid_lease"
    assert "generation" in payload["detail"]


def test_manager_rejects_slot_outside_manager_capacity() -> None:
    manager = SandboxManager(_config("sandbox"), RecordingLifecycle())
    lease = {**_lease(), "pool_kind": "sandbox", "worker_slot_id": "manager.1.slot-3"}

    status, payload = manager.invoke("check", {"args": {}, "lease": lease})

    assert status == 409
    assert payload["error"] == "invalid_lease"
    assert "worker slot" in payload["detail"]


def test_manager_times_out_slow_invocation() -> None:
    config = replace(_config("sandbox"), call_timeout_seconds=0.01)
    manager = SandboxManager(config, SlowLifecycle())
    lease = {**_lease(), "pool_kind": "sandbox"}

    status, payload = manager.invoke("check", {"args": {}, "lease": lease})

    assert status == 408
    assert payload["error"] == "invoke_timeout"
    assert manager.status()["metrics"]["timeouts"] == 1


def test_manager_timeout_response_includes_trace_summary_and_writes_trace(tmp_path, monkeypatch) -> None:
    written: dict[str, object] = {}

    class Store:
        def put_json(self, uri: str, payload: object) -> None:
            written[uri] = payload

        def put_text(self, uri: str, payload: str, content_type: str = "text/plain") -> None:
            written[uri] = payload

    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
    source = tmp_path / "function.py"
    source.write_text(
        """
import asyncio
from agentcicd import function
from agentcicd.fixtures.core.tracing import runtime_trace_span


@function
async def check() -> dict:
    with runtime_trace_span("fixture.long_step", {"phase": "agent_run"}):
        await asyncio.sleep(8.0)
    return {"ok": True}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source))
    config = replace(_config("sandbox"), call_timeout_seconds=3.5)
    manager = SandboxManager(config)
    lease = {**_lease(), "pool_kind": "sandbox"}

    with patch("agentcicd.sandbox.manager.object_store_from_env", lambda: Store()):
        status, payload = manager.invoke(
            "check",
            {
                "args": {},
                "lease": lease,
                "trace": {
                    "trace_id": "trace-timeout",
                    "parent_span_id": "root-span",
                    "parent_call_id": "rtcall_root",
                },
            },
        )

    assert status == 408
    summary = payload["trace_summary"]
    assert summary["trace_id"] == "trace-timeout"
    assert summary["trace_spans_path"] == "debug/fixture_traces/trace-timeout/spans.jsonl"
    spans_uri = "agentcicd-object://org.test/runs/run.test/attempt_1/debug/fixture_traces/trace-timeout/spans.jsonl"
    assert spans_uri in written
    assert '"name":"agentcicd.fixture.call"' in str(written[spans_uri])
    assert '"status":"error"' in str(written[spans_uri])


def test_gvisor_helper_lifecycle_uses_json_helper_contract(tmp_path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "\n".join(
            [
                "import json, sys",
                "payload = json.loads(sys.stdin.read() or '{}')",
                "action = payload.get('action')",
                "if action == 'create':",
                "    print(json.dumps({'worker_id': 'gv-' + payload['slot_id']}))",
                "elif action == 'clear':",
                "    print(json.dumps({'cleared': payload['worker_id']}))",
                "elif action == 'invoke':",
                "    print(json.dumps({'result': {'worker_id': payload['worker_id'], 'value': payload['args']['value']}}))",
                "elif action == 'stop':",
                "    print(json.dumps({'stopped': payload['worker_id']}))",
                "else:",
                "    print(json.dumps({'error': 'bad action'}))",
            ]
        ),
        encoding="utf-8",
    )
    lifecycle = GVisorHelperWorkerLifecycle([sys.executable, str(helper)])

    worker = lifecycle.create("manager.1.slot-1")
    response = lifecycle.invoke(worker, "check", {"value": "ok"})
    lifecycle.clear(worker, reason="test_clear")
    lifecycle.stop(worker, reason="done")

    assert worker.worker_id == "gv-manager.1.slot-1"
    assert response == {"result": {"worker_id": "gv-manager.1.slot-1", "value": "ok"}}
    assert worker.healthy is False


def test_docker_lifecycle_copies_function_source_before_start(tmp_path) -> None:
    source = tmp_path / "function.py"
    source.write_text("def check():\n    return 'ok'\n", encoding="utf-8")
    lifecycle = DockerWorkerLifecycle(
        DockerWorkerConfig(
            image="registry.local/agentcicd-function-runner:test",
            source_path=str(source),
            worker_source_path="/app/function.py",
            create_timeout_seconds=900,
        )
    )
    calls: list[tuple[list[str], float, bool]] = []

    def fake_run(command: list[str], *, timeout_seconds: float, check: bool = True):
        calls.append((command, timeout_seconds, check))

    with patch.object(lifecycle, "_run_docker", fake_run):
        worker = lifecycle.create("manager.1.slot-1")

    assert worker.worker_id.startswith("agentcicd-worker-manager-1-slot-1-")
    assert calls[0][0][:2] == ["docker", "create"]
    assert calls[0][1] == 900
    assert calls[1][0] == ["docker", "start", worker.worker_id]
    assert calls[2][0] == ["docker", "exec", worker.worker_id, "mkdir", "-p", "/app"]
    assert calls[3][0] == [
        "docker",
        "cp",
        "-L",
        str(source),
        f"{worker.worker_id}:/app/function.py",
    ]


def test_docker_lifecycle_copies_grouped_function_sources_before_start(tmp_path) -> None:
    first = tmp_path / "function_0.py"
    second = tmp_path / "function_1.py"
    first.write_text("def first():\n    return 'ok'\n", encoding="utf-8")
    second.write_text("def second():\n    return 'ok'\n", encoding="utf-8")
    lifecycle = DockerWorkerLifecycle(
        DockerWorkerConfig(
            image="registry.local/agentcicd-function-runner:test",
            source_path=str(first),
            worker_source_path="/app/functions/function_0.py",
            source_paths=(str(first), str(second)),
            worker_source_paths=("/app/functions/function_0.py", "/app/functions/function_1.py"),
        )
    )
    calls: list[tuple[list[str], float, bool]] = []

    def fake_run(command: list[str], *, timeout_seconds: float, check: bool = True):
        calls.append((command, timeout_seconds, check))

    with patch.object(lifecycle, "_run_docker", fake_run):
        worker = lifecycle.create("manager.1.slot-1")

    assert calls[1][0] == ["docker", "start", worker.worker_id]
    assert calls[2][0] == ["docker", "exec", worker.worker_id, "mkdir", "-p", "/app/functions"]
    assert calls[3][0] == ["docker", "exec", worker.worker_id, "mkdir", "-p", "/app/functions"]
    assert calls[4][0] == ["docker", "cp", "-L", str(first), f"{worker.worker_id}:/app/functions/function_0.py"]
    assert calls[5][0] == ["docker", "cp", "-L", str(second), f"{worker.worker_id}:/app/functions/function_1.py"]


def test_gvisor_helper_lifecycle_collects_jsonl_trace_summary(tmp_path, monkeypatch) -> None:
    written: dict[str, object] = {}

    class Store:
        def put_json(self, uri: str, payload: object) -> None:
            written[uri] = payload

        def put_text(self, uri: str, payload: str, content_type: str = "text/plain") -> None:
            written[uri] = payload

    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", "5")
    helper = tmp_path / "helper_jsonl.py"
    helper.write_text(
        "\n".join(
            [
                "import json, sys",
                "payload = json.loads(sys.stdin.read() or '{}')",
                "if payload.get('action') == 'create':",
                "    print(json.dumps({'worker_id': 'gv-' + payload['slot_id']}))",
                "elif payload.get('action') == 'invoke':",
                "    trace = payload.get('trace') or {}",
                "    print(json.dumps({'type': 'trace_record', 'record': {'record_type': 'span', 'trace_id': trace.get('trace_id'), 'span_id': 'child-span', 'parent_span_id': trace.get('parent_span_id'), 'name': 'fixture.child', 'kind': 'span', 'status': 'ok', 'started_at': '2026-01-01T00:00:00Z', 'duration_ms': 1}}), flush=True)",
                "    print(json.dumps({'type': 'result', 'result': {'ok': True}}), flush=True)",
                "else:",
                "    print(json.dumps({'error': 'bad action'}))",
            ]
        ),
        encoding="utf-8",
    )
    lifecycle = GVisorHelperWorkerLifecycle([sys.executable, str(helper)])
    worker = lifecycle.create("manager.1.slot-1")

    with patch("agentcicd.sandbox.manager.object_store_from_env", lambda: Store()):
        response = lifecycle.invoke(
            worker,
            "check",
            {},
            trace={
                "trace_id": "trace-gvisor",
                "parent_span_id": "root-span",
                "parent_call_id": "rtcall_root",
            },
        )

    assert response["result"] == {"ok": True}
    assert response["trace_summary"]["trace_id"] == "trace-gvisor"
    spans_uri = "agentcicd-object://org.test/runs/run.test/attempt_1/debug/fixture_traces/trace-gvisor/spans.jsonl"
    assert spans_uri in written
    assert '"name":"fixture.child"' in str(written[spans_uri])


def test_manager_gvisor_timeout_returns_trace_summary(tmp_path, monkeypatch) -> None:
    written: dict[str, object] = {}

    class Store:
        def put_json(self, uri: str, payload: object) -> None:
            written[uri] = payload

        def put_text(self, uri: str, payload: str, content_type: str = "text/plain") -> None:
            written[uri] = payload

    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
    helper = tmp_path / "helper_timeout.py"
    helper.write_text(
        "\n".join(
            [
                "import json, sys, time",
                "payload = json.loads(sys.stdin.read() or '{}')",
                "if payload.get('action') == 'create':",
                "    print(json.dumps({'worker_id': 'gv-' + payload['slot_id']}), flush=True)",
                "elif payload.get('action') == 'invoke':",
                "    trace = payload.get('trace') or {}",
                "    print(json.dumps({'type': 'trace_record', 'record': {'record_type': 'span', 'trace_id': trace.get('trace_id'), 'span_id': 'child-span', 'parent_span_id': trace.get('parent_span_id'), 'name': 'fixture.waiting', 'kind': 'span', 'status': 'running', 'started_at': '2026-01-01T00:00:00Z'}}), flush=True)",
                "    time.sleep(5)",
                "else:",
                "    print(json.dumps({'ok': True}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    lifecycle = GVisorHelperWorkerLifecycle([sys.executable, str(helper)])
    config = replace(_config("sandbox"), call_timeout_seconds=0.5)
    manager = SandboxManager(config, lifecycle)
    lease = {**_lease(), "pool_kind": "sandbox"}

    with patch("agentcicd.sandbox.manager.object_store_from_env", lambda: Store()):
        status, payload = manager.invoke(
            "check",
            {
                "args": {},
                "lease": lease,
                "trace": {
                    "trace_id": "trace-gvisor-timeout",
                    "parent_span_id": "root-span",
                    "parent_call_id": "rtcall_root",
                },
            },
        )

    assert status == 408
    assert payload["trace_summary"]["trace_id"] == "trace-gvisor-timeout"
    spans_uri = "agentcicd-object://org.test/runs/run.test/attempt_1/debug/fixture_traces/trace-gvisor-timeout/spans.jsonl"
    assert spans_uri in written
    assert '"name":"fixture.waiting"' in str(written[spans_uri])


def test_manager_config_prefers_source_entrypoint_over_runtime_alias(monkeypatch) -> None:
    monkeypatch.setenv("AGENTCICD_FUNCTION_ID", "fixture.echo")
    monkeypatch.setenv("AGENTCICD_FUNCTION_CALL_NAME", "e2e.pool.service_echo")
    monkeypatch.setenv("AGENTCICD_FUNCTION_RUNTIME_ALIAS", "e2e_pool_service_echo")
    monkeypatch.setenv("AGENTCICD_FUNCTION_ENTRYPOINT_NAME", "service_echo")
    monkeypatch.setenv("AGENTCICD_FUNCTION_POOL_KIND", "service")

    config = manager_config_from_env()

    assert config.function_name == "service_echo"
