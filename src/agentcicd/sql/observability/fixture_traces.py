from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from agentcicd.sql.runtime.udf_compat.tracing import use_runtime_trace
from agentcicd.sql.observability.redaction import redacted_preview

try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside DP runtime images
    object_store_from_env = None  # type: ignore[assignment]

SCHEMA_VERSION = "agentcicd.fixture_trace.v1"


def fixture_call_tracing_enabled() -> bool:
    return _truthy(os.getenv("AGENTCICD_FIXTURE_CALL_TRACING_ENABLED", "")) and bool(
        os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    )


@dataclass
class FixtureTraceRecorder:
    function_name: str
    runtime_alias: str
    backend: str
    execution_runtime: str
    input_preview: Any | None = None
    cache_hit: bool = False
    limiter_key: str | None = None
    max_in_flight: int | None = None
    pool_name: str | None = None
    pool_kind: str | None = None
    fixture_id: str | None = None
    image_id: str | None = None
    trace_id: str = field(default_factory=lambda: secrets.token_hex(16))
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    call_id: str = field(default_factory=lambda: f"rtcall_{secrets.token_hex(12)}")
    parent_span_id: str | None = None
    parent_call_id: str | None = None
    _started_at: float = field(default_factory=time.perf_counter)
    _started_at_iso: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    _records: list[dict[str, Any]] = field(default_factory=list)
    _span_stack: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._records.append(
            self._span_record(
                span_id=self.span_id,
                parent_span_id=self.parent_span_id,
                call_id=self.call_id,
                name="agentcicd.fixture.call",
                kind="call",
                status="running",
                started_at=self._started_at_iso,
                attributes={
                    "function_name": self.function_name,
                    "runtime_alias": self.runtime_alias,
                    "backend": self.backend,
                    "execution_runtime": self.execution_runtime,
                    "cache_hit": self.cache_hit,
                    "limiter_key": self.limiter_key,
                    "max_in_flight": self.max_in_flight,
                    "pool_name": self.pool_name,
                    "pool_kind": self.pool_kind,
                    "fixture_id": self.fixture_id,
                    "image_id": self.image_id,
                    "input_preview": self.input_preview,
                },
            )
        )

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        span_id = secrets.token_hex(8)
        parent_span_id = self._span_stack[-1] if self._span_stack else self.span_id
        started_at = time.perf_counter()
        started_at_iso = _utc_now()
        status = "ok"
        error_message = None
        try:
            self._span_stack.append(span_id)
            yield None
        except Exception as exc:
            status = "error"
            error_message = str(exc)
            raise
        finally:
            if self._span_stack and self._span_stack[-1] == span_id:
                self._span_stack.pop()
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            record = self._span_record(
                span_id=span_id,
                parent_span_id=parent_span_id,
                call_id=f"rtcall_{secrets.token_hex(12)}",
                name=name,
                kind="span",
                status=status,
                started_at=started_at_iso,
                duration_ms=duration_ms,
                attributes=attributes or {},
            )
            if error_message:
                record["error_message"] = error_message
            self._records.append(record)

    def request_context(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "parent_span_id": self.span_id,
            "parent_call_id": self.call_id,
            "function_name": self.function_name,
            "runtime_alias": self.runtime_alias,
            "backend": self.backend,
            "execution_runtime": self.execution_runtime,
            "fixture_id": self.fixture_id,
            "image_id": self.image_id,
            "pool_name": self.pool_name,
            "pool_kind": self.pool_kind,
            "cache_hit": self.cache_hit,
            "limiter_key": self.limiter_key,
            "max_in_flight": self.max_in_flight,
            "started_at": self._started_at_iso,
            "max_preview_bytes": _max_preview_bytes(),
        }

    def extend_records(self, records: list[dict[str, Any]] | None) -> None:
        if not records:
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("trace_id") != self.trace_id:
                continue
            record_type = str(record.get("record_type") or "")
            if record_type not in {"span", "event"}:
                continue
            cleaned = _drop_none(dict(record))
            if record_type == "span" and not cleaned.get("parent_span_id"):
                cleaned["parent_span_id"] = self.span_id
            self._records.append(cleaned)

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self._records.append(
            {
                "record_type": "event",
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "name": name,
                "timestamp": _utc_now(),
                "attributes": redacted_preview(attributes or {}, max_preview_bytes=_max_preview_bytes()),
            }
        )

    def finish(
        self,
        *,
        status: str,
        duration_ms: int,
        result_preview: Any | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
    ) -> dict[str, Any] | None:
        finished_at = _utc_now()
        self._records[0].update(
            {
                "status": status,
                "duration_ms": duration_ms,
                "finished_at": finished_at,
                "error_code": error_code,
                "error_message": error_message,
                "error_type": error_type,
                "http_status": http_status,
                "result_preview": redacted_preview(result_preview, max_preview_bytes=_max_preview_bytes()),
            }
        )
        return _write_trace_and_summary(self, finished_at=finished_at)

    def _span_record(
        self,
        *,
        span_id: str,
        parent_span_id: str | None,
        call_id: str,
        name: str,
        kind: str,
        status: str,
        started_at: str,
        duration_ms: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "record_type": "span",
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "call_id": call_id,
            "name": name,
            "kind": kind,
            "status": status,
            "started_at": started_at,
            "duration_ms": duration_ms,
            "attributes": redacted_preview(attributes or {}, max_preview_bytes=_max_preview_bytes()),
        }


