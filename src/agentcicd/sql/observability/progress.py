from __future__ import annotations

from typing import Any

from agentcicd.sql.contracts import ProgressCallbackEvent
from agentcicd.sql.observability.events import DiagnosticEvent


def progress_event_to_diagnostic(event: ProgressCallbackEvent) -> DiagnosticEvent:
    details: dict[str, Any] = {}
    if event.metadata:
        details.update(event.metadata)
    if event.error:
        details["error"] = event.error
    return DiagnosticEvent(
        event="progress.updated",
        severity="error" if event.status == "failed" else "info",
        stage_name=event.step_name,
        stage_kind=event.step_type,
        details=details,
    )


def diagnostic_to_progress_event(event: DiagnosticEvent) -> ProgressCallbackEvent:
    details = dict(event.details or {})
    status = "failed" if event.severity == "error" else "started"
    error = details.pop("error", None)
    return ProgressCallbackEvent(
        step_type=event.stage_kind or "diagnostic",
        step_name=event.stage_name or event.event,
        status=status,
        error=error if isinstance(error, str) else None,
        metadata=details,
    )
