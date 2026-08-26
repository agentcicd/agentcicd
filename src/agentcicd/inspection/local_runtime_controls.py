from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from agentcicd.inspection.entities import PoolLeaseRead, PoolNodeRead, RateLimitLeaseRead
from agentcicd.inspection.models import envelope, record


class LocalRuntimeControlsMixin:
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
