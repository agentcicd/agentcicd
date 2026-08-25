import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from agentcicd.sql.contracts import ProgressCallbackEvent
from agentcicd.sql.engine.config import RunRuntimeConfig
from agentcicd.sql.observability.sinks import LocalJsonlSink, ObjectStoreJsonlSink
from agentcicd.sql.parsing.sql_segments import ProgressEvent, ProgressStatus
try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside the Spark image
    object_store_from_env = None  # type: ignore[assignment]

STATUS_BY_NAME = {
    "started": ProgressStatus.RUNNING,
    "completed": ProgressStatus.COMPLETED,
    "failed": ProgressStatus.FAILED,
    "waiting": ProgressStatus.WAITING,
}
logger = logging.getLogger(__name__)


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _progress_key(step_type: str, step_name: str) -> str:
    return f"{step_type}:{step_name}"


def _progress_status_from_name(status_name: str) -> ProgressStatus:
    return STATUS_BY_NAME.get(status_name, ProgressStatus.RUNNING)


def _append_progress_event(progress_path: Path, payload: Dict[str, object]) -> None:
    LocalJsonlSink(progress_path).emit(dict(payload))


class ProgressReporter:
    def __init__(self, progress_path: Optional[Path], runtime_config: RunRuntimeConfig | None = None) -> None:
        self._progress_path = progress_path
        self._runtime_config = runtime_config
        self._progress_events_uri = (
            runtime_config.progress_events_uri
            if runtime_config is not None
            else os.getenv("AGENTCICD_PROGRESS_EVENTS_URI", "").strip()
        ) or ""
        self._object_event_count = 0
        self._start_times: Dict[str, float] = {}

    def emit(
        self,
        step_type: str,
        step_name: str,
        status: str,
        error: Optional[str],
        metadata: Optional[Dict[str, object]] = None,
    ) -> None:
        timestamp = _utc_timestamp()
        progress_status = _progress_status_from_name(status)
        event = ProgressEvent(
            step_type=step_type,
            step_name=step_name,
            status=progress_status,
            timestamp=timestamp,
            error=error,
        )

        self._apply_timing(
            event=event,
            event_key=_progress_key(step_type, step_name),
            progress_status=progress_status,
            timestamp=timestamp,
        )
        self._apply_metadata(event, metadata)
        payload = event.to_dict()
        if self._progress_path is not None:
            _append_progress_event(self._progress_path, payload)
        self._log_progress_event(payload, progress_status)
        self._append_object_progress_event(payload)

    def _append_object_progress_event(self, payload: Dict[str, object]) -> None:
        store_factory = self._runtime_config.object_store_factory if self._runtime_config else None
        if not self._progress_events_uri or (store_factory is None and object_store_from_env is None):
            return
        progress_root = self._progress_events_uri.rstrip("/")
        if progress_root.endswith("/events"):
            progress_root = progress_root.removesuffix("/events")
        event_uri = f"{progress_root}/events/part-{self._object_event_count:06d}.jsonl"
        summary_uri = f"{progress_root}/summary.json"
        self._object_event_count += 1
        store = store_factory() if store_factory is not None else object_store_from_env()
        ObjectStoreJsonlSink(store, event_uri).emit(dict(payload))
        store.put_json(
            summary_uri,
            {
                "status": "running",
                "event_count": self._object_event_count,
                "chunk_count": self._object_event_count,
                "latest_chunk_uri": event_uri,
                "updated_at": _utc_timestamp(),
            },
        )

    def emit_event(self, event: ProgressCallbackEvent) -> None:
        self.emit(
            event.step_type,
            event.step_name,
            event.status,
            event.error,
            event.metadata,
        )

    def _log_progress_event(self, payload: Dict[str, object], progress_status: ProgressStatus) -> None:
        step_label = _format_step_label(
            str(payload.get("step_type") or ""),
            str(payload.get("step_name") or ""),
        )
        duration = _format_duration(payload)
        error = str(payload.get("error") or "").strip()
        if progress_status == ProgressStatus.RUNNING:
            logger.info("Started %s", step_label)
        elif progress_status == ProgressStatus.WAITING:
            logger.info("Waiting for %s", step_label)
        elif progress_status == ProgressStatus.COMPLETED:
            logger.info("Completed %s%s", step_label, duration)
        elif progress_status == ProgressStatus.FAILED:
            detail = f": {error}" if error else ""
            logger.error("Failed %s%s%s", step_label, duration, detail)
        else:
            logger.info("Updated %s", step_label)

    def _apply_timing(
        self,
        event: ProgressEvent,
        event_key: str,
        progress_status: ProgressStatus,
        timestamp: str,
    ) -> None:
        if progress_status == ProgressStatus.RUNNING:
            self._start_times[event_key] = time.monotonic()
            event.started_at = timestamp
            return

        if progress_status not in {ProgressStatus.COMPLETED, ProgressStatus.FAILED}:
            return

        event.finished_at = timestamp
        start_time = self._start_times.pop(event_key, None)
        if start_time is None:
            return
        event.duration_ms = int((time.monotonic() - start_time) * 1000)

    def _apply_metadata(
        self,
        event: ProgressEvent,
        metadata: Optional[Dict[str, object]],
    ) -> None:
        if metadata is None:
            return

        action = metadata.get("action")
        annotation_request_id = metadata.get("annotation_request_id")
        source_ref = metadata.get("source_ref")
        data_path = metadata.get("data_path")
        target_table = metadata.get("target_table")
        error_category = metadata.get("error_category")
        error_type = metadata.get("error_type")
        error_summary = metadata.get("error_summary")
        error_traceback = metadata.get("error_traceback")
        debug_log_path = metadata.get("debug_log_path")
        row_count = metadata.get("row_count")
        row_error_count = metadata.get("row_error_count")
        cell_error_count = metadata.get("cell_error_count")
        reuse_state = metadata.get("reuse_state")
        cache_hits = metadata.get("cache_hits")
        cache_misses = metadata.get("cache_misses")
        cache_writes = metadata.get("cache_writes")

        event.action = action if isinstance(action, str) else None
        event.annotation_request_id = annotation_request_id if isinstance(annotation_request_id, str) else None
        event.source_ref = source_ref if isinstance(source_ref, str) else None
        event.data_path = data_path if isinstance(data_path, str) else None
        event.target_table = target_table if isinstance(target_table, str) else None
        event.error_category = error_category if isinstance(error_category, str) else None
        event.error_type = error_type if isinstance(error_type, str) else None
        event.error_summary = error_summary if isinstance(error_summary, str) else None
        event.error_traceback = error_traceback if isinstance(error_traceback, str) else None
        event.debug_log_path = debug_log_path if isinstance(debug_log_path, str) else None
        event.row_count = row_count if isinstance(row_count, int) else None
        event.row_error_count = row_error_count if isinstance(row_error_count, int) else None
        event.cell_error_count = cell_error_count if isinstance(cell_error_count, int) else None
        event.reuse_state = reuse_state if isinstance(reuse_state, str) else None
        event.cache_hits = cache_hits if isinstance(cache_hits, int) else None
        event.cache_misses = cache_misses if isinstance(cache_misses, int) else None
        event.cache_writes = cache_writes if isinstance(cache_writes, int) else None


def _format_step_label(step_type: str, step_name: str) -> str:
    normalized = step_type.strip().lower()
    labels = {
        "declare_input": "input",
        "declare_variable": "input",
        "create_batch_table": "table",
        "create_stream_table": "table",
        "load_table": "load",
        "save_table": "save",
        "publish_report": "report publish",
        "publish_dataset": "dataset publish",
        "publish_annotation": "annotation publish",
        "retrieve_annotation": "annotation retrieval",
    }
    step_kind = labels.get(normalized, normalized.replace("_", " ") or "step")
    name = step_name.strip()
    if not name:
        return step_kind
    return f"{step_kind} {name}"


def _format_duration(payload: Dict[str, object]) -> str:
    duration_ms = payload.get("duration_ms")
    if not isinstance(duration_ms, int):
        return ""
    if duration_ms < 1000:
        return f" in {duration_ms} ms"
    return f" in {duration_ms / 1000:.1f} s"
