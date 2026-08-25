from __future__ import annotations

import json

import pytest

from agentcicd.sql.observability.events import DiagnosticEvent
from agentcicd.sql.engine.config import RunRuntimeConfig
from agentcicd.sql.engine.progress_reporter import ProgressReporter
from agentcicd.sql.observability.diagnostics import failed_stage_event
from agentcicd.sql.observability.progress import diagnostic_to_progress_event
from agentcicd.sql.observability.sinks import FanoutDiagnosticSink, LocalJsonlSink, ObjectStoreJsonlSink


pytestmark = pytest.mark.smoke


class _MemoryStore:
    def __init__(self) -> None:
        self.text: dict[str, str] = {}
        self.json: dict[str, object] = {}

    def get_text(self, uri: str) -> str:
        return self.text[uri]

    def put_text(self, uri: str, value: str, *, content_type: str) -> None:
        assert content_type == "application/x-ndjson"
        self.text[uri] = value

    def put_json(self, uri: str, value: object) -> None:
        self.json[uri] = value


def test_local_and_object_store_sinks_receive_identical_events(tmp_path):
    store = _MemoryStore()
    path = tmp_path / "app.jsonl"
    uri = "agentcicd-object://org/runs/run-1/logs/app.jsonl"
    sink = FanoutDiagnosticSink(LocalJsonlSink(path), ObjectStoreJsonlSink(store, uri))
    event = DiagnosticEvent(
        event="stage.failed",
        severity="error",
        run_id="run-1",
        attempt=2,
        stage_name="evaluated",
        stage_kind="batch",
        details={"exception_type": "RuntimeError", "dependency_blocked": False},
        timestamp="2026-01-01T00:00:00Z",
    )

    sink.emit(event.to_dict())

    local_payload = json.loads(path.read_text(encoding="utf-8").strip())
    object_payload = json.loads(store.text[uri].strip())
    assert local_payload == object_payload
    assert local_payload["stage_name"] == "evaluated"
    assert local_payload["details"]["exception_type"] == "RuntimeError"


def test_failed_stage_diagnostic_includes_dependency_metadata():
    event = failed_stage_event(
        stage_name="scored",
        stage_kind="batch",
        exc=RuntimeError("upstream failed"),
        run_id="run-1",
        attempt=3,
        organization_id="org-1",
        dependency_blocked=True,
    )

    payload = event.to_dict()

    assert payload["event"] == "stage.failed"
    assert payload["run_id"] == "run-1"
    assert payload["stage_name"] == "scored"
    assert payload["details"]["exception_type"] == "RuntimeError"
    assert payload["details"]["dependency_blocked"] is True


def test_diagnostic_progress_adapter_preserves_public_event_shape():
    event = DiagnosticEvent(
        event="stage.failed",
        severity="error",
        stage_name="scored",
        stage_kind="create_batch_table",
        details={"error": "remote 400", "error_type": "HTTPError"},
    )

    progress = diagnostic_to_progress_event(event)

    assert progress.step_type == "create_batch_table"
    assert progress.step_name == "scored"
    assert progress.status == "failed"
    assert progress.error == "remote 400"
    assert progress.metadata == {"error_type": "HTTPError"}


def test_progress_reporter_uses_typed_runtime_config_for_object_store_events():
    store = _MemoryStore()
    reporter = ProgressReporter(
        None,
        RunRuntimeConfig(
            progress_events_uri="agentcicd-object://org/runs/run-1/progress",
            object_store_factory=lambda: store,
        ),
    )

    reporter.emit("create_batch_table", "scored", "started", None, {"target_table": "scored"})

    event_uri = "agentcicd-object://org/runs/run-1/progress/events/part-000000.jsonl"
    summary_uri = "agentcicd-object://org/runs/run-1/progress/summary.json"
    payload = json.loads(store.text[event_uri])
    assert payload["step_type"] == "create_batch_table"
    assert payload["target_table"] == "scored"
    assert store.json[summary_uri]["event_count"] == 1
