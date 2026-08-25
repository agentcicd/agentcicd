from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os

from agentcicd.sql.runtime.udf_compat.function import AsyncRowFunction
from agentcicd.sql.runtime.udf_compat.runtime_control import runtime_limiter, runtime_pool_lease
from agentcicd.sql.ir.functions import FunctionDefinitionIR

RUNTIME_CONTROL_TYPES = {"RATELIMIT", "POOL"}


def _http_timeout_seconds(metadata: dict[str, object], *, default: int) -> int:
    raw_value = metadata.get("timeout_seconds")
    if raw_value is None:
        raw_value = metadata.get("http_timeout_seconds")
    if raw_value is None:
        return default
    try:
        parsed = int(float(raw_value))
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return parsed

def _control_argument_indexes(definition: FunctionDefinitionIR) -> set[int]:
    return {
        index
        for index, parameter in enumerate(getattr(definition, "parameters", []) or [])
        if str(getattr(parameter, "type_sql", "") or "").strip().upper() in RUNTIME_CONTROL_TYPES
    }

def _udf_control_argument_indexes(udf_cls) -> set[int]:
    try:
        parameters = tuple(udf_cls().signature())
    except Exception:
        return set()
    return {
        index
        for index, parameter in enumerate(parameters)
        if str(getattr(parameter, "type_sql", "") or "").strip().upper() in RUNTIME_CONTROL_TYPES
    }

def _runtime_limit_for_local_function(function_instance: object, limiter_key: str, max_in_flight: int | None):
    if isinstance(function_instance, AsyncRowFunction):
        return _null_context()
    return runtime_limiter(max_in_flight, key=limiter_key).acquire_blocking(permits=1)

@contextmanager
def _null_context():
    yield

def _limiter_from_control_values(
    values: list[object],
    *,
    fallback_key: str = "default",
) -> tuple[str, int | None]:
    for value in values:
        payload = _rate_limit_payload(value)
        if payload is None:
            continue
        key = str(payload.get("key") or fallback_key).strip() or fallback_key
        raw_max = payload.get("max_in_flight")
        max_in_flight = int(raw_max) if raw_max is not None else None
        return key, max_in_flight
    return fallback_key, None


def _pool_from_control_values(values: list[object]) -> dict[str, object] | None:
    for value in values:
        payload = _pool_payload(value)
        if payload is not None:
            return payload
    return None

def _rate_limit_payload(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if _looks_like_pool_payload(value):
            return None
        kind = str(value.get("kind") or "").strip().lower()
        if kind and kind != "ratelimit":
            return None
        if kind == "ratelimit" or "max_in_flight" in value:
            return value
        return None
    as_dict = getattr(value, "asDict", None)
    if callable(as_dict):
        payload = dict(as_dict(recursive=True))
        return _rate_limit_payload(payload)
    if hasattr(value, "key") or hasattr(value, "max_in_flight"):
        return {
            "key": getattr(value, "key", None),
            "max_in_flight": getattr(value, "max_in_flight", None),
        }
    return None


def _looks_like_pool_payload(value: dict[str, object]) -> bool:
    kind = str(value.get("kind") or "").strip().lower()
    if kind in {"service", "session", "sandbox", "executor", "pool"}:
        return True
    return any(key in value for key in ("config", "config_json", "pool_name"))


def _pool_payload(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        payload = value
    else:
        as_dict = getattr(value, "asDict", None)
        if callable(as_dict):
            payload = dict(as_dict(recursive=True))
        elif hasattr(value, "key") or hasattr(value, "config_json"):
            payload = {
                "key": getattr(value, "key", None),
                "config_json": getattr(value, "config_json", None),
            }
        else:
            return None
    if not payload.get("config_json") and not payload.get("config") and not payload.get("pool_name"):
        return None
    normalized = dict(payload)
    config_json = normalized.get("config_json")
    if isinstance(config_json, str) and config_json.strip():
        try:
            parsed = json.loads(config_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            normalized["config"] = parsed
            normalized.setdefault("kind", parsed.get("kind"))
    normalized.setdefault("pool_name", normalized.get("key"))
    return normalized


def _pool_request_id(
    definition: FunctionDefinitionIR,
    payload_args: dict[str, object] | None = None,
) -> str:
    task_context = _spark_task_context()
    stage_id = str(task_context.stageId()) if task_context is not None else os.getenv("AGENTCICD_POOL_STAGE_ID", "")
    partition_id = str(task_context.partitionId()) if task_context is not None else os.getenv("AGENTCICD_POOL_PARTITION_ID", "")
    task_attempt = str(task_context.attemptNumber()) if task_context is not None else os.getenv("AGENTCICD_POOL_TASK_ATTEMPT", "")
    payload_hash = hashlib.sha256(
        json.dumps(payload_args or {}, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    parts = [
        os.getenv("AGENTCICD_RUN_ID", ""),
        os.getenv("AGENTCICD_RUN_ATTEMPT", ""),
        stage_id,
        partition_id,
        task_attempt,
        os.getenv("AGENTCICD_POOL_ROW_ID", ""),
        str(getattr(definition, "canonical_name", "") or ""),
        os.getenv("AGENTCICD_POOL_FUNCTION_CALL_ID", ""),
        payload_hash,
    ]
    return ":".join(part for part in parts if part)


def _spark_task_context():
    try:
        from pyspark import TaskContext
    except Exception:
        return None
    try:
        return TaskContext.get()
    except Exception:
        return None


def _pool_fixture_id(definition: FunctionDefinitionIR) -> str:
    metadata = getattr(definition, "metadata", {}) or {}
    for key in ("id", "fixture_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    fixture_ids = metadata.get("fixture_ids")
    if isinstance(fixture_ids, list) and len(fixture_ids) == 1:
        return str(fixture_ids[0] or "").strip()
    pool = metadata.get("pool")
    if isinstance(pool, dict):
        return str(pool.get("fixture_id") or "").strip()
    return ""


@contextmanager
def _runtime_pool_for_control_values(
    values: list[object],
    *,
    fallback_address: str | None = None,
    definition: FunctionDefinitionIR | None = None,
    payload_args: dict[str, object] | None = None,
):
    pool = _pool_from_control_values(values)
    request_id = _pool_request_id(definition, payload_args) if definition is not None else None
    fixture_id = _pool_fixture_id(definition) if definition is not None else None
    with runtime_pool_lease(
        pool,
        fallback_address=fallback_address,
        request_id=request_id,
        fixture_id=fixture_id,
    ) as lease:
        yield lease
