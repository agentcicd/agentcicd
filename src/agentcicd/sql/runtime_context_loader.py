from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from agentcicd.sql.contracts import ExecutionContext, RegisteredRuntimeFunction

try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside Spark image
    object_store_from_env = None  # type: ignore[assignment]


def load_execution_context(run_dir: str | Path, run_object_uri: str = "") -> ExecutionContext:
    payloads = _load_context_payloads(run_dir, run_object_uri)
    fixtures: dict[str, dict[str, object]] = {}
    for payload in payloads:
        raw_fixtures = payload.get("fixtures")
        if not isinstance(raw_fixtures, list):
            continue
        for item in raw_fixtures:
            if not isinstance(item, Mapping):
                continue
            normalized = {str(key): value for key, value in item.items()}
            key = _fixture_key(normalized)
            if not key:
                continue
            fixtures[key] = _merge_fixture_payloads(fixtures.get(key, {}), normalized)
    return ExecutionContext.from_mapping({"fixtures": list(fixtures.values())})


def load_registered_runtime_functions(
    run_dir: str | Path,
    run_object_uri: str = "",
) -> list[RegisteredRuntimeFunction]:
    return list(load_execution_context(run_dir, run_object_uri).fixtures)


def _load_context_payloads(run_dir: str | Path, run_object_uri: str) -> list[dict[str, object]]:
    if run_object_uri and object_store_from_env is not None:
        return _load_object_context_payloads(run_object_uri)
    return _load_local_context_payloads(Path(run_dir))


def _load_object_context_payloads(run_object_uri: str) -> list[dict[str, object]]:
    store = object_store_from_env()
    payloads: list[dict[str, object]] = []
    for name in ("context.enriched.json", "context.raw.json"):
        try:
            payload = store.get_json(f"{run_object_uri.rstrip('/')}/fixture-context/{name}")
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _load_local_context_payloads(run_dir: Path) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for directory in ("fixture-definitions", "fixtures"):
        for name in ("context.enriched.json", "context.raw.json"):
            context_path = run_dir / directory / name
            if not context_path.exists():
                continue
            try:
                payload = json.loads(context_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


def _fixture_key(payload: Mapping[str, object]) -> str:
    for field in ("call_name", "name", "id", "runtime_alias"):
        value = str(payload.get(field) or "").strip().lower()
        if value:
            return value
    return ""


def _merge_fixture_payloads(left: Mapping[str, object], right: Mapping[str, object]) -> dict[str, object]:
    merged = dict(left)
    for key, value in right.items():
        if _has_value(value):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True
