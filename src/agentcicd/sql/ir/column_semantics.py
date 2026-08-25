from __future__ import annotations

from typing import Any, Mapping

SEMANTIC_TYPES = {"text", "markdown", "trace", "conversation", "code", "json", "directory", "image"}
TRACE_FORMATS = {"otel"}
DISPLAY_MODES = {"auto", "inline", "ref"}
CONVERSATION_FORMATS = {
    "a2a",
    "openai_chat",
    "openai_responses",
    "anthropic_messages",
    "generic_messages",
    "auto",
}
CODE_LANGUAGES = {"python", "javascript", "typescript", "sql", "json", "bash", "text"}


def empty_column_semantics() -> dict[str, Any]:
    return {"columns": {}}


def normalize_column_semantics(value: Any) -> dict[str, Any]:
    if value is None:
        return empty_column_semantics()
    if isinstance(value, Mapping) and isinstance(value.get("columns"), Mapping):
        raw_columns = value.get("columns")
    elif isinstance(value, Mapping):
        raw_columns = value
    else:
        raise ValueError("COLUMN_SEMANTICS must be an object mapping column names to semantic objects")

    columns: dict[str, Any] = {}
    for raw_name, raw_semantic in raw_columns.items():
        column_name = str(raw_name).strip()
        if not column_name:
            raise ValueError("COLUMN_SEMANTICS column names must be non-empty")
        columns[column_name] = normalize_column_semantic(raw_semantic, column_name=column_name)
    return {"columns": columns}


def normalize_column_semantic(value: Any, *, column_name: str = "") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"COLUMN_SEMANTICS for '{column_name}' must be an object")
    semantic_type = str(value.get("type") or "").strip().lower()
    if semantic_type not in SEMANTIC_TYPES:
        raise ValueError(f"Unsupported COLUMN_SEMANTICS type '{semantic_type}' for column '{column_name}'")
    if semantic_type == "text":
        return {"type": "text"}
    if semantic_type == "markdown":
        return {"type": "markdown"}
    if semantic_type == "json":
        return {"type": "code", "language": "json"}
    if semantic_type in {"directory", "image"}:
        display = str(value.get("display") or "auto").strip().lower()
        if display not in DISPLAY_MODES:
            raise ValueError(f"Unsupported {semantic_type} display mode '{display}' for column '{column_name}'")
        return {"type": semantic_type, "display": display}
    if semantic_type == "trace":
        trace_format = str(value.get("format") or "otel").strip().lower()
        if trace_format not in TRACE_FORMATS:
            raise ValueError(f"Unsupported trace format '{trace_format}' for column '{column_name}'")
        return {"type": "trace", "format": trace_format}
    if semantic_type == "conversation":
        conversation_format = str(value.get("format") or "auto").strip().lower()
        if conversation_format not in CONVERSATION_FORMATS:
            raise ValueError(f"Unsupported conversation format '{conversation_format}' for column '{column_name}'")
        return {"type": "conversation", "format": conversation_format}
    language = str(value.get("language") or "text").strip().lower()
    if language not in CODE_LANGUAGES:
        raise ValueError(f"Unsupported code language '{language}' for column '{column_name}'")
    return {"type": "code", "language": language}


def column_semantics_from_options(options: Any) -> dict[str, Any]:
    if not isinstance(options, Mapping):
        return empty_column_semantics()
    raw = options.get("column_semantics")
    if raw is None:
        return empty_column_semantics()
    return normalize_column_semantics(raw)
