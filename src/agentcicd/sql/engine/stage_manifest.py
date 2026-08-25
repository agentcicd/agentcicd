from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from agentcicd.sql.engine.plan import (
    ExecutionPlanStep,
    LoadTableStepPayload,
    RetrieveAnnotationStepPayload,
    SqlStepPayload,
    StreamTableStepPayload,
    plan_node_id,
    payload_to_dict,
)
from agentcicd.sql.ir.column_semantics import column_semantics_from_options, empty_column_semantics, normalize_column_semantics

ENGINE_VERSION = "local"


StageKind = Literal["load_table", "batch", "stream", "retrieve_annotation"]


@dataclass(frozen=True)
class StageManifest:
    stage_name: str
    stage_kind: StageKind
    fingerprint: str
    stage_node_id: str = ""
    source_sql_hash: str | None = None
    lowered_sql_hash: str | None = None
    input_stage_fingerprints: dict[str, str] = field(default_factory=dict)
    declared_inputs_and_options_hash: str | None = None
    source_dataset_or_file_fingerprints: dict[str, str] = field(default_factory=dict)
    runtime_function_versions: dict[str, str] = field(default_factory=dict)
    runtime_image_digest_or_version: str | None = None
    engine_version: str = ENGINE_VERSION
    wrapped_mode: bool = True
    value_schema_json: Any = None
    output_schema_json: Any = None
    input_schema_hashes: dict[str, str] = field(default_factory=dict)
    table_format: str | None = None
    output_path: str | None = None
    checkpoint_path: str | None = None
    row_count: int | None = None
    row_error_count: int = 0
    cell_error_count: int = 0
    errors_by_code: dict[str, int] = field(default_factory=dict)
    errors_by_column: dict[str, int] = field(default_factory=dict)
    sample_errors: list[dict[str, Any]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    cache_writes: int = 0
    description: str | None = None
    column_semantics: dict[str, Any] = field(default_factory=empty_column_semantics)
    debug_artifacts: dict[str, Any] = field(default_factory=dict)
    status: str = "planned"
    attempt: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "stage_node_id": self.stage_node_id,
            "stage_kind": self.stage_kind,
            "fingerprint": self.fingerprint,
            "source_sql_hash": self.source_sql_hash,
            "lowered_sql_hash": self.lowered_sql_hash,
            "input_table_hashes": {},
            "upstream_stage_fingerprints": self.input_stage_fingerprints,
            "input_schema_hashes": self.input_schema_hashes,
            "runtime_function_versions": self.runtime_function_versions,
            "runtime_image_digest_or_version": self.runtime_image_digest_or_version,
            "engine_version": self.engine_version,
            "wrapped_mode": self.wrapped_mode,
            "value_schema_json": self.value_schema_json,
            "output_schema_json": self.output_schema_json,
            "declared_inputs_and_options_hash": self.declared_inputs_and_options_hash,
            "source_dataset_or_file_fingerprints": self.source_dataset_or_file_fingerprints,
            "execution_options_hash": self.declared_inputs_and_options_hash,
            "table_format": self.table_format,
            "output_path": self.output_path,
            "checkpoint_path": self.checkpoint_path,
            "row_count": self.row_count,
            "row_error_count": self.row_error_count,
            "cell_error_count": self.cell_error_count,
            "errors_by_code": self.errors_by_code,
            "errors_by_column": self.errors_by_column,
            "sample_errors": self.sample_errors,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_writes": self.cache_writes,
            "description": self.description,
            "column_semantics": self.column_semantics,
            "debug_artifacts": self.debug_artifacts,
            "status": self.status,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageManifest":
        return cls(
            stage_node_id=str(payload.get("stage_node_id") or ""),
            stage_name=str(payload.get("stage_name") or ""),
            stage_kind=str(payload.get("stage_kind") or "batch"),  # type: ignore[arg-type]
            fingerprint=str(payload.get("fingerprint") or ""),
            source_sql_hash=_optional_str(payload.get("source_sql_hash")),
            lowered_sql_hash=_optional_str(payload.get("lowered_sql_hash")),
            input_stage_fingerprints={
                str(key): str(value)
                for key, value in dict(payload.get("upstream_stage_fingerprints") or {}).items()
            },
            declared_inputs_and_options_hash=_optional_str(payload.get("declared_inputs_and_options_hash")),
            source_dataset_or_file_fingerprints={
                str(key): str(value)
                for key, value in dict(payload.get("source_dataset_or_file_fingerprints") or {}).items()
            },
            runtime_function_versions={
                str(key): str(value)
                for key, value in dict(payload.get("runtime_function_versions") or {}).items()
            },
            runtime_image_digest_or_version=_optional_str(payload.get("runtime_image_digest_or_version")),
            engine_version=str(payload.get("engine_version") or ENGINE_VERSION),
            wrapped_mode=bool(payload.get("wrapped_mode", True)),
            value_schema_json=payload.get("value_schema_json"),
            output_schema_json=payload.get("output_schema_json"),
            input_schema_hashes={str(key): str(value) for key, value in dict(payload.get("input_schema_hashes") or {}).items()},
            table_format=_optional_str(payload.get("table_format")),
            output_path=_optional_str(payload.get("output_path")),
            checkpoint_path=_optional_str(payload.get("checkpoint_path")),
            row_count=payload.get("row_count") if isinstance(payload.get("row_count"), int) else None,
            row_error_count=int(payload.get("row_error_count") or 0),
            cell_error_count=int(payload.get("cell_error_count") or 0),
            errors_by_code={
                str(key): int(value)
                for key, value in dict(payload.get("errors_by_code") or {}).items()
                if isinstance(value, int)
            },
            errors_by_column={
                str(key): int(value)
                for key, value in dict(payload.get("errors_by_column") or {}).items()
                if isinstance(value, int)
            },
            sample_errors=[
                dict(item)
                for item in list(payload.get("sample_errors") or [])
                if isinstance(item, Mapping)
            ],
            cache_hits=int(payload.get("cache_hits") or 0),
            cache_misses=int(payload.get("cache_misses") or 0),
            cache_writes=int(payload.get("cache_writes") or 0),
            description=_optional_str(payload.get("description")),
            column_semantics=_safe_column_semantics(payload.get("column_semantics")),
            debug_artifacts=dict(payload.get("debug_artifacts") or {}),
            status=str(payload.get("status") or "planned"),
            attempt=payload.get("attempt") if isinstance(payload.get("attempt"), int) else None,
        )


def build_expected_stage_manifests(plan: list[ExecutionPlanStep]) -> dict[str, StageManifest]:
    manifests: dict[str, StageManifest] = {}
    for step in plan:
        stage_kind = _stage_kind_for_step(step)
        if stage_kind is None:
            continue
        input_fingerprints = {
            dependency.split(":", 1)[1]: manifests[dependency.split(":", 1)[1]].fingerprint
            for dependency in step.dependencies
            if dependency.startswith("table:") and dependency.split(":", 1)[1] in manifests
        }
        payload = payload_to_dict(step.payload)
        source_sql = _step_sql(step)
        source_path = _step_source_path(step)
        fingerprint_payload = {
            "stage_name": step.name.lower(),
            "stage_kind": stage_kind,
            "payload": _stable_json_value(payload),
            "dependencies": sorted(step.dependencies),
            "upstream_stage_fingerprints": input_fingerprints,
            "engine_version": ENGINE_VERSION,
            "wrapped_mode": True,
        }
        manifests[step.name.lower()] = StageManifest(
            stage_node_id=plan_node_id(step),
            stage_name=step.name,
            stage_kind=stage_kind,
            fingerprint=_hash_json(fingerprint_payload),
            source_sql_hash=_sha256(source_sql) if source_sql is not None else None,
            lowered_sql_hash=_sha256(source_sql) if source_sql is not None else None,
            input_stage_fingerprints=input_fingerprints,
            declared_inputs_and_options_hash=_sha256_json(payload),
            source_dataset_or_file_fingerprints={step.name: _sha256(source_path)} if source_path else {},
            description=description_from_options(payload.get("options")),
            column_semantics=column_semantics_from_options(payload.get("options")),
        )
    return manifests


def completed_manifest_from_expected(
    expected: StageManifest,
    *,
    table_format: str,
    output_path: str,
    output_schema_json: Any,
    value_schema_json: Any,
    checkpoint_path: str | None = None,
    row_count: int | None = None,
    row_error_count: int = 0,
    cell_error_count: int = 0,
    errors_by_code: dict[str, int] | None = None,
    errors_by_column: dict[str, int] | None = None,
    sample_errors: list[dict[str, Any]] | None = None,
    cache_hits: int = 0,
    cache_misses: int = 0,
    cache_writes: int = 0,
    attempt: int | None = None,
    description: str | None = None,
    column_semantics: dict[str, Any] | None = None,
    debug_artifacts: dict[str, Any] | None = None,
) -> StageManifest:
    return StageManifest(
        **{
            **expected.__dict__,
            "table_format": table_format,
            "output_path": output_path,
            "checkpoint_path": checkpoint_path,
            "row_count": row_count,
            "row_error_count": row_error_count,
            "cell_error_count": cell_error_count,
            "errors_by_code": errors_by_code or {},
            "errors_by_column": errors_by_column or {},
            "sample_errors": sample_errors or [],
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_writes": cache_writes,
            "attempt": attempt,
            "description": description if description is not None else expected.description,
            "column_semantics": column_semantics or expected.column_semantics,
            "debug_artifacts": debug_artifacts or {},
            "output_schema_json": output_schema_json,
            "value_schema_json": value_schema_json,
            "status": "completed",
        }
    )


def manifest_matches_expected(previous: StageManifest, expected: StageManifest) -> bool:
    return (
        previous.status == "completed"
        and previous.fingerprint == expected.fingerprint
        and previous.wrapped_mode == expected.wrapped_mode
        and previous.row_error_count == 0
        and previous.cell_error_count == 0
    )


def _stage_kind_for_step(step: ExecutionPlanStep) -> StageKind | None:
    if step.kind == "load_table":
        return "load_table"
    if step.kind == "create_batch_table":
        return "batch"
    if step.kind == "create_stream_table":
        return "stream"
    if step.kind == "retrieve_annotation":
        return "retrieve_annotation"
    return None


def _step_sql(step: ExecutionPlanStep) -> str | None:
    if isinstance(step.payload, (SqlStepPayload, StreamTableStepPayload)):
        return step.payload.sql
    return None


def _step_source_path(step: ExecutionPlanStep) -> str | None:
    if isinstance(step.payload, LoadTableStepPayload):
        return step.payload.path
    if isinstance(step.payload, RetrieveAnnotationStepPayload):
        return step.payload.source_ref
    return None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _hash_json(value)


def _hash_json(value: Any) -> str:
    return _sha256(json.dumps(_stable_json_value(value), sort_keys=True, separators=(",", ":")))


def _stable_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _stable_json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return [_stable_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return _stable_json_value(value.to_dict())
    if hasattr(value, "__dict__"):
        return _stable_json_value(vars(value))
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def description_from_options(options: Any) -> str | None:
    if not isinstance(options, Mapping):
        return None
    raw = options.get("description")
    if raw is None:
        raw = options.get("DESCRIPTION")
    return _optional_str(raw)


def _safe_column_semantics(value: Any) -> dict[str, Any]:
    try:
        return normalize_column_semantics(value)
    except ValueError:
        return empty_column_semantics()
