from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AnnotationQueueRead:
    id: str
    name: str
    description: str | None
    admins: list[dict[str, Any]] = field(default_factory=list)
    reviewers: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class AnnotationRequestRead:
    id: str
    organization_id: str | None
    local_project_id: str | None
    queue_id: str
    run_id: str | None
    recipe_id: str | None
    cluster_id: str | None
    source_table: str
    publish_alias: str | None
    instructions: str | None
    reviewers_per_task: int
    reservation_minutes: int
    consensus: str
    template_snapshot: str
    data_path: str
    reviews_path: str
    results_path: str
    manifest_path: str
    status: str
    total_tasks: int
    completed_tasks: int
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class AnnotationTaskRead:
    task_id: str
    data: dict[str, Any] = field(default_factory=dict)
    status: str = "unlabeled"
    review_count: int = 0


@dataclass(frozen=True, slots=True)
class AnnotationReviewRead:
    task_id: str
    reviewer_id: str
    submitted_at: str
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PoolNodeRead:
    pool_name: str
    pool_kind: str
    node_id: str
    address: str | None
    status: str
    capacity: int
    available: int
    generation: int


@dataclass(frozen=True, slots=True)
class PoolLeaseRead:
    lease_id: str
    pool_name: str
    pool_kind: str
    node_id: str
    manager_id: str
    worker_slot_id: str
    address: str | None
    request_id: str
    executor_id: str
    fixture_id: str
    status: str
    acquired_at: float | None
    expires_at: float | None
    generation: int
    lease_decision: str


@dataclass(frozen=True, slots=True)
class RateLimitLeaseRead:
    lease_id: str
    key: str
    max_in_flight: int
    active_count: int
    request_id: str
    acquired_at: float | None
    expires_at: float | None
