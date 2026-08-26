from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentcicd.inspection.entities import AnnotationRequestRead, AnnotationReviewRead, AnnotationTaskRead
from agentcicd.inspection.local_common import ANNOTATION_CONSENSUS_POLICIES, LocalRunReference
from agentcicd.inspection.models import envelope, record


class LocalAnnotationApiMixin:
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
        return value if value in ANNOTATION_CONSENSUS_POLICIES else "none"

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
