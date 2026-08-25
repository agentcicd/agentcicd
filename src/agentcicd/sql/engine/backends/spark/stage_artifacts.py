from __future__ import annotations

import hashlib
import json
import logging
import os
import resource
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import Any, Mapping

from agentcicd.sql.engine.backends.spark.common import F
from agentcicd.sql.engine.stage_manifest import StageManifest, completed_manifest_from_expected
from agentcicd.sql.ir.column_semantics import column_semantics_from_options
from agentcicd.sql.observability.sinks import ObjectStoreJsonlSink

try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside DP runtime images
    object_store_from_env = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class SparkStageArtifactsMixin:
    def _schema_sidecar_path(self, name: str) -> Path:
        return Path(self._paths.outputs_root) / "schemas" / f"{name}.json"

    def _write_schema_sidecar(
        self,
        name: str,
        schema: Any,
        *,
        table_path: str,
        kind: str,
        description: str | None = None,
        column_semantics: Mapping[str, Any] | None = None,
    ) -> None:
        if self._is_uri_path(str(self._paths.outputs_root)):
            return
        path = self._schema_sidecar_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "table": name,
            "kind": kind,
            "table_path": table_path,
            "wrapped_schema": _schema_json_value(schema),
            "value_schema": _value_schema_json_value(schema),
            "description": description,
            "column_semantics": dict(column_semantics or column_semantics_from_options({})),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_schema_sidecar(self, name: str):
        path = self._schema_sidecar_path(name)
        return _read_schema_sidecar_path(path)

    def _read_previous_schema_sidecar(self, name: str, previous_run_object_uri: str):
        current_run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
        for run_object_uri in (current_run_object_uri, previous_run_object_uri):
            object_schema = _read_schema_sidecar_from_object_store(name, run_object_uri)
            if object_schema is not None:
                return object_schema
        if self._is_uri_path(previous_run_object_uri):
            return None
        return _read_schema_sidecar_path(Path(previous_run_object_uri) / "outputs" / "schemas" / f"{name}.json")

    def _write_stage_manifest(
        self,
        name: str,
        stage_kind: str,
        *,
        sql: str | None,
        dataframe: Any,
        table_path: str,
        checkpoint_path: str | None = None,
        description: str | None = None,
        column_semantics: Mapping[str, Any] | None = None,
    ) -> None:
        expected = self._expected_stage_manifests.get(name.lower())
        if expected is None:
            from agentcicd.sql.engine.stage_manifest import StageManifest

            expected = StageManifest(
                stage_name=name,
                stage_kind=stage_kind,  # type: ignore[arg-type]
                fingerprint=_sha256_json(
                    {
                        "stage_name": name.lower(),
                        "stage_kind": stage_kind,
                        "sql": sql,
                    }
                ),
                source_sql_hash=_sha256(sql) if sql is not None else None,
                lowered_sql_hash=_sha256(sql) if sql is not None else None,
                description=description,
                column_semantics=dict(column_semantics or column_semantics_from_options({})),
            )
        materialized = self._read_table_path(table_path, schema=dataframe.schema)
        self._emit_memory_snapshot("stage_manifest_start", stage_name=name, stage_kind=stage_kind)
        error_summary = self._stage_error_summary(name, materialized)
        self._emit_memory_snapshot("stage_manifest_after_error_summary", stage_name=name, stage_kind=stage_kind)
        debug_artifacts = self._write_debug_row_streams(
            name,
            materialized,
            row_count=error_summary["row_count"],
            stage_kind=stage_kind,
        )
        self._emit_memory_snapshot("stage_manifest_after_debug_streams", stage_name=name, stage_kind=stage_kind)
        manifest = completed_manifest_from_expected(
            expected,
            table_format=self._table_format,
            output_path=table_path,
            checkpoint_path=checkpoint_path,
            output_schema_json=_schema_json_value(dataframe.schema),
            value_schema_json=_value_schema_json_value(dataframe.schema),
            row_count=error_summary["row_count"],
            row_error_count=error_summary["row_error_count"],
            cell_error_count=error_summary["cell_error_count"],
            errors_by_code=error_summary["errors_by_code"],
            errors_by_column=error_summary["errors_by_column"],
            sample_errors=error_summary["sample_errors"],
            attempt=_int_env("AGENTCICD_RUN_ATTEMPT"),
            description=description,
            column_semantics=dict(column_semantics or expected.column_semantics),
            debug_artifacts=debug_artifacts,
        )
        self._write_output_manifest(kind="stage", name=name, payload=manifest.to_dict())
        self._write_output_manifest(kind="stage_error_summary", name=name, payload=error_summary)
        metadata = {
            "reuse_state": "recomputed",
            "row_count": error_summary["row_count"],
            "row_error_count": error_summary["row_error_count"],
            "cell_error_count": error_summary["cell_error_count"],
            "cache_hits": manifest.cache_hits,
            "cache_misses": manifest.cache_misses,
            "cache_writes": manifest.cache_writes,
        }
        self._completion_metadata[(self._stage_kind_to_step_kind(stage_kind), name)] = metadata

    def _stage_error_summary(self, name: str, dataframe: Any) -> dict[str, Any]:
        row_count = int(dataframe.count())
        cell_columns = [
            field.name
            for field in getattr(dataframe.schema, "fields", [])
            if self._is_cell_struct_type(getattr(field, "dataType", None))
        ]
        if not cell_columns:
            return {
                "stage": name,
                "row_count": row_count,
                "row_error_count": 0,
                "cell_error_count": 0,
                "errors_by_code": {},
                "errors_by_column": {},
                "sample_errors": [],
            }

        cell_error_flags = [
            (F.size(F.col(column)["metadata"]["errors"]) > F.lit(0)).alias(column)
            for column in cell_columns
        ]
        flags = dataframe.select(*cell_error_flags)
        any_error_expr = reduce(
            lambda left, right: left | right,
            [F.col(column) for column in cell_columns],
        )
        row_error_count = int(
            flags.select(
                F.sum(
                    F.when(
                        any_error_expr,
                        F.lit(1),
                    ).otherwise(F.lit(0))
                ).alias("row_error_count")
            ).collect()[0]["row_error_count"]
            or 0
        )
        errors_by_column = {
            column: int(flags.select(F.sum(F.col(column).cast("int")).alias("count")).collect()[0]["count"] or 0)
            for column in cell_columns
        }
        cell_error_count = sum(errors_by_column.values())
        error_rows = []
        for column in cell_columns:
            error_rows.append(
                dataframe.select(
                    F.lit(column).alias("column"),
                    F.explode_outer(F.col(column)["metadata"]["errors"]).alias("error"),
                ).where(F.col("error").isNotNull())
            )
        errors = error_rows[0]
        for item in error_rows[1:]:
            errors = errors.unionByName(item)
        errors_by_code = {
            str(row["code"] or "UNKNOWN"): int(row["count"] or 0)
            for row in errors.select(F.col("error.code").alias("code")).groupBy("code").count().collect()
        }
        sample_errors = [
            {
                "column": str(row["column"]),
                "code": str(row["code"] or "UNKNOWN"),
                "message": str(row["message"] or ""),
            }
            for row in errors.select(
                "column",
                F.col("error.code").alias("code"),
                F.col("error.message").alias("message"),
            ).limit(10).collect()
        ]
        return {
            "stage": name,
            "row_count": row_count,
            "row_error_count": row_error_count,
            "cell_error_count": cell_error_count,
            "errors_by_code": errors_by_code,
            "errors_by_column": errors_by_column,
            "sample_errors": sample_errors,
        }

    def _emit_memory_snapshot(
        self,
        event: str,
        *,
        stage_name: str | None = None,
        stage_kind: str | None = None,
    ) -> None:
        payload = _drop_none(
            {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "event": event,
                "run_id": os.getenv("AGENTCICD_RUN_ID", ""),
                "attempt": os.getenv("AGENTCICD_RUN_ATTEMPT", ""),
                "stage_name": stage_name,
                "stage_kind": stage_kind,
                "memory": _driver_memory_snapshot(self._spark),
            }
        )
        logger.info("Driver memory snapshot %s", json.dumps(payload, separators=(",", ":"), sort_keys=True))
        _append_app_jsonl_event(payload)

    @staticmethod
    def _stage_kind_to_step_kind(stage_kind: str) -> str:
        if stage_kind == "batch":
            return "create_batch_table"
        if stage_kind == "stream":
            return "create_stream_table"
        return stage_kind

    def _read_previous_stage_manifest(self, name: str, previous_run_object_uri: str) -> StageManifest | None:
        env_manifest = _previous_manifest_from_env(name)
        if env_manifest is not None:
            return env_manifest
        object_manifest = _read_stage_manifest_from_object_store(name, previous_run_object_uri)
        if object_manifest is not None:
            return object_manifest
        if self._is_uri_path(previous_run_object_uri):
            return None
        manifest_path = Path(previous_run_object_uri) / "outputs" / f"stage_{name}.json"
        if not manifest_path.exists():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return StageManifest.from_dict(payload)

