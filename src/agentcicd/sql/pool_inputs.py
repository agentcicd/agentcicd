from __future__ import annotations

import ast
import json
import re
from typing import Any, Mapping

from agentcicd.sql.ir.statements import DeclareInputStmt

POOL_KINDS = {"executor", "service", "session", "sandbox"}
_EXECUTOR_FIELDS = {
    "kind",
    "min_workers",
    "max_workers",
    "cores_per_worker",
    "memory_per_worker",
    "task_cpus",
    "max_parallel_stages",
}
_FIXTURE_FIELDS = {
    "kind",
    "min_instances",
    "min_warm",
    "max_instances",
    "cpu_per_instance",
    "memory_per_instance",
    "timeout_seconds",
    "lease_ttl_seconds",
    "reset_timeout_seconds",
    "idle_ttl_seconds",
}
_INT_FIELDS = {
    "min_workers",
    "max_workers",
    "cores_per_worker",
    "task_cpus",
    "max_parallel_stages",
    "min_instances",
    "min_warm",
    "max_instances",
    "timeout_seconds",
    "lease_ttl_seconds",
    "reset_timeout_seconds",
    "idle_ttl_seconds",
}
_NON_NEGATIVE_FIELDS = {"min_workers", "min_instances", "min_warm"}
_CPU_FIELDS = {"cpu_per_instance"}
_MEMORY_FIELDS = {"memory_per_worker", "memory_per_instance"}


def pool_kind_from_statement(statement: DeclareInputStmt) -> str:
    options = _normalized_options(statement.options)
    kind = str(options.get("kind") or "").strip().lower()
    if kind not in POOL_KINDS:
        raise ValueError("DECLARE INPUT POOL requires WITH kind = 'executor|service|session|sandbox'")
    return kind


def canonical_pool_default_json(statement: DeclareInputStmt) -> str:
    kind = pool_kind_from_statement(statement)
    payload = parse_pool_default(statement.default_sql)
    if payload.get("kind") is None:
        payload["kind"] = kind
    elif str(payload["kind"]).strip().lower() != kind:
        raise ValueError("DECLARE INPUT POOL DEFAULT kind must match WITH kind")
    return json.dumps(validate_pool_payload(payload), sort_keys=True, separators=(",", ":"))


def parse_pool_default(default_sql: str | None) -> dict[str, Any]:
    if default_sql is None or not str(default_sql).strip():
        return {}
    text = str(default_sql).strip()
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("DECLARE INPUT POOL DEFAULT must be a JSON-like object literal") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise ValueError("DECLARE INPUT POOL DEFAULT must be a JSON-like object literal") from exc
    if not isinstance(value, dict):
        raise ValueError("DECLARE INPUT POOL DEFAULT must be a JSON-like object literal")
    return {str(key).strip(): item for key, item in value.items()}


def validate_pool_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {str(key).strip().lower(): value for key, value in dict(payload).items()}
    kind = str(normalized.get("kind") or "").strip().lower()
    if kind not in POOL_KINDS:
        raise ValueError("POOL input value requires kind 'executor', 'service', 'session', or 'sandbox'")
    allowed = _EXECUTOR_FIELDS if kind == "executor" else _FIXTURE_FIELDS
    unsupported = sorted(key for key in normalized if key not in allowed)
    if unsupported:
        raise ValueError(f"Unsupported {kind} POOL field(s): {', '.join(unsupported)}")
    for field in list(normalized):
        value = normalized[field]
        if value is None:
            normalized.pop(field)
            continue
        if field in _INT_FIELDS:
            normalized[field] = _validate_int_field(field, value)
        elif field in _CPU_FIELDS:
            normalized[field] = _validate_cpu_field(field, value)
        elif field in _MEMORY_FIELDS:
            normalized[field] = _normalize_memory(str(value))
    _validate_pool_bounds(normalized)
    return normalized


def canonical_pool_value_json(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = parse_pool_default(value)
    return json.dumps(validate_pool_payload(parsed), sort_keys=True, separators=(",", ":"))


def _normalized_options(options: Mapping[str, object]) -> dict[str, object]:
    return {str(key).strip().lower(): value for key, value in dict(options).items()}


def _validate_int_field(field: str, value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"POOL field '{field}' must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"POOL field '{field}' must be an integer") from exc
    if field in _NON_NEGATIVE_FIELDS:
        if parsed < 0:
            raise ValueError(f"POOL field '{field}' must be greater than or equal to 0")
    elif parsed < 1:
        raise ValueError(f"POOL field '{field}' must be greater than or equal to 1")
    return parsed


def _validate_cpu_field(field: str, value: object) -> str:
    raw = str(value).strip()
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ValueError(f"POOL field '{field}' must be a positive CPU value") from exc
    if parsed <= 0:
        raise ValueError(f"POOL field '{field}' must be positive")
    return raw


def _normalize_memory(value: str) -> str:
    raw = value.strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?", raw)
    if not match:
        raise ValueError(f"Invalid POOL memory value '{value}'. Use formats like 1g or 512m.")
    number, unit = match.groups()
    return f"{number}{unit}" if unit else number


def _validate_pool_bounds(payload: dict[str, Any]) -> None:
    kind = str(payload.get("kind") or "")
    if kind == "executor":
        _validate_max_gte_min(payload, "min_workers", "max_workers")
        return
    if kind == "service":
        _validate_max_gte_min(payload, "min_instances", "max_instances")
        return
    _validate_max_gte_min(payload, "min_warm", "max_instances")


def _validate_max_gte_min(payload: dict[str, Any], min_field: str, max_field: str) -> None:
    minimum = payload.get(min_field)
    maximum = payload.get(max_field)
    if minimum is not None and maximum is not None and int(maximum) < int(minimum):
        raise ValueError(f"POOL field '{max_field}' must be greater than or equal to '{min_field}'")
