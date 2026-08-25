from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from agentcicd.sql.ir.functions import FunctionDefinitionIR

_RUNTIME_CALL_CACHE: dict[str, str] = {}


def _runtime_cache_enabled(metadata: dict[str, object]) -> bool:
    raw_cache = metadata.get("cache")
    if raw_cache is True:
        return True
    if isinstance(raw_cache, dict):
        return bool(raw_cache.get("enabled"))
    return False

def _runtime_cache_context() -> dict[str, str]:
    return {
        "organization_id": os.getenv("AGENTCICD_ORGANIZATION_ID", ""),
        "run_id": os.getenv("AGENTCICD_RUN_ID", ""),
        "recipe_id": os.getenv("AGENTCICD_RECIPE_ID", ""),
    }

def _runtime_cache_key(
    definition: FunctionDefinitionIR,
    payload_args: dict[str, Any],
    *,
    cache_context: dict[str, str] | None = None,
) -> str:
    metadata = getattr(definition, "metadata", {}) or {}
    context = cache_context or _runtime_cache_context()
    payload = {
        "organization_id": context.get("organization_id", ""),
        "run_id": context.get("run_id", ""),
        "recipe_id": context.get("recipe_id", ""),
        "function_name": getattr(definition, "canonical_name", ""),
        "function_version": metadata.get("version") or metadata.get("image_ref") or metadata.get("id") or "",
        "fixture_or_udf_id": metadata.get("id") or "",
        "fixture_or_udf_version": metadata.get("version") or metadata.get("image_ref") or "",
        "normalized_arguments": payload_args,
        "cache_policy": metadata.get("cache"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "fncache." + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _runtime_cache_get(cache_key: str | None) -> str | None:
    if cache_key is None:
        return None
    if cache_key in _RUNTIME_CALL_CACHE:
        return _RUNTIME_CALL_CACHE[cache_key]
    payload = _read_runtime_cache_file()
    raw_payload = payload.get(cache_key)
    if isinstance(raw_payload, str):
        _RUNTIME_CALL_CACHE[cache_key] = raw_payload
        return raw_payload
    return None

def _runtime_cache_put(cache_key: str | None, raw_payload: str) -> None:
    if cache_key is None:
        return
    _RUNTIME_CALL_CACHE[cache_key] = raw_payload
    def _merge(payload: dict[str, str]) -> dict[str, str]:
        payload[cache_key] = raw_payload
        return payload
    _update_runtime_cache_file(_merge)

def _runtime_cache_delete(cache_key: str | None) -> None:
    if cache_key is None:
        return
    _RUNTIME_CALL_CACHE.pop(cache_key, None)
    def _delete(payload: dict[str, str]) -> dict[str, str]:
        payload.pop(cache_key, None)
        return payload
    _update_runtime_cache_file(_delete)

def _runtime_cache_file() -> Path:
    return Path(tempfile.gettempdir()) / "agentcicd_runtime_call_cache.json"

def _read_runtime_cache_file() -> dict[str, str]:
    path = _runtime_cache_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}

def _write_runtime_cache_file(payload: dict[str, str]) -> None:
    path = _runtime_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)

def _update_runtime_cache_file(update_fn) -> None:
    import fcntl

    path = _runtime_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            payload = _read_runtime_cache_file()
            _write_runtime_cache_file(update_fn(payload))
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
