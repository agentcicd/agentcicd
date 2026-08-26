from __future__ import annotations

import os
import sys
import json
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.annotation_store import HttpAnnotationStore
from agentcicd.sql.engine.plan import ExecutionPlanStep, payload_to_dict
from agentcicd.sql.engine.publication_store import DriverArtifactPublicationStore, HttpPublicationStore
from agentcicd.sql.engine.runtime import ExecutionReport
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend, build_spark_session, default_backend_paths
from agentcicd.sql.engine.progress_reporter import ProgressReporter
from agentcicd.sql.engine.reusable_stages import reusable_table_names_from_env
from agentcicd.sql.ir.functions import RegisteredFunctionSpec, coerce_registered_function_specs
from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.runtime_context_loader import load_registered_runtime_functions

try:
    from minio import Minio
except Exception:  # pragma: no cover - optional outside execution extra
    Minio = None  # type: ignore[assignment]

try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside Spark image
    object_store_from_env = None  # type: ignore[assignment]


@dataclass
class EngineRunConfig:
    working_dir: str
    table_format: str = "parquet"
    enable_delta: bool = False
    include_cells: bool = True
    register_functions_from_context: bool = True
    progress_file: Optional[str] = None
    tables_root: Optional[str] = None
    checkpoints_root: Optional[str] = None
    input_values: Mapping[str, str] | None = None
    max_parallel_stages: int | None = None
    wait_for_annotations: bool = False
    annotation_poll_seconds: float = 1.0
    debug: bool | Mapping[str, object] | None = None
    registered_functions: Iterable[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]] | None = None


def _physical_object_bucket(logical_bucket: str) -> str:
    return logical_bucket.strip().lower().replace(".", "-")


def run_script_with_new_engine(
    script: str,
    config: EngineRunConfig,
) -> ExecutionReport:
    registered_functions = _load_registered_functions(config) if config.register_functions_from_context else []
    spark = build_spark_session(enable_delta=config.enable_delta)
    publication_store, annotation_store = _remote_stores_from_env()
    backend = SparkExecutionBackend(
        spark,
        working_dir=config.working_dir,
        table_format=config.table_format,
        paths=default_backend_paths(
            config.working_dir,
            tables_root=config.tables_root,
            checkpoints_root=config.checkpoints_root,
        ),
        publication_store=publication_store,
        annotation_store=annotation_store,
        debug=config.debug,
    )
    try:
        entrypoint = EngineEntrypoint(
            script,
            registered_functions=registered_functions,
            input_values=config.input_values or {},
            external_tables=reusable_table_names_from_env(),
        )
        plan = entrypoint.compile_plan(include_cells=config.include_cells)
        _write_plan_artifacts(config.working_dir, plan)
        reporter = ProgressReporter(Path(config.progress_file)) if config.progress_file else ProgressReporter(None)
        max_parallel_stages = max(1, int(config.max_parallel_stages or _int_env("AGENTCICD_MAX_PARALLEL_STAGES", 1)))
        if max_parallel_stages > 1:
            try:
                spark.conf.set("spark.scheduler.mode", "FAIR")
            except Exception:
                pass
        report = entrypoint.execute(
            backend,
            include_cells=config.include_cells,
            progress_callback=reporter.emit_event,
            max_parallel_stages=max_parallel_stages,
            wait_for_annotations=config.wait_for_annotations,
            annotation_poll_seconds=config.annotation_poll_seconds,
        )
        _write_execution_report(config.working_dir, report)
        return report
    finally:
        active_exception = sys.exc_info()[0]
        try:
            _archive_working_dir_to_object_storage(config.working_dir)
        except Exception as archive_exc:
            print(f"Failed to archive run artifacts to object storage: {archive_exc}", file=sys.stderr)
            if active_exception is None:
                raise
        try:
            spark.stop()
        except Exception:
            pass


def _completed_batch_tables_from_env() -> set[str]:
    return reusable_table_names_from_env()


def _load_registered_functions(config: EngineRunConfig) -> list[RegisteredFunctionSpec]:
    registered_functions: list[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]] = []
    run_dir = os.getenv("AGENTCICD_RUN_DIR", "").strip() or config.working_dir
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    try:
        functions = load_registered_runtime_functions(run_dir, run_object_uri)
    except Exception:
        functions = []
    registered_functions.extend(functions)
    registered_functions.extend(config.registered_functions or [])
    return coerce_registered_function_specs(registered_functions)