def _schema_json_value(schema: Any) -> Any:
    if hasattr(schema, "jsonValue"):
        try:
            return schema.jsonValue()
        except Exception:
            pass
    return {"type": "unknown", "repr": repr(schema)}

def _value_schema_json_value(schema: Any) -> Any:
    fields = getattr(schema, "fields", None)
    if not fields:
        return _schema_json_value(schema)
    value_fields = []
    for field in fields:
        data_type = getattr(field, "dataType", None)
        field_name = getattr(field, "name", "")
        value_type = data_type
        try:
            field_names = set(data_type.fieldNames()) if hasattr(data_type, "fieldNames") else set()
            if {"value", "metadata", "__agentcicd_cell"}.issubset(field_names):
                value_type = data_type["value"].dataType
        except Exception:
            value_type = data_type
        value_fields.append(
            {
                "name": field_name,
                "type": _schema_json_value(value_type),
                "nullable": bool(getattr(field, "nullable", True)),
            }
        )
    return {"type": "struct", "fields": value_fields}

def _read_schema_sidecar_path(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _schema_sidecar_payload_to_struct(payload)
    except Exception:
        return None

def _read_schema_sidecar_from_object_store(name: str, run_object_uri: str):
    if object_store_from_env is None or not run_object_uri.startswith("agentcicd-object://"):
        return None
    try:
        payload = object_store_from_env().get_json(
            f"{run_object_uri.rstrip('/')}/outputs/schemas/{name}.json"
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _schema_sidecar_payload_to_struct(payload)

def _read_stage_manifest_from_object_store(name: str, run_object_uri: str) -> StageManifest | None:
    if object_store_from_env is None or not run_object_uri.startswith("agentcicd-object://"):
        return None
    try:
        payload = object_store_from_env().get_json(
            f"{run_object_uri.rstrip('/')}/outputs/stage_{name}.json"
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return StageManifest.from_dict(payload)
    except Exception:
        return None

def _schema_sidecar_payload_to_struct(payload: dict[str, Any]):
    wrapped_schema = payload.get("wrapped_schema")
    if not isinstance(wrapped_schema, dict):
        return None
    try:
        from pyspark.sql.types import StructType

        return StructType.fromJson(wrapped_schema)
    except Exception:
        return None

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _sha256_json(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")))

def _int_env(name: str) -> int | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None

def _driver_memory_snapshot(spark: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "python_rss_bytes": _proc_status_bytes("VmRSS"),
        "python_peak_rss_bytes": _peak_rss_bytes(),
        "cgroup_memory_current_bytes": _read_cgroup_int("memory.current", "memory/memory.usage_in_bytes"),
        "cgroup_memory_limit_bytes": _read_cgroup_int("memory.max", "memory/memory.limit_in_bytes"),
    }
    try:
        runtime = spark._jvm.java.lang.Runtime.getRuntime()
        snapshot["jvm_heap_used_bytes"] = int(runtime.totalMemory()) - int(runtime.freeMemory())
        snapshot["jvm_heap_total_bytes"] = int(runtime.totalMemory())
        snapshot["jvm_heap_max_bytes"] = int(runtime.maxMemory())
    except Exception:
        pass
    return _drop_none(snapshot)

def _proc_status_bytes(field_name: str) -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if not line.startswith(f"{field_name}:"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) * 1024
    except Exception:
        return None
    return None

def _peak_rss_bytes() -> int | None:
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return None
    if value <= 0:
        return None
    # Linux reports KiB, macOS reports bytes. Spark runs on Linux in DP pods.
    return value * 1024 if value < 10_000_000 else value

def _read_cgroup_int(*relative_paths: str) -> int | None:
    for relative_path in relative_paths:
        path = Path("/sys/fs/cgroup") / relative_path
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not raw or raw == "max":
            return None
        try:
            return int(raw)
        except ValueError:
            continue
    return None

def _append_app_jsonl_event(payload: dict[str, Any]) -> None:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri or object_store_from_env is None:
        return
    app_log_uri = f"{run_object_uri.rstrip('/')}/logs/app.jsonl"
    try:
        ObjectStoreJsonlSink(object_store_from_env(), app_log_uri).emit(_drop_none(payload))
    except Exception:
        logger.debug("Failed to append app JSONL event", exc_info=True)

def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != "" and value != {}
    }

def _previous_manifest_from_env(name: str) -> StageManifest | None:
    raw_value = os.getenv("AGENTCICD_PREVIOUS_STAGE_MANIFESTS_JSON", "").strip()
    if not raw_value:
        return None
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    manifest_payload = payload.get(name) or payload.get(name.lower())
    if not isinstance(manifest_payload, dict):
        return None
    return StageManifest.from_dict(manifest_payload)
