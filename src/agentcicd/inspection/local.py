from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from agentcicd.inspection.entities import (
    AnnotationQueueRead,
    AnnotationRequestRead,
    AnnotationReviewRead,
    AnnotationTaskRead,
    PoolLeaseRead,
    PoolNodeRead,
    RateLimitLeaseRead,
)
from agentcicd.inspection.models import (
    InspectionCapabilities,
    InspectionProject,
    InspectionResource,
    InspectionRun,
    envelope,
    record,
)
from agentcicd.project import LocalRunSpec, load_project
from agentcicd.sql.analysis import GraphEdge, GraphNode, build_recipe_dependency_graph
from agentcicd.sql.parsing.segmentation import SQLSegmenter
from agentcicd.sql.observability.redaction import redacted_preview


_SAFE_ARTIFACT_SUFFIXES = frozenset({".html", ".json", ".jsonl", ".log", ".md", ".sql", ".txt"})
_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 1000
_LOCAL_FIXTURE_CALL_PATTERN = re.compile(r"\blocal\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)
_ANNOTATION_CONSENSUS_POLICIES = {"none", "majority", "unanimous"}


@dataclass(frozen=True, slots=True)
class LocalRunReference:
    run_id: str
    path: Path


class LocalInspectionStore:
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
        files: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for path in sorted(logs_dir.rglob("*")) if logs_dir.is_dir() else []:
            if not path.is_file() or path.suffix.lower() not in _SAFE_ARTIFACT_SUFFIXES:
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
            logs_dir / "app.log",
            logs_dir / "engine_execution_report.json",
        ]
        for path in preferred:
            if path.is_file():
                text_parts.append(f"== {path.name} ==\n{self._replace_secret_values(path.read_text(encoding='utf-8'))}")
        return envelope({"run_id": reference.run_id, "files": files, "text": "\n\n".join(text_parts)})

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
        normalized_size = min(max(1, page_size), _MAX_PAGE_SIZE)
        rows = self._read_parquet_rows(table_dir, offset=(normalized_page - 1) * normalized_size, limit=normalized_size + 1)
        has_more = len(rows) > normalized_size
        returned_rows = rows[:normalized_size]
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
        normalized_size = min(max(1, page_size), _MAX_PAGE_SIZE)
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
        if not artifact_path.is_file() or artifact_path.suffix.lower() not in _SAFE_ARTIFACT_SUFFIXES:
            raise KeyError(artifact_name)
        return self._content_type(artifact_path), self._redacted_file_bytes(artifact_path)

    def annotation_requests(self, run_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        items = [record(item) for item in self._annotation_request_records(reference)]
        return envelope({"items": self._redact(items), "total": len(items)})

    def annotation_request(self, run_id: str, request_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        request = self._annotation_request_record(reference, request_id)
        return envelope({"request": self._redact(record(request)), "progress": self._annotation_progress(reference, request_id)})

    def annotation_tasks(self, run_id: str, request_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        request_root = self._annotation_request_root(reference, request_id)
        tasks = [record(self._annotation_task_read(reference, request_root, task)) for task in self._annotation_task_payloads(request_root)]
        completed = sum(1 for task in tasks if task.get("status") == "labeled")
        return envelope({"request_id": request_root.name, "tasks": self._redact(tasks), "total": len(tasks), "completed": completed})

    def annotation_task(self, run_id: str, request_id: str, task_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        request_root = self._annotation_request_root(reference, request_id)
        for task in self._annotation_task_payloads(request_root):
            if str(task.get("task_id") or "") == task_id:
                return envelope({"request_id": request_root.name, "task": self._redact(record(self._annotation_task_read(reference, request_root, task)))})
        raise KeyError(task_id)

    def submit_annotation_review(self, run_id: str, request_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        reference = self._run(run_id)
        request_root = self._annotation_request_root(reference, request_id)
        tasks_by_id = {str(task.get("task_id") or ""): task for task in self._annotation_task_payloads(request_root)}
        if task_id not in tasks_by_id:
            raise KeyError(task_id)
        reviewer_id = str(payload.get("reviewer_id") or "").strip()
        if not reviewer_id:
            raise ValueError("reviewer_id is required")
        result = payload.get("result")
        if not isinstance(result, dict):
            result = {}
        submitted_at = self._now()
        review = AnnotationReviewRead(task_id=task_id, reviewer_id=reviewer_id, submitted_at=submitted_at, result=result)
        review_path = self._annotation_review_path(request_root, task_id, reviewer_id)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(record(review), indent=2, ensure_ascii=False), encoding="utf-8")
        self._update_annotation_manifest_progress(reference, request_root)
        return envelope({"review": self._redact(record(review)), "progress": self._annotation_progress(reference, request_root.name)})

    def finalize_annotation_request(self, run_id: str, request_id: str) -> dict[str, Any]:
        reference = self._run(run_id)
        request_root = self._annotation_request_root(reference, request_id)
        tasks = self._annotation_task_payloads(request_root)
        lines: list[str] = []
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            reviews = self._annotation_reviews(request_root, task_id)
            if len(reviews) < self._annotation_reviewers_per_task(request_root):
                raise RuntimeError("Annotation request is not complete")
            result = self._resolve_annotation_result(request_root, task_id, reviews)
            lines.append(
                json.dumps(
                    {
                        "task_id": task_id,
                        "data": task.get("data") if isinstance(task.get("data"), dict) else {},
                        "result": result,
                        "reviews": reviews,
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            )
        results_path = request_root / "results.jsonl"
        results_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        self._update_annotation_manifest_progress(reference, request_root, status="completed")
        progress = self._annotation_progress(reference, request_root.name)
        return envelope(
            {
                "request_id": request_root.name,
                "total_tasks": progress["total_tasks"],
                "completed_tasks": progress["completed_tasks"],
                "results_path": results_path.as_posix(),
            }
        )

    def runtime_pools(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        snapshot = self._runtime_control_json("/pools/snapshot")
        raw_nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), dict) else {}
        raw_leases = snapshot.get("leases") if isinstance(snapshot.get("leases"), dict) else {}
        leases = [self._pool_lease_record(lease_id, lease) for lease_id, lease in sorted(raw_leases.items()) if isinstance(lease, dict)]
        nodes = [
            self._pool_node_record(node, list(raw_leases.values()))
            for node in raw_nodes.values()
            if isinstance(node, dict)
        ]
        return envelope({"run_id": run_id, "nodes": [record(item) for item in nodes], "leases": [record(item) for item in leases]})

    def runtime_rate_limits(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        snapshot = self._runtime_control_json("/rate-limit/status")
        limiters = snapshot.get("limiters") if isinstance(snapshot.get("limiters"), dict) else {}
        leases = [
            RateLimitLeaseRead(
                lease_id=f"limiter.{key}",
                key=str(key),
                max_in_flight=int((value or {}).get("max_in_flight") or 0) if isinstance(value, dict) else 0,
                active_count=int((value or {}).get("in_flight") or 0) if isinstance(value, dict) else 0,
                request_id="",
                acquired_at=None,
                expires_at=None,
            )
            for key, value in sorted(limiters.items())
        ]
        return envelope({"run_id": run_id, "leases": [record(item) for item in leases]})

    def _annotation_request_records(self, reference: LocalRunReference) -> list[AnnotationRequestRead]:
        root = reference.path / "annotation_tasks"
        if not root.is_dir():
            return []
        records: list[AnnotationRequestRead] = []
        for request_root in sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("annreq.")):
            records.append(self._annotation_request_record(reference, request_root.name))
        return records

    def _annotation_request_record(self, reference: LocalRunReference, request_id: str) -> AnnotationRequestRead:
        request_root = self._annotation_request_root(reference, request_id)
        manifest = self._read_json(request_root / "manifest.json")
        progress = self._annotation_progress(reference, request_root.name)
        queue_name = str(manifest.get("queue_name") or "default")
        created_at = self._timestamp(request_root / "manifest.json")
        updated_at = self._timestamp(request_root / "results.jsonl") or created_at
        return AnnotationRequestRead(
            id=request_root.name,
            organization_id=None,
            local_project_id=self.project_id,
            queue_id=f"annq.{self._slug(queue_name)}",
            run_id=reference.run_id,
            recipe_id="recipe.sql",
            cluster_id=None,
            source_table=str(manifest.get("source_table") or manifest.get("table") or ""),
            publish_alias=str(manifest.get("publish_alias")) if manifest.get("publish_alias") else None,
            instructions=str(manifest.get("instructions")) if manifest.get("instructions") else None,
            reviewers_per_task=self._annotation_reviewers_per_task(request_root),
            reservation_minutes=self._annotation_reservation_minutes(request_root),
            consensus=self._annotation_consensus(request_root),
            template_snapshot=str(manifest.get("template") or ""),
            data_path=str(manifest.get("data_path") or (request_root / "tasks.jsonl").as_posix()),
            reviews_path=str(manifest.get("reviews_path") or (request_root / "reviews").as_posix()),
            results_path=str(manifest.get("results_path") or (request_root / "results.jsonl").as_posix()),
            manifest_path=str(manifest.get("manifest_path") or (request_root / "manifest.json").as_posix()),
            status=str(progress["status"]),
            total_tasks=int(progress["total_tasks"]),
            completed_tasks=int(progress["completed_tasks"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    def _annotation_request_root(self, reference: LocalRunReference, request_id: str) -> Path:
        root = reference.path / "annotation_tasks"
        direct = self._safe_child(root, request_id)
        if direct.is_dir() and direct.name.startswith("annreq."):
            return direct
        alias_file = direct / "request.json"
        if alias_file.is_file():
            payload = self._read_json(alias_file)
            resolved = str(payload.get("request_id") or "").strip()
            if resolved:
                resolved_root = self._safe_child(root, resolved)
                if resolved_root.is_dir():
                    return resolved_root
        raise KeyError(request_id)

    def _annotation_progress(self, reference: LocalRunReference, request_id: str) -> dict[str, Any]:
        request_root = self._annotation_request_root(reference, request_id)
        tasks = self._annotation_task_payloads(request_root)
        completed = sum(1 for task in tasks if self._annotation_task_status(request_root, str(task.get("task_id") or "")) == "labeled")
        status = "completed" if (request_root / "results.jsonl").is_file() else "pending"
        if tasks and completed >= len(tasks) and status != "completed":
            status = "ready_to_finalize"
        return {
            "request_id": request_root.name,
            "total_tasks": len(tasks),
            "completed_tasks": completed,
            "status": status,
        }

    def _annotation_task_payloads(self, request_root: Path) -> list[dict[str, Any]]:
        return self._read_jsonl(request_root / "tasks.jsonl")

    def _annotation_task_read(self, reference: LocalRunReference, request_root: Path, task: dict[str, Any]) -> AnnotationTaskRead:
        task_id = str(task.get("task_id") or task.get("id") or "")
        return AnnotationTaskRead(
            task_id=task_id,
            data=task.get("data") if isinstance(task.get("data"), dict) else {key: value for key, value in task.items() if key != "task_id"},
            status=self._annotation_task_status(request_root, task_id),
            review_count=len(self._annotation_reviews(request_root, task_id)),
        )

    def _annotation_task_status(self, request_root: Path, task_id: str) -> str:
        if len(self._annotation_reviews(request_root, task_id)) < self._annotation_reviewers_per_task(request_root):
            return "unlabeled"
        try:
            self._resolve_annotation_result(request_root, task_id, self._annotation_reviews(request_root, task_id))
        except RuntimeError:
            return "unlabeled"
        return "labeled"

    def _annotation_reviews(self, request_root: Path, task_id: str) -> list[dict[str, Any]]:
        reviews_dir = self._safe_child(request_root / "reviews", f"task={task_id}")
        if not reviews_dir.is_dir():
            return []
        return [
            self._read_json(path)
            for path in sorted(reviews_dir.glob("reviewer=*.json"))
            if path.is_file()
        ]

    def _annotation_review_path(self, request_root: Path, task_id: str, reviewer_id: str) -> Path:
        safe_task_id = self._safe_ref(task_id)
        safe_reviewer_id = self._safe_ref(reviewer_id)
        return request_root / "reviews" / f"task={safe_task_id}" / f"reviewer={safe_reviewer_id}.json"

    def _annotation_reviewers_per_task(self, request_root: Path) -> int:
        return max(1, int(self._annotation_review_policy(request_root).get("reviewers_per_task") or 1))

    def _annotation_reservation_minutes(self, request_root: Path) -> int:
        return max(5, int(self._annotation_review_policy(request_root).get("reservation_minutes") or 30))

    def _annotation_consensus(self, request_root: Path) -> str:
        value = str(self._annotation_review_policy(request_root).get("consensus") or "none").strip().lower()
        return value if value in _ANNOTATION_CONSENSUS_POLICIES else "none"

    def _annotation_review_policy(self, request_root: Path) -> dict[str, Any]:
        manifest = self._read_json(request_root / "manifest.json")
        policy = manifest.get("review_policy")
        return dict(policy) if isinstance(policy, dict) else {}

    def _resolve_annotation_result(self, request_root: Path, task_id: str, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        results = [review.get("result") for review in reviews if isinstance(review.get("result"), dict)]
        if not results:
            return {}
        policy = self._annotation_consensus(request_root)
        if policy == "none":
            return dict(results[0])
        counts: dict[str, int] = {}
        values: dict[str, dict[str, Any]] = {}
        for result in results:
            key = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            counts[key] = counts.get(key, 0) + 1
            values[key] = result
        winner_key, winner_count = max(counts.items(), key=lambda item: item[1])
        required = self._annotation_reviewers_per_task(request_root)
        if policy == "unanimous" and winner_count == len(results) and len(results) >= required:
            return dict(values[winner_key])
        if policy == "majority" and winner_count >= required // 2 + 1:
            return dict(values[winner_key])
        raise RuntimeError(f"Task {task_id} has no {policy} consensus")

    def _update_annotation_manifest_progress(self, reference: LocalRunReference, request_root: Path, *, status: str | None = None) -> None:
        manifest_path = request_root / "manifest.json"
        manifest = self._read_json(manifest_path)
        progress = self._annotation_progress(reference, request_root.name)
        manifest["total_tasks"] = progress["total_tasks"]
        manifest["completed_tasks"] = progress["completed_tasks"]
        manifest["status"] = status or progress["status"]
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    def _runtime_control_json(self, path: str) -> dict[str, Any]:
        base_url = os.getenv("AGENTCICD_RATE_LIMITER_BASE_URL", "").strip()
        if not base_url:
            return {}
        try:
            with urlopen(f"{base_url.rstrip('/')}{path}", timeout=0.3) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except (OSError, URLError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _pool_node_record(self, node: dict[str, Any], leases: list[Any]) -> PoolNodeRead:
        node_id = str(node.get("node_id") or "")
        capacity = int(node.get("capacity") or 1)
        leased = sum(1 for lease in leases if isinstance(lease, dict) and str(lease.get("node_id") or "") == node_id)
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        return PoolNodeRead(
            pool_name=str(node.get("pool_name") or ""),
            pool_kind=str(node.get("pool_kind") or ""),
            node_id=node_id,
            address=str(node.get("address")) if node.get("address") else None,
            status=str(node.get("status") or ""),
            capacity=capacity,
            available=max(0, capacity - leased),
            generation=int(node.get("generation") or metadata.get("generation") or 1),
        )

    def _pool_lease_record(self, lease_id: str, lease: dict[str, Any]) -> PoolLeaseRead:
        return PoolLeaseRead(
            lease_id=lease_id,
            pool_name=str(lease.get("pool_name") or ""),
            pool_kind=str(lease.get("pool_kind") or ""),
            node_id=str(lease.get("node_id") or ""),
            manager_id=str(lease.get("manager_id") or ""),
            worker_slot_id=str(lease.get("worker_slot_id") or ""),
            address=str(lease.get("address")) if lease.get("address") else None,
            request_id=str(lease.get("request_id") or ""),
            executor_id=str(lease.get("executor_id") or ""),
            fixture_id=str(lease.get("fixture_id") or ""),
            status=str(lease.get("status") or ""),
            acquired_at=float(lease["acquired_at"]) if lease.get("acquired_at") is not None else None,
            expires_at=float(lease["expires_at"]) if lease.get("expires_at") is not None else None,
            generation=int(lease.get("generation") or 1),
            lease_decision=str(lease.get("lease_decision") or ""),
        )

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

    def _recipe_graph(self) -> tuple[list[GraphNode], list[GraphEdge]]:
        return self._build_stable_recipe_graph(self._spec.recipe_sql)

    def _build_stable_recipe_graph(self, source_text: str) -> tuple[list[GraphNode], list[GraphEdge]]:
        segmentation = SQLSegmenter(source_text).segment()
        input_segments = [self._segment_record(item) for item in segmentation.inputs]
        function_segments = [self._segment_record(item) for item in segmentation.functions]
        load_segments = [self._segment_record(item) for item in segmentation.loads]
        table_segments = [self._segment_record(item) for item in segmentation.tables]
        save_segments = [self._segment_record(item) for item in segmentation.saves]
        publish_segments = [self._segment_record(item) for item in segmentation.publishes]
        publish_annotation_segments = [self._segment_record(item) for item in segmentation.publish_annotations]
        retrieve_annotation_segments = [self._segment_record(item) for item in segmentation.retrieve_annotations]
        nodes, edges = build_recipe_dependency_graph(
            segmentation=segmentation,
            input_segments=input_segments,
            function_segments=function_segments,
            load_segments=load_segments,
            table_segments=table_segments,
            save_segments=save_segments,
            publish_segments=publish_segments,
            publish_annotation_segments=publish_annotation_segments,
            retrieve_annotation_segments=retrieve_annotation_segments,
            registered_function_names={f"local.{name}" for name in self._used_fixture_names()},
        )
        nodes, edges = self._stable_recipe_graph(
            nodes=nodes,
            edges=edges,
            function_segments=function_segments,
            load_segments=load_segments,
            table_segments=table_segments,
            save_segments=save_segments,
            publish_segments=publish_segments,
            publish_annotation_segments=publish_annotation_segments,
            retrieve_annotation_segments=retrieve_annotation_segments,
        )
        node_ids = {node.id for node in nodes}
        edge_keys = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

        def add_node_once(node_id: str, node_type: str, label: object) -> None:
            if node_id in node_ids:
                return
            nodes.append(GraphNode(id=node_id, type=node_type, label=str(label or "unknown")))
            node_ids.add(node_id)

        def add_edge_once(from_id: str, to_id: str, relation: str) -> None:
            key = (from_id, to_id, relation)
            if key in edge_keys:
                return
            edges.append(GraphEdge(from_id=from_id, to_id=to_id, relation=relation))
            edge_keys.add(key)

        for index, segment in enumerate(save_segments):
            table_name = segment.get("table")
            table_id = self._stable_recipe_item_id("table", table_name)
            save_id = self._stable_recipe_item_id("save", table_name)
            if save_id in node_ids:
                add_node_once(table_id, "table", table_name)
                add_edge_once(table_id, save_id, "save_from_table")

        for index, segment in enumerate(publish_segments):
            table_name = segment.get("table")
            destination = str(segment.get("destination") or "").upper()
            component = str(segment.get("component") or "").lower()
            table_id = self._stable_recipe_item_id("table", table_name)
            publish_id = self._stable_publish_id(table_name, segment.get("destination"), segment.get("component"), index)
            if publish_id not in node_ids:
                continue
            add_node_once(table_id, "table", table_name)
            if destination == "DATASET":
                relation = "publish_dataset"
            elif destination == "REPORTS":
                relation = f"publish_report_{component or 'other'}"
            else:
                relation = "publish"
            add_edge_once(table_id, publish_id, relation)

        for index, segment in enumerate(publish_annotation_segments):
            table_name = segment.get("table")
            table_id = self._stable_recipe_item_id("table", table_name)
            publish_id = self._stable_annotation_publish_id(segment.get("alias") or segment.get("queue_name"), index)
            if publish_id in node_ids:
                add_node_once(table_id, "table", table_name)
                add_edge_once(table_id, publish_id, "publish_annotation")

        for input_name, secret_reference in self._used_secret_input_references(source_text).items():
            secret_id = f"secret:{secret_reference.removeprefix('secret.')}"
            input_id = self._stable_recipe_item_id("input", input_name)
            if secret_id not in node_ids:
                nodes.append(GraphNode(id=secret_id, type="secret", label=secret_reference))
                node_ids.add(secret_id)
            edges.append(GraphEdge(from_id=secret_id, to_id=input_id, relation="provided_to"))
        return nodes, edges

    def _recipe_analysis_payload(self, source_text: str, *, segmentation_key: str) -> dict[str, Any]:
        segmentation = SQLSegmenter(source_text).segment()
        inputs = [self._stable_segment_record("input", item.name, item) for item in segmentation.inputs]
        functions = [self._stable_segment_record("function", item.name, item) for item in segmentation.functions]
        loads = [self._stable_segment_record("load", item.table, item) for item in segmentation.loads]
        tables = [self._stable_segment_record("table", item.table, item) for item in segmentation.tables]
        saves = [self._stable_segment_record("save", item.table, item) for item in segmentation.saves]
        publishes = [
            {
                **self._segment_record(item),
                "id": self._stable_publish_id(item.table, item.destination, item.component, index),
                "source_id": self._stable_recipe_item_id("table", item.table),
            }
            for index, item in enumerate(segmentation.publishes)
        ]
        publish_annotations = [
            {
                **self._segment_record(item),
                "id": self._stable_annotation_publish_id(item.alias or item.queue_name or item.table, index),
                "source_id": self._stable_recipe_item_id("table", item.table),
                "kind": "annotation",
            }
            for index, item in enumerate(segmentation.publish_annotations)
        ]
        retrieves = [
            {
                **self._segment_record(item),
                "id": self._stable_annotation_retrieve_id(item.table, index),
                "target_id": self._stable_recipe_item_id("table", item.table),
            }
            for index, item in enumerate(segmentation.retrieve_annotations)
        ]
        nodes, edges = self._build_stable_recipe_graph(source_text)
        metadata = {
            "total_segments": segmentation.get_segment_count(),
            "total_lines": segmentation.total_lines,
            "has_macros": segmentation.has_macros,
            "macro_placeholders": segmentation.macro_placeholders,
        }
        dependency_edges = [{"from_id": edge.from_id, "to_id": edge.to_id, "relation": edge.relation} for edge in edges]
        payload: dict[str, Any] = {
            "schema_version": "recipe_analysis.v2" if segmentation_key == "analysis" else "recipe_segmentation.v1",
            "valid": True,
            "errors": [],
            "warnings": [],
            "inputs": inputs,
            "functions": functions,
            "loads": loads,
            "tables": tables,
            "saves": saves,
            "publishes": [*publishes, *publish_annotations],
            "retrieves": retrieves,
            "nodes": [{"id": node.id, "type": node.type, "label": node.label} for node in nodes],
            "dependencies": dependency_edges,
            "graph": [{"from": edge.from_id, "to": edge.to_id, "relation": edge.relation} for edge in edges],
            "fixtures": [],
            "metadata": metadata,
        }
        if segmentation_key == "segments":
            payload["publish_annotations"] = publish_annotations
            payload["retrieve_annotations"] = retrieves
        else:
            payload["report"] = {
                "components": publishes,
                "metrics": [item for item in publishes if str(item.get("component") or "").lower() == "metric"],
                "charts": [item for item in publishes if str(item.get("component") or "").lower() == "chart"],
                "issues": [item for item in publishes if str(item.get("component") or "").lower() == "issue"],
                "examples": [item for item in publishes if str(item.get("component") or "").lower() == "example"],
                "datasets": [item for item in publishes if str(item.get("destination") or "").lower() == "dataset"],
                "layout": [],
            }
        return self._redact(payload)

    @classmethod
    def _stable_recipe_graph(
        cls,
        *,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        function_segments: list[dict[str, Any]],
        load_segments: list[dict[str, Any]],
        table_segments: list[dict[str, Any]],
        save_segments: list[dict[str, Any]],
        publish_segments: list[dict[str, Any]],
        publish_annotation_segments: list[dict[str, Any]],
        retrieve_annotation_segments: list[dict[str, Any]],
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        node_id_map: dict[str, str] = {}
        stable_nodes: list[GraphNode] = []
        seen_nodes: set[str] = set()
        for node in nodes:
            stable_id = cls._stable_graph_node_id(
                node_id=node.id,
                node_type=node.type,
                label=node.label,
                function_segments=function_segments,
                load_segments=load_segments,
                table_segments=table_segments,
                save_segments=save_segments,
                publish_segments=publish_segments,
                publish_annotation_segments=publish_annotation_segments,
                retrieve_annotation_segments=retrieve_annotation_segments,
            )
            node_id_map[node.id] = stable_id
            if stable_id in seen_nodes:
                continue
            seen_nodes.add(stable_id)
            stable_nodes.append(GraphNode(id=stable_id, type=node.type, label=node.label))

        stable_edges: list[GraphEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for edge in edges:
            from_id = node_id_map.get(edge.from_id)
            to_id = node_id_map.get(edge.to_id)
            if not from_id or not to_id:
                continue
            key = (from_id, to_id, edge.relation)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            stable_edges.append(GraphEdge(from_id=from_id, to_id=to_id, relation=edge.relation))
        return stable_nodes, stable_edges

    @classmethod
    def _stable_graph_node_id(
        cls,
        *,
        node_id: str,
        node_type: str,
        label: str,
        function_segments: list[dict[str, Any]],
        load_segments: list[dict[str, Any]],
        table_segments: list[dict[str, Any]],
        save_segments: list[dict[str, Any]],
        publish_segments: list[dict[str, Any]],
        publish_annotation_segments: list[dict[str, Any]],
        retrieve_annotation_segments: list[dict[str, Any]],
    ) -> str:
        prefix, _, raw_index = node_id.partition(":")
        index = int(raw_index) if raw_index.isdigit() else -1
        if prefix == "function" and 0 <= index < len(function_segments):
            return cls._stable_recipe_item_id("function", function_segments[index].get("name"))
        if prefix == "load" and 0 <= index < len(load_segments):
            return cls._stable_recipe_item_id("load", load_segments[index].get("table"))
        if prefix == "table" and 0 <= index < len(table_segments):
            return cls._stable_recipe_item_id("table", table_segments[index].get("table"))
        if prefix == "save" and 0 <= index < len(save_segments):
            return cls._stable_recipe_item_id("save", save_segments[index].get("table"))
        if prefix == "publish" and 0 <= index < len(publish_segments):
            segment = publish_segments[index]
            return cls._stable_publish_id(segment.get("table"), segment.get("destination"), segment.get("component"), index)
        if prefix == "publish_annotation" and 0 <= index < len(publish_annotation_segments):
            segment = publish_annotation_segments[index]
            return cls._stable_annotation_publish_id(segment.get("alias") or segment.get("queue_name"), index)
        if prefix == "retrieve_annotation" and 0 <= index < len(retrieve_annotation_segments):
            return cls._stable_annotation_retrieve_id(retrieve_annotation_segments[index].get("table"), index)
        if node_type == "table":
            return cls._stable_recipe_item_id("table", label)
        if node_type == "load":
            return cls._stable_recipe_item_id("load", label)
        if node_type.startswith("publish_report"):
            return cls._stable_publish_id(label, "reports", node_type.removeprefix("publish_report_"), 0)
        if node_type == "publish_dataset":
            return cls._stable_publish_id(label, "dataset", "", 0)
        return node_id

    @classmethod
    def _stable_recipe_item_id(cls, prefix: str, name: object) -> str:
        return f"{prefix}:{cls._stable_slug(str(name or 'unknown'))}"

    @classmethod
    def _stable_publish_id(cls, table: object, destination: object, component: object, index: int) -> str:
        destination_slug = cls._stable_slug(str(destination or "publish"))
        table_slug = cls._stable_slug(str(table or f"publish_{index + 1}"))
        component_slug = cls._stable_slug(str(component or ""))
        suffix = f":{component_slug}" if component_slug else ""
        return f"publish:{table_slug}:{destination_slug}{suffix}"

    @classmethod
    def _stable_annotation_publish_id(cls, ref: object, index: int) -> str:
        return f"publish:{cls._stable_slug(str(ref or f'annotation_{index + 1}'))}:annotation"

    @classmethod
    def _stable_annotation_retrieve_id(cls, table: object, index: int) -> str:
        return f"retrieve:{cls._stable_slug(str(table or f'annotations_{index + 1}'))}:annotation"

    @staticmethod
    def _stable_slug(value: str) -> str:
        return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip().lower()).strip("_") or "unknown"

    @staticmethod
    def _segment_record(segment: object) -> dict[str, Any]:
        payload = dict(vars(segment))
        payload["source_text"] = payload.pop("sql_text", "")
        return payload

    @classmethod
    def _stable_segment_record(cls, prefix: str, name: object, segment: object) -> dict[str, Any]:
        return {"id": cls._stable_recipe_item_id(prefix, name), **cls._segment_record(segment)}

    def _used_fixture_names(self) -> set[str]:
        return {match.group(1).lower() for match in _LOCAL_FIXTURE_CALL_PATTERN.finditer(self._spec.recipe_sql)}

    def _fixture_function_names(self, source: Path) -> set[str]:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        except (OSError, SyntaxError):
            return set()
        return {
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
        }

    def _used_secret_input_references(self, source_text: str | None = None) -> dict[str, str]:
        used_input_names = self._used_input_names(source_text or self._spec.recipe_sql)
        return {
            name: value
            for name, value in self._spec.inputs.input_values.items()
            if name in used_input_names and isinstance(value, str) and value.startswith("secret.")
        }

    def _used_secret_references(self) -> set[str]:
        return set(self._used_secret_input_references().values())

    def _used_input_names(self, source_text: str | None = None) -> set[str]:
        declarations = self._spec.inputs.input_sources
        non_declaration_sql = re.sub(r"(?is)\bDECLARE\s+INPUT\b.*?;", "", source_text or self._spec.recipe_sql)
        return {
            name
            for name in declarations
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", non_declaration_sql)
        }

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