def _remote_stores_from_env():
    base_url = (
        os.getenv("AGENTCICD_DP_API_BASE_URL")
        or os.getenv("AGENTCICD_DP_INTERNAL_BASE_URL")
        or ""
    ).strip()
    if not base_url:
        return None, None
    token = os.getenv("AGENTCICD_CP_DP_INTERNAL_TOKEN", "").strip()
    headers = {"X-AgentCICD-Internal-Token": token} if token else {}
    remote_publication_store = HttpPublicationStore(base_url=base_url, headers=headers)
    return (
        DriverArtifactPublicationStore(remote_publication_store),
        HttpAnnotationStore(base_url=base_url, headers=headers),
    )


def _int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _write_execution_report(working_dir: str, report: ExecutionReport) -> None:
    logs_dir = Path(working_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_path = logs_dir / "engine_execution_report.json"
    payload = [
        {
            "step_kind": event.step_kind,
            "step_name": event.step_name,
            "status": event.status,
            "payload": _json_safe(event.payload),
        }
        for event in report.events
    ]
    report_path.write_text(
        json.dumps(
            {
                "events": payload,
                "failed_step_kind": report.failed_step_kind,
                "failed_step_name": report.failed_step_name,
                "error": _json_safe(report.error),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_plan_artifacts(working_dir: str, plan: list[ExecutionPlanStep]) -> None:
    logs_dir = Path(working_dir) / "logs"
    transpiled_dir = logs_dir / "transpiled"
    transpiled_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    for index, step in enumerate(plan):
        entry = {
            "index": index,
            "kind": step.kind,
            "name": step.name,
            "dependencies": list(step.dependencies),
            "payload_keys": sorted(payload_to_dict(step.payload).keys()),
        }
        manifest.append(entry)

        sql_text = payload_to_dict(step.payload).get("sql")
        if isinstance(sql_text, str) and sql_text.strip():
            artifact_name = f"{index:02d}_{step.kind}_{_sanitize_artifact_name(step.name)}.sql"
            (transpiled_dir / artifact_name).write_text(sql_text.strip() + "\n", encoding="utf-8")

    (logs_dir / "engine_plan.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _archive_working_dir_to_object_storage(working_dir: str) -> None:
    organization_id = os.getenv("AGENTCICD_ORGANIZATION_ID", "").strip()
    run_id = os.getenv("AGENTCICD_RUN_ID", "").strip()
    if not organization_id or not run_id or Minio is None:
        return

    attempt = int(os.getenv("AGENTCICD_RUN_ATTEMPT", "1") or "1")
    root = Path(working_dir)
    if not root.exists():
        return

    client = Minio(
        os.getenv("AGENTCICD_DP_MINIO_ENDPOINT", "minio.agentcicd-dp.svc.cluster.local:9000"),
        access_key=os.getenv("AGENTCICD_DP_MINIO_ACCESS_KEY", "agentcicd-minio"),
        secret_key=os.getenv("AGENTCICD_DP_MINIO_SECRET_KEY", "change_me_minio"),
        secure=os.getenv("AGENTCICD_DP_MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"},
    )
    physical_bucket = _physical_object_bucket(organization_id)
    if not client.bucket_exists(physical_bucket):
        client.make_bucket(physical_bucket)

    manifest = {
        "organization_id": organization_id,
        "run_id": run_id,
        "current_attempt": attempt,
        "latest_attempt": attempt,
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    for object_name in (
        f"runs/{run_id}/manifest.json",
        f"runs/{run_id}/attempt_{attempt}/manifest.json",
    ):
        client.put_object(
            physical_bucket,
            object_name,
            BytesIO(manifest_bytes),
            length=len(manifest_bytes),
            content_type="application/json",
        )

    roots_to_archive = ("tables", "logs", "outputs", "published", "published_datasets", "progress", "reports", "debug")
    for relative_root in roots_to_archive:
        directory = root / relative_root
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            relative_path = path.relative_to(root).as_posix()
            client.fput_object(
                physical_bucket,
                f"runs/{run_id}/attempt_{attempt}/{relative_path}",
                str(path),
            )


def _sanitize_artifact_name(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "_" for char in value.strip())
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "step"


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        payload = {key: _json_safe(item) for key, item in vars(value).items()}
        payload["__type__"] = value.__class__.__name__
        return payload
    return str(value)
