from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentcicd.inspection.models import (
    InspectionCapabilities,
    InspectionProject,
    InspectionResource,
    InspectionRun,
    envelope,
    record,
)
from agentcicd.inspection.local_annotations import LocalAnnotationApiMixin
from agentcicd.inspection.local_common import MAX_PAGE_SIZE, SAFE_ARTIFACT_SUFFIXES, LocalRunReference
from agentcicd.inspection.local_recipe_graph import LocalRecipeGraphMixin
from agentcicd.inspection.local_runtime_controls import LocalRuntimeControlsMixin
from agentcicd.project import load_project
from agentcicd.sql.observability.redaction import redacted_preview




class LocalInspectionStore(LocalAnnotationApiMixin, LocalRecipeGraphMixin, LocalRuntimeControlsMixin):
    """Read-only, redaction-aware view over one folder-project and its run artifacts."""

    def __init__(self, project_dir: str | Path) -> None:
        self._spec = load_project(project_dir)
        digest = hashlib.sha256(self._spec.paths.root.as_posix().encode("utf-8")).hexdigest()[:16]
        self._project_id = f"local-{digest}"
        self._secret_values = tuple(secret.value for secret in self._spec.secrets if secret.value)

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def project_root(self) -> Path:
        return self._spec.paths.root

    def project(self) -> dict[str, Any]:
        project = InspectionProject(
            id=self.project_id,
            name=self._spec.paths.root.name,
            source="local",
            root_label=self._spec.paths.root.name,
        )
        return envelope(
            {
                "project": record(project),
                "resources": {
                    "recipes": [record(self._recipe_resource())],
                    "fixtures": [record(item) for item in self._fixture_resources()],
                    "inputs": self._input_resources(),
                    "secrets": self._secret_resources(),
                    "runs": [record(item) for item in self._run_resources()],
                },
                "capabilities": record(InspectionCapabilities(rerun=True)),
            }
        )

    def recipes(self) -> dict[str, Any]:
        return envelope({"items": [record(self._recipe_resource())]})

    def recipe(self, recipe_id: str) -> dict[str, Any]:
        resource = self._recipe_resource()
        if recipe_id != resource.id:
            raise KeyError(recipe_id)
        return envelope(
            {
                "recipe": {
                    **record(resource),
                    "source_text": self._spec.recipe_sql,
                    "path": self._spec.paths.recipe_sql.name,
                }
            }
        )

    def fixtures(self) -> dict[str, Any]:
        return envelope({"items": [record(item) for item in self._fixture_resources()]})

    def fixture(self, fixture_id: str) -> dict[str, Any]:
        for source in self._spec.fixture_sources:
            resource = self._fixture_resource(source)
            if resource.id == fixture_id:
                return envelope(
                    {
                        "fixture": {
                            **record(resource),
                    "source_text": self._redact(source.read_text(encoding="utf-8")),
                            "path": source.relative_to(self._spec.paths.root).as_posix(),
                        }
                    }
                )
        raise KeyError(fixture_id)

    def inputs(self) -> dict[str, Any]:
        return envelope({"items": self._input_resources()})

    def secrets(self) -> dict[str, Any]:
        return envelope({"items": self._secret_resources()})

    def runs(self) -> dict[str, Any]:
        return envelope({"items": [record(item) for item in self._run_resources()]})

    def run_summary(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        progress = self._progress_payload(reference)
        report = self._report_payload(reference)
        execution = self._execution_summary(progress)
        status = self._run_status(progress, reference.path)
        run = InspectionRun(
            id=reference.run_id,
            status=status,
            started_at=self._first_started_at(progress),
            finished_at=self._last_finished_at(progress),
            attempt=1,
            source="local",
        )
        return envelope(
            {
                "run": record(run),
                "report_summary": {
                    "metrics_count": len(report["metrics"]),
                    "issues_count": len(report["issues"]),
                    "charts_count": len(report["charts"]),
                },
                "execution_summary": execution,
                "capabilities": record(InspectionCapabilities(rerun=True, cancel=status == "running")),
                "project_id": self.project_id,
                "project_name": self._spec.paths.root.name,
                "links": {"self": f"/inspection/v1/runs/{reference.run_id}"},
            }
        )

    def progress(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        steps = self._progress_payload(reference)
        return envelope(
            {
                "run_id": reference.run_id,
                "steps": steps,
                "total_steps": len(steps),
                "completed_steps": sum(item.get("status") == "completed" for item in steps),
                "failed_steps": sum(item.get("status") == "failed" for item in steps),
                "running_steps": sum(item.get("status") == "running" for item in steps),
                "pending_steps": 0,
            }
        )

    def public_runs(self) -> list[dict[str, Any]]:
        return [self.public_run(item.run_id) for item in self._runs()]

    def public_run(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        progress = self._progress_payload(reference)
        submitted_at = self._first_started_at(progress) or self._mtime_iso(reference.path)
        return {
            "id": reference.run_id,
            "recipe_id": self._recipe_resource().id,
            "recipe_version": 1,
            "cluster_id": None,
            "status": self._run_status(progress, reference.path),
            "submitted_at": submitted_at,
            "started_at": self._first_started_at(progress),
            "finished_at": self._last_finished_at(progress),
            "log_path": (reference.path / "logs").as_posix(),
            "error": self._first_error(progress),
            "payload": {
                "source": "local",
                "project_id": self.project_id,
                "project_root": self._spec.paths.root.as_posix(),
                "sql_text": self._spec.recipe_sql,
            },
            "aisystem_environment_bindings": {},
            "target_aisystem_refs": [],
            "resource_refs": [],
        }

    def public_progress(self, run_id: str) -> dict[str, Any]:
        return {key: value for key, value in self.progress(run_id).items() if key != "schema_version"}

    def public_recipes(self) -> dict[str, Any]:
        return {"items": [self.public_recipe(self._recipe_resource().id)], "total": 1}

    def public_recipe(self, recipe_id: str) -> dict[str, Any]:
        recipe = self._recipe_resource()
        if recipe_id != recipe.id:
            raise KeyError(recipe_id)
        return {
            "id": recipe.id,
            "name": recipe.name,
            "description": None,
            "source_text": self._spec.recipe_sql,
            "version": 1,
            "status": recipe.status,
            "organization_id": self.project_id,
            "analysis": self.recipe_analysis({"source_text": self._spec.recipe_sql}),
        }

    def recipe_segments(self, recipe_id: str) -> dict[str, Any]:
        if recipe_id != self._recipe_resource().id:
            raise KeyError(recipe_id)
        return self._recipe_analysis_payload(self._spec.recipe_sql, segmentation_key="segments")

    def recipe_analysis(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_text = str(payload.get("source_text") or self._spec.recipe_sql)
        if not source_text.strip():
            raise ValueError("source_text must not be empty")
        return self._recipe_analysis_payload(source_text, segmentation_key="analysis")

    def logs(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        logs_dir = reference.path / "logs"
        debug_dir = reference.path / "debug"
        files: list[dict[str, Any]] = []
        text_parts: list[str] = []
        log_files = self._safe_log_artifact_files(reference, (logs_dir, debug_dir))
        for path in log_files:
            if not path.is_file() or path.suffix.lower() not in SAFE_ARTIFACT_SUFFIXES:
                continue
            relative = path.relative_to(reference.path).as_posix()
            files.append(
                {
                    "name": path.name,
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                }
            )
        preferred = [
            logs_dir / "run.log",
            logs_dir / "debug.log",
            logs_dir / "app.log",
            logs_dir / "app.jsonl",
        ]
        text_files = self._ordered_log_text_files(reference, preferred, log_files)
        for path in text_files:
            relative = path.relative_to(reference.path).as_posix()
            text_parts.append(f"== {relative} ==\n{self._replace_secret_values(path.read_text(encoding='utf-8'))}")
        return envelope({"run_id": reference.run_id, "files": files, "text": "\n\n".join(text_parts)})

    def _safe_log_artifact_files(self, reference: LocalRunReference, roots: Iterable[Path]) -> list[Path]:
        files: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".log", ".jsonl", ".txt"} and self._is_relative_to(path, reference.path):
                    files.append(path)
        return files

    def _ordered_log_text_files(self, reference: LocalRunReference, preferred: Iterable[Path], files: Iterable[Path]) -> list[Path]:
        ordered: list[Path] = []
        seen: set[Path] = set()
        for path in [*preferred, *files]:
            if not path.is_file() or path.suffix.lower() not in SAFE_ARTIFACT_SUFFIXES:
                continue
            if not self._is_relative_to(path, reference.path):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            ordered.append(path)
        return ordered

    def graph(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        nodes, edges = self._recipe_graph()
        progress_by_name = {
            str(step.get("step_name") or "").lower(): step
            for step in self._progress_payload(reference)
        }
        graph_nodes = []
        for node in nodes:
            progress = progress_by_name.get(node.label.lower())
            graph_nodes.append(
                {
                    "id": node.id,
                    "type": node.type,
                    "label": node.label,
                    "status": "available" if node.type == "secret" else str(progress.get("status")) if progress else "pending",
                    "details": {
                        "row_count": progress.get("row_count") if progress else None,
                        "row_error_count": progress.get("row_error_count") if progress else None,
                        "cell_error_count": progress.get("cell_error_count") if progress else None,
                        "error": progress.get("error") if progress else None,
                    },
                }
            )
        return envelope(
            {
                "run_id": reference.run_id,
                "nodes": self._redact(graph_nodes),
                "edges": [
                    {"from_id": edge.from_id, "to_id": edge.to_id, "relation": edge.relation}
                    for edge in edges
                ],
            }
        )

    def report(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        payload = self._report_payload(reference)
        return envelope({"run_id": reference.run_id, **payload})

    def tables(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        tables_dir = reference.path / "tables"
        items: list[dict[str, Any]] = []
        if tables_dir.is_dir():
            for table_dir in sorted(path for path in tables_dir.iterdir() if path.is_dir()):
                summary = self._read_json(reference.path / "outputs" / f"stage_{table_dir.name}.json")
                items.append(
                    {
                        "name": table_dir.name,
                        "status": "available",
                        "row_count": self._number(summary, "row_count"),
                        "row_error_count": self._number(summary, "row_error_count"),
                        "cell_error_count": self._number(summary, "cell_error_count"),
                        "reuse_state": self._string(summary, "reuse_state"),
                    }
                )
        return envelope({"run_id": reference.run_id, "items": items})

    def table_schema(self, run_id: str, table_name: str) -> dict[str, Any]:
        reference = self._run(run_id)
        table_dir = self._table_dir(reference, table_name)
        schema = self._read_json(reference.path / "outputs" / "schemas" / f"{table_name}.json")
        columns = schema.get("fields") if isinstance(schema.get("fields"), list) else schema.get("columns", [])
        if not isinstance(columns, list):
            columns = []
        return envelope(
            {
                "run_id": reference.run_id,
                "table_name": table_dir.name,
                "columns": self._redact(columns),
                "format": "parquet",
            }
        )

    def table_rows(self, run_id: str, table_name: str, *, page: int, page_size: int) -> dict[str, Any]:
        reference = self._run(run_id)
        table_dir = self._table_dir(reference, table_name)
        normalized_page = max(1, page)
        normalized_size = min(max(1, page_size), MAX_PAGE_SIZE)
        rows = self._read_parquet_rows(table_dir, offset=(normalized_page - 1) * normalized_size, limit=normalized_size + 1)
        has_more = len(rows) > normalized_size
        returned_rows = [self._present_output_row(row) for row in rows[:normalized_size]]
        columns = sorted({key for row in returned_rows for key in row})
        return envelope(
            {
                "run_id": reference.run_id,
                "table_name": table_dir.name,
                "columns": columns,
                "rows": self._redact(returned_rows),
                "page": normalized_page,
                "page_size": normalized_size,
                "returned": len(returned_rows),
                "total_rows": None,
                "has_more": has_more,
                "format": "parquet",
            }
        )

    @staticmethod
    def _present_output_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in row.items()
            if key != "__agentcicd_row_id"
        }

    def traces(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        traces_dir = reference.path / "traces"
        items: list[dict[str, Any]] = []
        if traces_dir.is_dir():
            for path in sorted(traces_dir.iterdir()):
                if path.is_dir():
                    items.append({"id": path.name, "status": "available"})
        return envelope({"run_id": reference.run_id, "items": items})

    def trace(self, run_id: str, trace_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        trace_dir = self._safe_child(reference.path / "traces", trace_id)
        if not trace_dir.is_dir():
            raise KeyError(trace_id)
        summary = self._read_json(trace_dir / "summary.json")
        return envelope({"run_id": reference.run_id, "trace_id": trace_id, "summary": self._redact(summary)})

    def trace_spans(self, run_id: str, trace_id: str, *, page: int, page_size: int) -> dict[str, Any]:
        reference = self._run(run_id)
        trace_dir = self._safe_child(reference.path / "traces", trace_id)
        spans_path = trace_dir / "spans.jsonl"
        records = self._read_jsonl(spans_path)
        normalized_page = max(1, page)
        normalized_size = min(max(1, page_size), MAX_PAGE_SIZE)
        start = (normalized_page - 1) * normalized_size
        selected = records[start : start + normalized_size]
        return envelope(
            {
                "run_id": reference.run_id,
                "trace_id": trace_id,
                "records": self._redact(selected),
                "page": normalized_page,
                "page_size": normalized_size,
                "returned": len(selected),
                "total_records": len(records),
                "has_more": start + len(selected) < len(records),
            }
        )

    def artifact(self, run_id: str, artifact_name: str) -> tuple[str, bytes]:
        reference = self._run(run_id)
        artifact_path = self._safe_child(reference.path, artifact_name)
        if not artifact_path.is_file() or artifact_path.suffix.lower() not in SAFE_ARTIFACT_SUFFIXES:
            raise KeyError(artifact_name)
        return self._content_type(artifact_path), self._redacted_file_bytes(artifact_path)


    @staticmethod
    def _safe_ref(value: str) -> str:
        return str(value or "").strip().replace("/", "_")

    @staticmethod
    def _slug(value: str) -> str:
        normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
        return "_".join(part for part in normalized.split("_") if part) or "default"

    @staticmethod
    def _timestamp(path: Path) -> str | None:
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _recipe_resource(self) -> InspectionResource:
        return InspectionResource(id="recipe.sql", name="recipe.sql", status="available")

    def _fixture_resources(self) -> tuple[InspectionResource, ...]:
        used_names = self._used_fixture_names()
        return tuple(
            self._fixture_resource(path)
            for path in self._spec.fixture_sources
            if used_names.intersection(self._fixture_function_names(path))
        )

    def _fixture_resource(self, source: Path) -> InspectionResource:
        relative_path = source.relative_to(self._spec.paths.root).as_posix()
        return InspectionResource(
            id=relative_path,
            name=source.stem.removeprefix("fixture_") or source.stem,
            status="available",
            details={
                "path": relative_path,
                "functions": sorted(self._fixture_function_names(source)),
            },
        )

    def _input_resources(self) -> list[dict[str, Any]]:
        values = self._spec.inputs.input_values
        resources: list[dict[str, Any]] = []
        for name, source in sorted(self._spec.inputs.input_sources.items()):
            value = values.get(name)
            declared_type = source.declared_type.upper()
            sensitive = declared_type == "SECRET" or self._looks_sensitive(name)
            resources.append(
                {
                    "name": name,
                    "type": declared_type,
                    "source": source.source,
                    "value_preview": "[redacted]" if sensitive else self._preview(value),
                    "redacted": sensitive,
                }
            )
        return resources

    def _secret_resources(self) -> list[dict[str, Any]]:
        used_secret_references = self._used_secret_references()
        return [
            {
                "reference": record.id,
                "type": record.secret_type,
                "configured": True,
                "description": record.description,
            }
            for record in self._spec.secrets
            if record.id in used_secret_references
        ]

    def _run_resources(self) -> tuple[InspectionRun, ...]:
        items: list[InspectionRun] = []
        for reference in self._runs():
            progress = self._progress_payload(reference)
            items.append(
                InspectionRun(
                    id=reference.run_id,
                    status=self._run_status(progress, reference.path),
                    started_at=self._first_started_at(progress),
                    finished_at=self._last_finished_at(progress),
                    attempt=1,
                    source="local",
                )
            )
        return tuple(items)

    def _runs(self) -> tuple[LocalRunReference, ...]:
        root = self._spec.paths.run_root
        if not root.is_dir():
            return ()
        return tuple(
            LocalRunReference(run_id=path.name, path=path)
            for path in sorted(root.glob("run-*"), reverse=True)
            if path.is_dir()
        )

    def _run(self, run_id: str) -> LocalRunReference:
        for reference in self._runs():
            if reference.run_id == run_id:
                return reference
        raise KeyError(run_id)

    def _progress_payload(self, reference: LocalRunReference) -> list[dict[str, Any]]:
        events = self._read_jsonl(reference.path / "progress" / "progress.jsonl")
        indexed: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            step_name = str(event.get("step_name") or "unknown")
            step_type = str(event.get("step_type") or "unknown")
            key = (step_type, step_name)
            current = indexed.setdefault(
                key,
                {
                    "id": f"{step_type}:{step_name}",
                    "step_type": step_type,
                    "step_name": step_name,
                    "status": "pending",
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "error_type": None,
                    "error_traceback": None,
                },
            )
            current["status"] = str(event.get("status") or current["status"])
            if event.get("started_at"):
                current["started_at"] = event["started_at"]
            if event.get("finished_at"):
                current["finished_at"] = event["finished_at"]
            for field_name in ("error", "error_type", "error_traceback", "row_count", "row_error_count", "cell_error_count", "reuse_state", "cache_hits", "cache_misses", "cache_writes"):
                if field_name in event:
                    current[field_name] = event[field_name]
            timestamp = str(current.get("started_at") or current.get("finished_at") or datetime.now(timezone.utc).isoformat())
            current.setdefault("created_at", timestamp)
            current["updated_at"] = str(current.get("finished_at") or timestamp)
        return [self._redact(item) for item in indexed.values()]

    def _report_payload(self, reference: LocalRunReference) -> dict[str, Any]:
        reports_dir = reference.path / "reports"
        return {
            "metrics": self._redact(self._present_report_values(self._read_json_list(reports_dir / "metrics.json"))),
            "issues": self._redact(self._present_report_values(self._read_json_list(reports_dir / "issues.json"))),
            "charts": self._redact(self._present_report_values(self._read_json_list(reports_dir / "charts.json"))),
            "layout_json": {},
        }

    def _present_report_values(self, value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("__agentcicd_cell") is True and "value" in value:
                return self._present_report_values(value["value"])
            return {key: self._present_report_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._present_report_values(item) for item in value]
        return value

    def _execution_summary(self, steps: Iterable[dict[str, Any]]) -> dict[str, int]:
        values = list(steps)
        return {
            "stage_count": len(values),
            "completed_stage_count": sum(item.get("status") == "completed" for item in values),
            "failed_stage_count": sum(item.get("status") == "failed" for item in values),
            "row_error_count": sum(self._number(item, "row_error_count") or 0 for item in values),
            "cell_error_count": sum(self._number(item, "cell_error_count") or 0 for item in values),
        }

    @staticmethod
    def _run_status(steps: list[dict[str, Any]], run_dir: Path) -> str:
        if any(item.get("status") == "failed" for item in steps):
            return "failed"
        if (run_dir / "logs" / "engine_execution_report.json").is_file():
            return "success"
        if any(item.get("status") == "running" for item in steps):
            return "running"
        return "queued"

    @staticmethod
    def _first_started_at(steps: Iterable[dict[str, Any]]) -> str | None:
        values = sorted(str(item["started_at"]) for item in steps if item.get("started_at"))
        return values[0] if values else None

    @staticmethod
    def _last_finished_at(steps: Iterable[dict[str, Any]]) -> str | None:
        values = sorted(str(item["finished_at"]) for item in steps if item.get("finished_at"))
        return values[-1] if values else None

    @staticmethod
    def _first_error(steps: Iterable[dict[str, Any]]) -> str | None:
        for item in steps:
            if item.get("error"):
                return str(item["error"])
        return None

    @staticmethod
    def _mtime_iso(path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()

    def _table_dir(self, reference: LocalRunReference, table_name: str) -> Path:
        path = self._safe_child(reference.path / "tables", table_name)
        if not path.is_dir():
            raise KeyError(table_name)
        return path

    @staticmethod
    def _safe_child(root: Path, raw_name: str) -> Path:
        candidate = (root / raw_name).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise KeyError(raw_name) from exc
        return candidate

    @staticmethod
    def _is_relative_to(candidate: Path, root: Path) -> bool:
        try:
            candidate.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_json_list(path: Path) -> list[Any]:
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8") or "[]")
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
        return records

    @staticmethod
    def _read_parquet_rows(path: Path, *, offset: int, limit: int) -> list[dict[str, Any]]:
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:  # pragma: no cover - requires spark extra
            raise RuntimeError("Reading local run tables requires the agentcicd[spark] extra") from exc
        files = sorted(path.glob("*.parquet"))
        if not files:
            return []
        table = parquet.read_table([str(item) for item in files])
        sliced = table.slice(offset, limit)
        return [dict(item) for item in sliced.to_pylist()]

    def _redact(self, value: Any) -> Any:
        return redacted_preview(self._replace_secret_values(value), max_preview_bytes=65_536)

    def _replace_secret_values(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._replace_secret_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_secret_values(item) for item in value]
        if isinstance(value, str):
            redacted = value
            for secret in self._secret_values:
                redacted = redacted.replace(secret, "[redacted]")
            return redacted
        return value

    def _redacted_file_bytes(self, path: Path) -> bytes:
        return self._replace_secret_values(path.read_text(encoding="utf-8")).encode("utf-8")

    @staticmethod
    def _content_type(path: Path) -> str:
        return {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".jsonl": "application/x-ndjson; charset=utf-8",
            ".log": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".sql": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }[path.suffix.lower()]

    @staticmethod
    def _number(value: dict[str, Any], key: str) -> int | None:
        raw = value.get(key)
        return raw if isinstance(raw, int) else None

    @staticmethod
    def _string(value: dict[str, Any], key: str) -> str | None:
        raw = value.get(key)
        return raw if isinstance(raw, str) else None

    @staticmethod
    def _looks_sensitive(name: str) -> bool:
        normalized = name.lower()
        return any(token in normalized for token in ("secret", "token", "password", "credential", "authorization", "api_key"))

    @staticmethod
    def _preview(value: str | None) -> str | None:
        if value is None:
            return None
        return value if len(value) <= 256 else f"{value[:255]}..."
