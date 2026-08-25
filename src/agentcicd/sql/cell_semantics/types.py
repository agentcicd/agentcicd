from __future__ import annotations

from typing import Any

CELL_MARKER_FIELD = "__agentcicd_cell"
CELL_VALUE_FIELD = "value"
CELL_METADATA_FIELD = "metadata"
CELL_ERRORS_FIELD = "errors"
CELL_LATENCY_FIELD = "latency_ms"
CELL_FIXTURE_TRACE_FIELD = "fixture_trace"


def cell_value(cell: Any) -> Any:
    if isinstance(cell, dict):
        return cell.get(CELL_VALUE_FIELD)
    return getattr(cell, CELL_VALUE_FIELD, None)


def cell_metadata(cell: Any) -> Any:
    if isinstance(cell, dict):
        return cell.get(CELL_METADATA_FIELD) or {}
    return getattr(cell, CELL_METADATA_FIELD, {}) or {}


def cell_errors(cell: Any) -> list[dict[str, Any]]:
    metadata = cell_metadata(cell)
    if isinstance(metadata, dict):
        raw_errors = metadata.get(CELL_ERRORS_FIELD)
    else:
        raw_errors = getattr(metadata, CELL_ERRORS_FIELD, None)
    if raw_errors is None:
        raw_errors = []
    if hasattr(raw_errors, "tolist"):
        raw_errors = raw_errors.tolist()
    return [dict(error) if isinstance(error, dict) else error.asDict(recursive=True) for error in raw_errors]


def cell_latency_ms(cell: Any) -> int | None:
    metadata = cell_metadata(cell)
    if isinstance(metadata, dict):
        raw_latency = metadata.get(CELL_LATENCY_FIELD)
    else:
        raw_latency = getattr(metadata, CELL_LATENCY_FIELD, None)
    if raw_latency is None:
        return None
    try:
        return int(raw_latency)
    except (TypeError, ValueError):
        return None


def cell_fixture_trace(cell: Any) -> dict[str, Any] | None:
    metadata = cell_metadata(cell)
    if isinstance(metadata, dict):
        raw_trace = metadata.get(CELL_FIXTURE_TRACE_FIELD)
    else:
        raw_trace = getattr(metadata, CELL_FIXTURE_TRACE_FIELD, None)
    if raw_trace is None:
        return None
    if isinstance(raw_trace, dict):
        return {key: value for key, value in raw_trace.items() if value is not None}
    if hasattr(raw_trace, "asDict"):
        return {key: value for key, value in raw_trace.asDict(recursive=True).items() if value is not None}
    return None


def merged_cell_errors(cells: tuple[Any, ...]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for cell in cells:
        errors.extend(cell_errors(cell))
    return errors


def clean_cell(
    value: Any,
    latency_ms: int | None = None,
    fixture_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        CELL_ERRORS_FIELD: [],
        CELL_LATENCY_FIELD: latency_ms,
        CELL_FIXTURE_TRACE_FIELD: fixture_trace,
    }
    return {
        "cell_id": None,
        CELL_VALUE_FIELD: value,
        CELL_METADATA_FIELD: metadata,
        CELL_MARKER_FIELD: True,
    }


def errored_cell(
    errors: list[dict[str, Any]],
    latency_ms: int | None = None,
    fixture_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        CELL_ERRORS_FIELD: errors,
        CELL_LATENCY_FIELD: latency_ms,
        CELL_FIXTURE_TRACE_FIELD: fixture_trace,
    }
    return {
        "cell_id": None,
        CELL_VALUE_FIELD: None,
        CELL_METADATA_FIELD: metadata,
        CELL_MARKER_FIELD: True,
    }


def is_error_payload(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("name"), str) and isinstance(value.get("message"), str)
