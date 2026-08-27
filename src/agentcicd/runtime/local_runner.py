from __future__ import annotations

import os
import signal
import sys
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentcicd.config import BackendName
from agentcicd.errors import BackendNotSupportedError
from agentcicd.project import LocalRunSpec, load_project
from agentcicd.reports import ReportSummary, render_local_report
from agentcicd.runtime.local_fixtures import build_fixture_runtime_plan, local_fixture_runtime
from agentcicd.sql.api import validate_recipe
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.plan import ExecutionPlanStep


@dataclass(frozen=True)
class RunResult:
    backend: BackendName
    run_dir: Path
    report_summary: ReportSummary | None = None


@dataclass(frozen=True)
class PreparedRun:
    spec: LocalRunSpec
    backend: BackendName
    run_dir: Path


@dataclass(frozen=True)
class TranspiledProject:
    spec: LocalRunSpec
    steps: tuple[ExecutionPlanStep, ...]


def validate_project(project_dir: str | Path) -> LocalRunSpec:
    spec = load_project(project_dir)
    fixture_plan = build_fixture_runtime_plan(spec)
    validate_recipe(spec.recipe_sql, registered_functions=fixture_plan.registered_functions)
    return spec


def transpile_project(project_dir: str | Path) -> TranspiledProject:
    spec = load_project(project_dir)
    with local_fixture_runtime(spec) as fixture_runtime:
        entrypoint = EngineEntrypoint(
            spec.recipe_sql,
            registered_functions=list(fixture_runtime.registered_functions),
            input_values=spec.inputs.input_values,
        )
        statements, registry = entrypoint.resolve_with_registry(apply_defaults=True)
        plan = entrypoint.compile_resolved_plan(
            statements,
            registry=registry,
            include_cells=spec.config.run.include_cells,
        )
    return TranspiledProject(spec=spec, steps=tuple(plan))


def run_project(project_dir: str | Path, *, backend: BackendName | None = None) -> RunResult:
    prepared = prepare_run(project_dir, backend=backend)
    return run_prepared(prepared)


def prepare_run(project_dir: str | Path, *, backend: BackendName | None = None) -> PreparedRun:
    spec = validate_project(project_dir)
    selected_backend = backend or spec.backend
    if selected_backend == BackendName.VALIDATE:
        return PreparedRun(spec=spec, backend=selected_backend, run_dir=spec.paths.run_root)
    if selected_backend == BackendName.DUCKDB:
        raise BackendNotSupportedError("Backend 'duckdb' is planned for v2 and is not supported in v1")
    if selected_backend != BackendName.SPARK:
        raise BackendNotSupportedError(f"Unsupported backend '{selected_backend.value}'")
    run_dir = _new_run_dir(spec.paths.run_root)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "progress").mkdir(parents=True, exist_ok=True)
    return PreparedRun(spec=spec, backend=selected_backend, run_dir=run_dir)


def run_prepared(prepared: PreparedRun) -> RunResult:
    if prepared.backend == BackendName.VALIDATE:
        return RunResult(backend=prepared.backend, run_dir=prepared.run_dir)
    if prepared.backend != BackendName.SPARK:
        raise BackendNotSupportedError(f"Unsupported backend '{prepared.backend.value}'")
    return _run_spark(prepared.spec, run_dir=prepared.run_dir)


def _run_spark(spec: LocalRunSpec, *, run_dir: Path) -> RunResult:
    from agentcicd.sql.api import run_recipe
    from agentcicd.sql.engine.runner import EngineRunConfig

    _configure_local_spark_python()
    _write_local_runtime_context(spec, run_dir=run_dir)
    progress_dir = run_dir / "progress"
    progress_file = progress_dir / "progress.jsonl"
    previous_sigint_handler = _current_sigint_handler()
    previous_env = _capture_env(("AGENTCICD_RUN_DIR", "AGENTCICD_FIXTURE_CONTEXT_PATH"))
    try:
        os.environ["AGENTCICD_RUN_DIR"] = str(run_dir)
        os.environ["AGENTCICD_FIXTURE_CONTEXT_PATH"] = str(run_dir / "fixtures" / "context.enriched.json")
        with local_fixture_runtime(spec) as fixture_runtime:
            config = EngineRunConfig(
                working_dir=str(run_dir),
                table_format=spec.config.run.table_format,
                include_cells=spec.config.run.include_cells,
                progress_file=str(progress_file),
                input_values=spec.inputs.input_values,
                max_parallel_stages=spec.config.run.max_parallel_stages,
                wait_for_annotations=True,
                debug=spec.config.debug.to_engine_debug(),
                registered_functions=fixture_runtime.registered_functions,
            )
            run_recipe(spec.recipe_sql, config, registered_functions=fixture_runtime.registered_functions)
    finally:
        _restore_env(previous_env)
        _restore_sigint_handler(previous_sigint_handler)
    return RunResult(
        backend=BackendName.SPARK,
        run_dir=run_dir,
        report_summary=render_local_report(run_dir, secret_values=tuple(record.value for record in spec.secrets)),
    )


def _new_run_dir(root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = root / f"run-{timestamp}"
    suffix = 1
    while candidate.exists():
        candidate = root / f"run-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def _configure_local_spark_python() -> None:
    """Run local Spark workers with the environment that installed AgentCICD."""
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)


def _write_local_runtime_context(spec: LocalRunSpec, *, run_dir: Path) -> Path:
    context_dir = run_dir / "fixtures"
    context_dir.mkdir(parents=True, exist_ok=True)
    context_path = context_dir / "context.enriched.json"
    secrets = [record.to_runtime_record() for record in spec.secrets]
    payload = {
        "secrets": secrets,
        "secret_ids": [record["id"] for record in secrets if isinstance(record.get("id"), str)],
    }
    context_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return context_path


def _current_sigint_handler() -> object | None:
    try:
        return signal.getsignal(signal.SIGINT)
    except ValueError:
        return None


def _restore_sigint_handler(handler: object | None) -> None:
    if handler is None:
        return
    try:
        signal.signal(signal.SIGINT, handler)
    except ValueError:
        return


def _capture_env(keys: tuple[str, ...]) -> dict[str, str | None]:
    return {key: os.environ[key] if key in os.environ else None for key in keys}


def _restore_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
