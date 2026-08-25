from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentcicd.config import BackendName
from agentcicd.errors import BackendNotSupportedError
from agentcicd.project import LocalRunSpec, load_project
from agentcicd.reports import ReportSummary, render_local_report
from agentcicd.runtime.local_fixtures import build_fixture_runtime_plan, local_fixture_runtime
from agentcicd.sql.api import validate_recipe


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


def validate_project(project_dir: str | Path) -> LocalRunSpec:
    spec = load_project(project_dir)
    fixture_plan = build_fixture_runtime_plan(spec)
    validate_recipe(spec.recipe_sql, registered_functions=fixture_plan.registered_functions)
    return spec


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
    progress_dir = run_dir / "progress"
    progress_file = progress_dir / "progress.jsonl"
    with local_fixture_runtime(spec) as fixture_runtime:
        config = EngineRunConfig(
            working_dir=str(run_dir),
            table_format=spec.config.run.table_format,
            include_cells=spec.config.run.include_cells,
            progress_file=str(progress_file),
            input_values=spec.inputs.input_values,
            max_parallel_stages=spec.config.run.max_parallel_stages,
            debug=spec.config.debug.to_engine_debug(),
            registered_functions=fixture_runtime.registered_functions,
        )
        run_recipe(spec.recipe_sql, config, registered_functions=fixture_runtime.registered_functions)
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
