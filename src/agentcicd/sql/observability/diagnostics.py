from __future__ import annotations

import traceback
from typing import Any

from agentcicd.sql.observability.events import DiagnosticEvent


def exception_details(exc: BaseException) -> dict[str, Any]:
    return {
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def failed_stage_event(
    *,
    stage_name: str,
    stage_kind: str,
    exc: BaseException,
    run_id: str | None = None,
    attempt: int | str | None = None,
    organization_id: str | None = None,
    dependency_blocked: bool = False,
    details: dict[str, Any] | None = None,
) -> DiagnosticEvent:
    event_details = exception_details(exc)
    event_details["dependency_blocked"] = dependency_blocked
    if details:
        event_details.update(details)
    return DiagnosticEvent(
        event="stage.failed",
        severity="error",
        run_id=run_id,
        attempt=attempt,
        organization_id=organization_id,
        stage_name=stage_name,
        stage_kind=stage_kind,
        details=event_details,
    )