@contextmanager
def fixture_trace_context(recorder: FixtureTraceRecorder | None) -> Iterator[FixtureTraceRecorder | None]:
    if recorder is None:
        yield None
        return
    with use_runtime_trace(recorder):
        yield recorder


def start_fixture_trace(
    *,
    function_name: str,
    runtime_alias: str,
    backend: str,
    execution_runtime: str,
    payload_args: dict[str, Any] | None = None,
    cache_hit: bool = False,
    limiter_key: str | None = None,
    max_in_flight: int | None = None,
    pool_name: str | None = None,
    pool_kind: str | None = None,
    fixture_id: str | None = None,
    image_id: str | None = None,
) -> FixtureTraceRecorder | None:
    if not fixture_call_tracing_enabled():
        return None
    return FixtureTraceRecorder(
        function_name=function_name,
        runtime_alias=runtime_alias,
        backend=backend,
        execution_runtime=execution_runtime,
        input_preview=redacted_preview(payload_args or {}, max_preview_bytes=_max_preview_bytes()),
        cache_hit=cache_hit,
        limiter_key=limiter_key,
        max_in_flight=max_in_flight,
        pool_name=pool_name,
        pool_kind=pool_kind,
        fixture_id=fixture_id,
        image_id=image_id,
    )


def _write_trace_and_summary(recorder: FixtureTraceRecorder, *, finished_at: str) -> dict[str, Any] | None:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri or object_store_from_env is None:
        return None
    error_records = [record for record in recorder._records if str(record.get("status") or "") == "error"]
    root_record = recorder._records[0]
    top_error = str(root_record.get("error_message") or "") or None
    if top_error is None and error_records:
        top_error = str(error_records[0].get("error_message") or "") or None
    trace_dir = f"debug/fixture_traces/{recorder.trace_id}"
    summary_path = f"{trace_dir}/summary.json"
    spans_path = f"{trace_dir}/spans.jsonl"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": recorder.trace_id,
        "root_span_id": recorder.span_id,
        "root_call_id": recorder.call_id,
        "function_name": recorder.function_name,
        "runtime_alias": recorder.runtime_alias,
        "backend": recorder.backend,
        "execution_runtime": recorder.execution_runtime,
        "status": root_record.get("status"),
        "duration_ms": root_record.get("duration_ms"),
        "span_count": sum(1 for record in recorder._records if record.get("record_type") == "span"),
        "error_count": len(error_records),
        "top_error": top_error,
        "started_at": recorder._started_at_iso,
        "finished_at": finished_at,
        "spans_path": spans_path,
    }
    records = sorted(recorder._records, key=_record_sort_key)
    payload = "\n".join(json.dumps(_drop_none(record), sort_keys=True, separators=(",", ":"), default=str) for record in records)
    if payload:
        payload += "\n"
    try:
        store = object_store_from_env()
        store.put_json(f"{run_object_uri.rstrip('/')}/{summary_path}", _drop_none(summary))
        store.put_text(
            f"{run_object_uri.rstrip('/')}/{spans_path}",
            payload,
            content_type="application/x-ndjson",
        )
    except Exception:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "call_id": recorder.call_id,
        "parent_call_id": recorder.parent_call_id,
        "trace_id": recorder.trace_id,
        "span_id": recorder.span_id,
        "parent_span_id": recorder.parent_span_id,
        "function_name": recorder.function_name,
        "runtime_alias": recorder.runtime_alias,
        "backend": recorder.backend,
        "fixture_id": recorder.fixture_id,
        "image_id": recorder.image_id,
        "execution_runtime": recorder.execution_runtime,
        "status": root_record.get("status"),
        "duration_ms": root_record.get("duration_ms"),
        "cache_hit": recorder.cache_hit,
        "limiter_key": recorder.limiter_key,
        "max_in_flight": recorder.max_in_flight,
        "pool_name": recorder.pool_name,
        "pool_kind": recorder.pool_kind,
        "http_status": root_record.get("http_status"),
        "error_code": root_record.get("error_code"),
        "error_message": root_record.get("error_message"),
        "error_type": root_record.get("error_type"),
        "summary": "Fixture failed" if root_record.get("status") == "error" else "Fixture completed",
        "top_error": top_error,
        "span_count": summary["span_count"],
        "error_count": summary["error_count"],
        "trace_summary_path": summary_path,
        "trace_spans_path": spans_path,
    }


def _record_sort_key(record: dict[str, Any]) -> tuple[int, int, str, str]:
    if record.get("record_type") == "event":
        depth = 1
    elif record.get("span_id") == record.get("parent_span_id"):
        depth = 1
    elif record.get("parent_span_id"):
        depth = 1
    else:
        depth = 0
    record_kind = 0 if record.get("record_type") == "span" else 1
    return (
        depth,
        record_kind,
        str(record.get("started_at") or record.get("timestamp") or ""),
        str(record.get("span_id") or record.get("call_id") or ""),
    )


def _max_preview_bytes() -> int:
    raw = os.getenv("AGENTCICD_FIXTURE_TRACE_MAX_PREVIEW_BYTES", "").strip()
    if not raw:
        return 4096
    try:
        return max(1, int(raw))
    except ValueError:
        return 4096


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
