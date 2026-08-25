from __future__ import annotations

import json

from agentcicd_dp_common.object_store import FakeObjectStore
from agentcicd.sql.observability import fixture_traces
from agentcicd.sql.observability.fixture_traces import fixture_trace_context, start_fixture_trace


def test_fixture_trace_writes_summary_and_jsonl(monkeypatch) -> None:
    store = FakeObjectStore()
    monkeypatch.setenv("AGENTCICD_FIXTURE_CALL_TRACING_ENABLED", "1")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org/runs/run.test/attempt_1")
    monkeypatch.setattr(fixture_traces, "object_store_from_env", lambda: store)

    trace = start_fixture_trace(
        function_name="fixture.example",
        runtime_alias="fixture_example",
        backend="spark_udf",
        execution_runtime="local_python",
        payload_args={"prompt": "hello", "api_key": "secret"},
    )
    assert trace is not None
    with fixture_trace_context(trace):
        with trace.span("launch_service", {"port": 1234}):
            pass
        trace.event("artifact_collected", {"path": "answer/plot.png"})

    metadata = trace.finish(status="ok", duration_ms=42, result_preview={"answer": "done"})

    assert metadata is not None
    assert metadata["trace_id"] == trace.trace_id
    assert metadata["span_id"] == trace.span_id
    assert metadata["trace_summary_path"] == f"debug/fixture_traces/{trace.trace_id}/summary.json"
    assert metadata["trace_spans_path"] == f"debug/fixture_traces/{trace.trace_id}/spans.jsonl"

    summary = store.get_json(f"agentcicd-object://org/runs/run.test/attempt_1/{metadata['trace_summary_path']}")
    assert summary["trace_id"] == trace.trace_id
    assert summary["span_count"] == 2

    raw_spans = store.get_text(f"agentcicd-object://org/runs/run.test/attempt_1/{metadata['trace_spans_path']}")
    records = [json.loads(line) for line in raw_spans.splitlines()]
    assert records[0]["name"] == "agentcicd.fixture.call"
    assert records[1]["name"] == "launch_service"
    assert records[2]["record_type"] == "event"
    assert records[0]["attributes"]["input_preview"]["api_key"] == "[redacted]"


def test_fixture_trace_merges_remote_child_records(monkeypatch) -> None:
    store = FakeObjectStore()
    monkeypatch.setenv("AGENTCICD_FIXTURE_CALL_TRACING_ENABLED", "1")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org/runs/run.test/attempt_1")
    monkeypatch.setattr(fixture_traces, "object_store_from_env", lambda: store)

    trace = start_fixture_trace(
        function_name="fixture.example",
        runtime_alias="fixture_example",
        backend="http",
        execution_runtime="function_runner",
    )
    assert trace is not None
    context = trace.request_context()
    trace.extend_records(
        [
            {
                "record_type": "span",
                "trace_id": context["trace_id"],
                "span_id": "child-span",
                "parent_span_id": context["parent_span_id"],
                "call_id": "rtcall_child",
                "name": "filesystem.materialize",
                "kind": "span",
                "status": "ok",
                "started_at": "2026-07-05T00:00:00Z",
                "duration_ms": 12,
                "attributes": {"environment_kind": "filesystem"},
            }
        ]
    )

    metadata = trace.finish(status="ok", duration_ms=42)

    assert metadata is not None
    assert metadata["span_count"] == 2
    records = [
        json.loads(line)
        for line in store.get_text(f"agentcicd-object://org/runs/run.test/attempt_1/{metadata['trace_spans_path']}").splitlines()
    ]
    assert [record["name"] for record in records] == ["agentcicd.fixture.call", "filesystem.materialize"]
