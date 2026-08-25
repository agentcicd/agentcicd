from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep
from typing import Any, AsyncContextManager, AsyncIterator, ContextManager, Iterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


__all__ = [
    "DriverLeaseManager",
    "DriverPoolLeaseManager",
    "Lease",
    "PoolLease",
    "RuntimeLimiter",
    "runtime_pool_lease",
    "runtime_limiter",
    "runtime_max_in_flight",
    "start_driver_rate_limit_server",
    "start_driver_runtime_control_server",
]

_DEFAULT_LIMITER_KEY = "default"
_DEFAULT_LEASE_TTL_SECONDS = 1200.0
_DEFAULT_WAIT_TIMEOUT_SECONDS = 1200.0
_DEFAULT_HTTP_TIMEOUT_SECONDS = 1210.0
_DEFAULT_POOL_ACQUIRE_POLL_SECONDS = 0.5
_DEFAULT_DRIVER_PORT = 18080


# Public runtime limiter contract.


@dataclass(frozen=True)
class Lease:
    limiter_name: str
    permits: int
    acquired_at: float
    lease_id: str | None = None
    key: str = _DEFAULT_LIMITER_KEY


@dataclass(frozen=True)
class PoolLease:
    pool_name: str
    pool_kind: str
    lease_id: str
    acquired_at: float
    node_id: str | None = None
    manager_id: str | None = None
    worker_slot_id: str | None = None
    generation: int | None = None
    address: str | None = None
    expires_at: float | None = None
    request_id: str | None = None
    fixture_id: str | None = None
    lease_decision: str | None = None


class PoolLeaseUnavailable(RuntimeError):
    """Raised when a non-blocking pool lease acquire finds no available slot."""


class RateLimitUnavailable(RuntimeError):
    """Raised when a non-blocking rate-limit acquire finds no available permit."""


class RuntimeLimiter(Protocol):
    def acquire(self, *, permits: int = 1) -> AsyncContextManager[Lease]:
        ...

    def acquire_blocking(self, *, permits: int = 1) -> ContextManager[Lease]:
        ...


class DriverLeaseManager:
    """In-driver, key-based lease manager for one Spark application."""

    def __init__(self, default_max_in_flight: int) -> None:
        self.default_max_in_flight = max(1, int(default_max_in_flight))
        self._condition = threading.Condition()
        self._limits: dict[str, int] = {_DEFAULT_LIMITER_KEY: self.default_max_in_flight}
        self._leases: dict[str, dict[str, Any]] = {}

    def configure(self, key: str, max_in_flight: int) -> None:
        normalized_key = str(key or _DEFAULT_LIMITER_KEY).strip() or _DEFAULT_LIMITER_KEY
        with self._condition:
            self._limits[normalized_key] = max(1, int(max_in_flight))
            self._condition.notify_all()

    def acquire(
        self,
        *,
        key: str = _DEFAULT_LIMITER_KEY,
        permits: int = 1,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        wait_timeout_seconds: float = _DEFAULT_WAIT_TIMEOUT_SECONDS,
        try_only: bool = False,
    ) -> Lease:
        normalized_key = str(key or _DEFAULT_LIMITER_KEY).strip() or _DEFAULT_LIMITER_KEY
        requested_permits = max(1, int(permits or 1))
        ttl_seconds = max(1.0, float(lease_ttl_seconds or 30.0))
        deadline = monotonic() + max(0.1, float(wait_timeout_seconds or 300.0))
        with self._condition:
            while True:
                self._expire_locked()
                max_in_flight = self._limits.get(normalized_key, self.default_max_in_flight)
                granted_permits = min(requested_permits, max_in_flight)
                if self._in_flight_locked(normalized_key) + granted_permits <= max_in_flight:
                    lease_id = uuid.uuid4().hex
                    now = monotonic()
                    self._leases[lease_id] = {
                        "key": normalized_key,
                        "permits": granted_permits,
                        "expires_at": now + ttl_seconds,
                    }
                    return Lease(
                        limiter_name="driver",
                        permits=granted_permits,
                        acquired_at=now,
                        lease_id=lease_id,
                        key=normalized_key,
                    )
                if try_only:
                    raise RateLimitUnavailable(
                        f"No rate-limit permit available for {normalized_key}"
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out acquiring {granted_permits} rate-limit permit(s) for {normalized_key}"
                    )
                self._condition.wait(timeout=min(remaining, 0.25))

    def release(self, lease_id: str) -> bool:
        if not lease_id:
            return False
        with self._condition:
            removed = self._leases.pop(lease_id, None)
            self._condition.notify_all()
            return removed is not None

    def renew(self, lease_id: str, *, lease_ttl_seconds: float = 30.0) -> bool:
        if not lease_id:
            return False
        with self._condition:
            self._expire_locked()
            lease = self._leases.get(lease_id)
            if lease is None:
                return False
            lease["expires_at"] = monotonic() + max(1.0, float(lease_ttl_seconds or 30.0))
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            self._expire_locked()
            keys = sorted(set(self._limits) | {str(lease["key"]) for lease in self._leases.values()})
            return {
                "limiters": {
                    key: {
                        "max_in_flight": self._limits.get(key, self.default_max_in_flight),
                        "in_flight": self._in_flight_locked(key),
                    }
                    for key in keys
                }
            }

    def _in_flight_locked(self, key: str) -> int:
        return sum(
            int(lease["permits"])
            for lease in self._leases.values()
            if lease.get("key") == key
        )

    def _expire_locked(self) -> None:
        now = monotonic()
        expired = [
            lease_id
            for lease_id, lease in self._leases.items()
            if float(lease.get("expires_at") or 0) <= now
        ]
        for lease_id in expired:
            self._leases.pop(lease_id, None)
        if expired:
            self._condition.notify_all()


class DriverPoolLeaseManager:
    """Driver-local pool node and lease manager for one run attempt."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._nodes: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, dict[str, Any]] = {}
        self._request_leases: dict[str, str] = {}

    def register_node(
        self,
        *,
        pool_name: str,
        pool_kind: str,
        node_id: str,
        address: str,
        capacity: int = 1,
        metadata: dict[str, Any] | None = None,
        heartbeat_ttl_seconds: float = 60.0,
    ) -> dict[str, Any]:
        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            raise ValueError("node_id is required")
        now = monotonic()
        node_metadata = dict(metadata or {})
        generation = int(node_metadata.get("generation") or 1)
        node_metadata.setdefault("base_node_id", normalized_node_id)
        node_metadata["generation"] = generation
        with self._condition:
            self._nodes[normalized_node_id] = {
                "pool_name": str(pool_name or "").strip(),
                "pool_kind": str(pool_kind or "").strip().lower(),
                "node_id": normalized_node_id,
                "address": str(address or "").strip(),
                "capacity": max(1, int(capacity or 1)),
                "metadata": node_metadata,
                "generation": generation,
                "heartbeat_ttl_seconds": max(1.0, float(heartbeat_ttl_seconds or 60.0)),
                "status": "available",
                "registered_at": now,
                "last_heartbeat_at": now,
                "expires_at": now + max(1.0, float(heartbeat_ttl_seconds or 60.0)),
            }
            self._condition.notify_all()
            return dict(self._nodes[normalized_node_id])

    @staticmethod
    def validate_pool_nodes(pool_nodes: list[dict[str, Any]]) -> None:
        for node in pool_nodes:
            metadata = dict(node.get("metadata") or {})
            fixture_ids = metadata.get("fixture_ids")
            if not isinstance(fixture_ids, list) or not fixture_ids:
                raise ValueError(f"Pool node {node.get('node_id') or '<unknown>'} is missing metadata.fixture_ids")
            normalized = [str(item).strip() for item in fixture_ids if str(item).strip()]
            if not normalized:
                raise ValueError(f"Pool node {node.get('node_id') or '<unknown>'} has empty metadata.fixture_ids")

    def heartbeat_node(self, node_id: str, *, heartbeat_ttl_seconds: float = 60.0) -> bool:
        normalized_node_id = str(node_id or "").strip()
        with self._condition:
            self._expire_locked()
            node = self._nodes.get(normalized_node_id)
            if node is None:
                return False
            now = monotonic()
            node["last_heartbeat_at"] = now
            node["expires_at"] = now + max(1.0, float(heartbeat_ttl_seconds or 60.0))
            return True

    def acquire(
        self,
        *,
        pool_name: str,
        pool_kind: str = "session",
        request_id: str | None = None,
        executor_id: str | None = None,
        fixture_id: str | None = None,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        wait_timeout_seconds: float = _DEFAULT_WAIT_TIMEOUT_SECONDS,
        synthetic_address: str | None = None,
        try_only: bool = False,
    ) -> PoolLease:
        normalized_pool = str(pool_name or "").strip()
        if not normalized_pool:
            raise ValueError("pool_name is required")
        normalized_kind = str(pool_kind or "session").strip().lower()
        normalized_request_id = str(request_id or "").strip()
        normalized_fixture_id = str(fixture_id or "").strip()
        ttl_seconds = max(1.0, float(lease_ttl_seconds or _DEFAULT_LEASE_TTL_SECONDS))
        wait_seconds = 0.0 if try_only else max(0.0, float(wait_timeout_seconds or _DEFAULT_WAIT_TIMEOUT_SECONDS))
        deadline = monotonic() + wait_seconds
        with self._condition:
            while True:
                self._expire_locked()
                if normalized_request_id:
                    existing_id = self._request_leases.get(normalized_request_id)
                    existing = self._leases.get(existing_id or "")
                    if existing is not None:
                        existing_fixture_id = str(existing.get("fixture_id") or "").strip()
                        if not normalized_fixture_id or not existing_fixture_id or existing_fixture_id == normalized_fixture_id:
                            return self._lease_from_record(existing_id or "", existing)
                        self._request_leases.pop(normalized_request_id, None)
                node = self._available_node_locked(normalized_pool, normalized_kind, normalized_fixture_id)
                if node is not None:
                    lease_id = f"lease.{uuid.uuid4().hex}"
                    now = monotonic()
                    lease = {
                        "pool_name": normalized_pool,
                        "pool_kind": normalized_kind,
                        "node_id": node["node_id"],
                        "manager_id": node["node_id"],
                        "worker_slot_id": self._available_worker_slot_locked(node),
                        "address": node.get("address"),
                        "request_id": normalized_request_id,
                        "executor_id": str(executor_id or "").strip(),
                        "fixture_id": normalized_fixture_id,
                        "status": "leased",
                        "acquired_at": now,
                        "expires_at": now + ttl_seconds,
                        "generation": int(node.get("generation") or 1),
                        "lease_decision": "reuse_compatible",
                    }
                    self._leases[lease_id] = lease
                    if normalized_request_id:
                        self._request_leases[normalized_request_id] = lease_id
                    return self._lease_from_record(lease_id, lease)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    if try_only:
                        raise PoolLeaseUnavailable(f"Pool lease unavailable for {normalized_pool}")
                    raise TimeoutError(f"Timed out acquiring pool lease for {normalized_pool}")
                self._condition.wait(timeout=min(remaining, 0.25))

    def renew_lease(self, lease_id: str, *, lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS) -> bool:
        with self._condition:
            self._expire_locked()
            lease = self._leases.get(str(lease_id or "").strip())
            if lease is None:
                return False
            lease["expires_at"] = monotonic() + max(1.0, float(lease_ttl_seconds or _DEFAULT_LEASE_TTL_SECONDS))
            return True

    def validate_lease(
        self,
        *,
        lease_id: str,
        pool_name: str,
        pool_kind: str,
        fixture_id: str,
        manager_id: str,
        worker_slot_id: str,
        generation: int,
    ) -> tuple[bool, str]:
        normalized_lease_id = str(lease_id or "").strip()
        with self._condition:
            self._expire_locked()
            lease = self._leases.get(normalized_lease_id)
            if lease is None:
                return False, "lease is unknown or expired"
            checks = {
                "pool_name": str(pool_name or "").strip(),
                "pool_kind": str(pool_kind or "").strip().lower(),
                "fixture_id": str(fixture_id or "").strip(),
                "manager_id": str(manager_id or "").strip(),
                "worker_slot_id": str(worker_slot_id or "").strip(),
            }
            for key, expected in checks.items():
                actual = str(lease.get(key) or "").strip()
                if key == "pool_kind":
                    actual = actual.lower()
                if expected and actual and actual != expected:
                    return False, f"lease {key} mismatch"
            if int(lease.get("generation") or 0) != int(generation or 0):
                return False, "lease generation is stale"
            return True, ""

    def return_lease(
        self,
        lease_id: str,
        *,
        terminal_status: str = "completed",
        node_disposition: str = "available",
    ) -> bool:
        normalized_lease_id = str(lease_id or "").strip()
        with self._condition:
            lease = self._leases.pop(normalized_lease_id, None)
            if lease is None:
                return False
            request_id = str(lease.get("request_id") or "")
            if request_id:
                self._request_leases.pop(request_id, None)
            node = self._nodes.get(str(lease.get("node_id") or ""))
            if node is not None:
                disposition = str(node_disposition or "available").strip().lower()
                if disposition in {"available", "healthy", "discard"}:
                    node["status"] = "available"
                elif disposition in {"drain", "unhealthy", "unknown"}:
                    node["status"] = "terminated" if disposition == "drain" else "unhealthy"
                node["last_terminal_status"] = str(terminal_status or "completed")
            self._condition.notify_all()
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            self._expire_locked()
            return {
                "nodes": {node_id: dict(node) for node_id, node in sorted(self._nodes.items())},
                "leases": {lease_id: dict(lease) for lease_id, lease in sorted(self._leases.items())},
            }

    def _available_node_locked(self, pool_name: str, pool_kind: str, fixture_id: str = "") -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for node in self._nodes.values():
            if node.get("pool_name") != pool_name or node.get("pool_kind") != pool_kind:
                continue
            if fixture_id and not _node_can_execute_fixture(node, fixture_id):
                continue
            if node.get("status") != "available":
                continue
            leased = sum(1 for lease in self._leases.values() if lease.get("node_id") == node.get("node_id"))
            if leased < int(node.get("capacity") or 1):
                item = dict(node)
                item["_leased"] = leased
                candidates.append(item)
        if not candidates:
            return None
        candidates.sort(key=_node_preference_key)
        selected = candidates[0]
        selected.pop("_leased", None)
        return selected

    def _available_worker_slot_locked(self, node: dict[str, Any]) -> str:
        node_id = str(node.get("node_id") or "")
        capacity = max(1, int(node.get("capacity") or 1))
        if str(node.get("pool_kind") or "").strip().lower() == "service":
            metadata = dict(node.get("metadata") or {})
            worker_capacity = max(1, int(metadata.get("worker_capacity") or capacity))
            leased_by_slot = {
                f"{node_id}.slot-{index + 1}": 0
                for index in range(worker_capacity)
            }
            for lease in self._leases.values():
                if lease.get("node_id") != node_id:
                    continue
                slot_id = str(lease.get("worker_slot_id") or "")
                if slot_id in leased_by_slot:
                    leased_by_slot[slot_id] += 1
            return min(leased_by_slot, key=lambda slot_id: (leased_by_slot[slot_id], slot_id))
        leased_slots = {
            str(lease.get("worker_slot_id") or "")
            for lease in self._leases.values()
            if lease.get("node_id") == node_id
        }
        for index in range(capacity):
            slot_id = f"{node_id}.slot-{index + 1}"
            if slot_id not in leased_slots:
                return slot_id
        return f"{node_id}.slot-1"

    def _expire_locked(self) -> None:
        now = monotonic()
        for node in self._nodes.values():
            if float(node.get("expires_at") or 0) <= now and node.get("status") not in {"terminated", "unhealthy"}:
                node["status"] = "unhealthy"
        expired_leases = [
            lease_id
            for lease_id, lease in self._leases.items()
            if float(lease.get("expires_at") or 0) <= now
        ]
        for lease_id in expired_leases:
            lease = self._leases.pop(lease_id, None)
            if lease and lease.get("request_id"):
                self._request_leases.pop(str(lease.get("request_id")), None)
            node = self._nodes.get(str((lease or {}).get("node_id") or ""))
            if node is not None:
                if node.get("pool_kind") == "session":
                    node["status"] = "unhealthy"
                else:
                    node["status"] = "available"
        if expired_leases:
            self._condition.notify_all()

    @staticmethod
    def _lease_from_record(lease_id: str, lease: dict[str, Any]) -> PoolLease:
        return PoolLease(
            pool_name=str(lease.get("pool_name") or ""),
            pool_kind=str(lease.get("pool_kind") or ""),
            lease_id=lease_id,
            acquired_at=float(lease.get("acquired_at") or monotonic()),
            node_id=str(lease.get("node_id") or "") or None,
            manager_id=str(lease.get("manager_id") or lease.get("node_id") or "") or None,
            worker_slot_id=str(lease.get("worker_slot_id") or "") or None,
            generation=int(lease["generation"]) if lease.get("generation") is not None else None,
            address=str(lease.get("address") or "") or None,
            expires_at=float(lease.get("expires_at")) if lease.get("expires_at") is not None else None,
            request_id=str(lease.get("request_id") or "") or None,
            fixture_id=str(lease.get("fixture_id") or "") or None,
            lease_decision=str(lease.get("lease_decision") or "") or None,
        )

# Private limiter implementations.


class _LocalRuntimeLimiter:
    def __init__(self, max_in_flight: int, *, key: str = _DEFAULT_LIMITER_KEY) -> None:
        self.max_in_flight = max(1, int(max_in_flight))
        self.key = str(key or _DEFAULT_LIMITER_KEY).strip() or _DEFAULT_LIMITER_KEY
        self._async_semaphore = asyncio.Semaphore(self.max_in_flight)
        self._sync_semaphore = threading.BoundedSemaphore(self.max_in_flight)

    @asynccontextmanager
    async def acquire(self, *, permits: int = 1) -> AsyncIterator[Lease]:
        permits = max(1, min(int(permits or 1), self.max_in_flight))
        for _ in range(permits):
            await self._async_semaphore.acquire()
        lease = Lease(limiter_name="local", permits=permits, acquired_at=monotonic(), key=self.key)
        try:
            yield lease
        finally:
            for _ in range(permits):
                self._async_semaphore.release()

    @contextmanager
    def acquire_blocking(self, *, permits: int = 1) -> Iterator[Lease]:
        permits = max(1, min(int(permits or 1), self.max_in_flight))
        for _ in range(permits):
            self._sync_semaphore.acquire()
        lease = Lease(limiter_name="local", permits=permits, acquired_at=monotonic(), key=self.key)
        try:
            yield lease
        finally:
            for _ in range(permits):
                self._sync_semaphore.release()


class _DriverRuntimeLimiterClient:
    def __init__(
        self,
        base_url: str,
        *,
        key: str = _DEFAULT_LIMITER_KEY,
        default_max_in_flight: int | None = None,
        lease_ttl_seconds: float = _DEFAULT_LEASE_TTL_SECONDS,
        wait_timeout_seconds: float = _DEFAULT_WAIT_TIMEOUT_SECONDS,
        http_timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = str(key or _DEFAULT_LIMITER_KEY).strip() or _DEFAULT_LIMITER_KEY
        self.default_max_in_flight = max(1, int(default_max_in_flight or 8))
        self.lease_ttl_seconds = max(0.1, float(lease_ttl_seconds))
        self.wait_timeout_seconds = max(0.1, float(wait_timeout_seconds))
        self.http_timeout_seconds = max(0.1, float(http_timeout_seconds))

    @asynccontextmanager
    async def acquire(self, *, permits: int = 1) -> AsyncIterator[Lease]:
        lease = await asyncio.to_thread(self._acquire_blocking, permits)
        try:
            yield lease
        finally:
            await asyncio.to_thread(self._release, lease)

    @contextmanager
    def acquire_blocking(self, *, permits: int = 1) -> Iterator[Lease]:
        lease = self._acquire_blocking(permits)
        try:
            yield lease
        finally:
            self._release(lease)

    def _acquire_blocking(self, permits: int) -> Lease:
        payload = {
            "key": self.key,
            "permits": max(1, int(permits or 1)),
            "max_in_flight": self.default_max_in_flight,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "wait_timeout_seconds": 0,
            "try_only": True,
        }
        deadline = monotonic() + max(0.1, self.wait_timeout_seconds)
        poll_seconds = float(
            os.getenv("AGENTCICD_RATE_LIMITER_ACQUIRE_POLL_SECONDS", _DEFAULT_POOL_ACQUIRE_POLL_SECONDS)
        )
        while True:
            try:
                response = _post_json(
                    f"{self.base_url}/rate-limit/acquire",
                    payload,
                    timeout_seconds=min(10.0, self.http_timeout_seconds),
                )
                break
            except RateLimitUnavailable:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    requested_permits = max(1, int(permits or 1))
                    raise TimeoutError(
                        f"Timed out acquiring {requested_permits} rate-limit permit(s) for {self.key}"
                    ) from None
                sleep(max(0.05, min(remaining, poll_seconds)))
        return Lease(
            limiter_name=str(response.get("limiter_name") or "driver"),
            permits=max(1, int(response.get("permits") or 1)),
            acquired_at=monotonic(),
            lease_id=str(response.get("lease_id") or ""),
            key=str(response.get("key") or self.key),
        )

    def _release(self, lease: Lease) -> None:
        if not lease.lease_id:
            return
        try:
            _post_json(
                f"{self.base_url}/rate-limit/release",
                {"lease_id": lease.lease_id},
                timeout_seconds=min(10.0, self.http_timeout_seconds),
            )
        except Exception:
            return


_LOCAL_LIMITERS: dict[tuple[str, int], _LocalRuntimeLimiter] = {}
_REMOTE_LIMITERS: dict[tuple[str, str, int], _DriverRuntimeLimiterClient] = {}
_LOCK = threading.Lock()


# Public factory and driver-server entrypoint.


def runtime_max_in_flight(default: int | None = None) -> int:
    raw = os.getenv("AGENTCICD_FIXTURE_MAX_IN_FLIGHT", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, int(default or 8))


def runtime_limiter(default: int | None = None, *, key: str = _DEFAULT_LIMITER_KEY) -> RuntimeLimiter:
    """Return the runtime-call limiter for this process.

    Spark executors get an HTTP client when AGENTCICD_RATE_LIMITER_BASE_URL is set.
    Local tests and fast/local jobs fall back to an in-process semaphore.
    """
    max_in_flight = runtime_max_in_flight(default)
    normalized_key = str(key or _DEFAULT_LIMITER_KEY).strip() or _DEFAULT_LIMITER_KEY
    base_url = os.getenv("AGENTCICD_RATE_LIMITER_BASE_URL", "").strip()
    lease_ttl_seconds = float(os.getenv("AGENTCICD_RATE_LIMITER_LEASE_TTL_SECONDS", _DEFAULT_LEASE_TTL_SECONDS))
    wait_timeout_seconds = float(os.getenv("AGENTCICD_RATE_LIMITER_WAIT_TIMEOUT_SECONDS", _DEFAULT_WAIT_TIMEOUT_SECONDS))
    http_timeout_seconds = float(os.getenv("AGENTCICD_RATE_LIMITER_HTTP_TIMEOUT_SECONDS", _DEFAULT_HTTP_TIMEOUT_SECONDS))
    with _LOCK:
        if base_url:
            cache_key = (base_url.rstrip("/"), normalized_key, max_in_flight)
            limiter = _REMOTE_LIMITERS.get(cache_key)
            if limiter is None:
                limiter = _DriverRuntimeLimiterClient(
                    base_url,
                    key=normalized_key,
                    default_max_in_flight=max_in_flight,
                    lease_ttl_seconds=lease_ttl_seconds,
                    wait_timeout_seconds=wait_timeout_seconds,
                    http_timeout_seconds=http_timeout_seconds,
                )
                _REMOTE_LIMITERS[cache_key] = limiter
            return limiter
        cache_key = (normalized_key, max_in_flight)
        local = _LOCAL_LIMITERS.get(cache_key)
        if local is None:
            local = _LocalRuntimeLimiter(max_in_flight, key=normalized_key)
            _LOCAL_LIMITERS[cache_key] = local
        return local


@contextmanager
def runtime_pool_lease(
    pool: dict[str, Any] | None,
    *,
    request_id: str | None = None,
    executor_id: str | None = None,
    fallback_address: str | None = None,
    fixture_id: str | None = None,
    heartbeat: bool = True,
) -> Iterator[PoolLease | None]:
    if not pool:
        yield None
        return
    base_url = os.getenv("AGENTCICD_RATE_LIMITER_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        yield None
        return
    pool_name = str(pool.get("key") or pool.get("pool_name") or "").strip()
    config = pool.get("config") if isinstance(pool.get("config"), dict) else {}
    pool_kind = str(pool.get("kind") or config.get("kind") or "session").strip().lower()
    lease_ttl_seconds = float(
        pool.get("lease_ttl_seconds")
        or config.get("lease_ttl_seconds")
        or os.getenv("AGENTCICD_POOL_LEASE_TTL_SECONDS", _DEFAULT_LEASE_TTL_SECONDS)
    )
    acquire_timeout_seconds = float(
        pool.get("wait_timeout_seconds")
        or config.get("wait_timeout_seconds")
        or pool.get("timeout_seconds")
        or config.get("timeout_seconds")
        or os.getenv("AGENTCICD_POOL_WAIT_TIMEOUT_SECONDS", _DEFAULT_WAIT_TIMEOUT_SECONDS)
    )
    http_timeout_seconds = float(os.getenv("AGENTCICD_RATE_LIMITER_HTTP_TIMEOUT_SECONDS", _DEFAULT_HTTP_TIMEOUT_SECONDS))
    poll_seconds = float(os.getenv("AGENTCICD_POOL_ACQUIRE_POLL_SECONDS", _DEFAULT_POOL_ACQUIRE_POLL_SECONDS))
    lease: PoolLease | None = None
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        pool_fixture_id = str(pool.get("fixture_id") or "").strip()
        payload = {
            "pool_name": pool_name,
            "pool_kind": pool_kind,
            "request_id": request_id or pool.get("request_id") or _default_pool_request_id(pool_name),
            "executor_id": executor_id or os.getenv("SPARK_EXECUTOR_ID", ""),
            "fixture_id": pool_fixture_id or fixture_id or "",
            "lease_ttl_seconds": lease_ttl_seconds,
            "wait_timeout_seconds": 0,
            "try_only": True,
        }
        deadline = monotonic() + max(0.1, acquire_timeout_seconds)
        while True:
            try:
                response = _post_json(
                    f"{base_url}/pools/leases/acquire",
                    payload,
                    timeout_seconds=min(10.0, http_timeout_seconds),
                )
                break
            except PoolLeaseUnavailable:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out acquiring pool lease for {pool_name}") from None
                sleep(max(0.05, min(remaining, poll_seconds)))
        lease = PoolLease(
            pool_name=str(response.get("pool_name") or pool_name),
            pool_kind=str(response.get("pool_kind") or pool_kind),
            lease_id=str(response.get("lease_id") or ""),
            acquired_at=monotonic(),
            node_id=str(response.get("node_id") or "") or None,
            manager_id=str(response.get("manager_id") or response.get("node_id") or "") or None,
            worker_slot_id=str(response.get("worker_slot_id") or "") or None,
            generation=int(response["generation"]) if response.get("generation") is not None else None,
            address=str(response.get("address") or "") or None,
            expires_at=float(response["expires_at"]) if response.get("expires_at") is not None else None,
            request_id=str(response.get("request_id") or payload["request_id"]) or None,
            fixture_id=str(response.get("fixture_id") or payload["fixture_id"]) or None,
            lease_decision=str(response.get("lease_decision") or "") or None,
        )
        if heartbeat and lease.lease_id:
            heartbeat_thread = _start_pool_lease_heartbeat(
                base_url,
                lease.lease_id,
                lease_ttl_seconds=lease_ttl_seconds,
                http_timeout_seconds=http_timeout_seconds,
                stop=heartbeat_stop,
            )
        yield lease
    except BaseException as exc:
        if lease is not None and lease.lease_id:
            _return_pool_lease(
                base_url,
                lease,
                terminal_status=_terminal_status_for_exception(exc),
                node_disposition=_node_disposition_for_status(pool_kind, _terminal_status_for_exception(exc)),
                http_timeout_seconds=http_timeout_seconds,
            )
            lease = None
        raise
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        if lease is not None and lease.lease_id:
            _return_pool_lease(
                base_url,
                lease,
                terminal_status="completed",
                node_disposition=_node_disposition_for_status(pool_kind, "completed"),
                http_timeout_seconds=http_timeout_seconds,
            )


def _default_pool_request_id(pool_name: str) -> str:
    parts = [
        os.getenv("AGENTCICD_RUN_ID", ""),
        os.getenv("AGENTCICD_RUN_ATTEMPT", ""),
        os.getenv("SPARK_EXECUTOR_ID", ""),
        os.getenv("AGENTCICD_POOL_STAGE_ID", ""),
        os.getenv("AGENTCICD_POOL_PARTITION_ID", ""),
        os.getenv("AGENTCICD_POOL_FUNCTION_CALL_ID", ""),
        pool_name,
    ]
    joined = ":".join(str(part) for part in parts if str(part))
    return joined or uuid.uuid4().hex


def _terminal_status_for_exception(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed_out"
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return "cancelled"
    return "failed"


def _node_disposition_for_status(pool_kind: str, terminal_status: str) -> str:
    normalized_kind = str(pool_kind or "").strip().lower()
    normalized_status = str(terminal_status or "").strip().lower()
    if normalized_kind == "sandbox":
        return "discard"
    if normalized_kind == "session" and normalized_status in {"timed_out", "cancelled"}:
        return "unhealthy"
    return "available"


def _return_pool_lease(
    base_url: str,
    lease: PoolLease,
    *,
    terminal_status: str,
    node_disposition: str,
    http_timeout_seconds: float,
) -> None:
    try:
        _post_json(
            f"{base_url}/pools/leases/{lease.lease_id}/return",
            {"terminal_status": terminal_status, "node_disposition": node_disposition},
            timeout_seconds=min(10.0, http_timeout_seconds),
        )
    except Exception:
        pass


def _start_pool_lease_heartbeat(
    base_url: str,
    lease_id: str,
    *,
    lease_ttl_seconds: float,
    http_timeout_seconds: float,
    stop: threading.Event,
) -> threading.Thread:
    interval = max(1.0, min(10.0, lease_ttl_seconds / 3.0))

    def _heartbeat_loop() -> None:
        while not stop.wait(interval):
            try:
                _post_json(
                    f"{base_url}/pools/leases/{lease_id}/heartbeat",
                    {"lease_ttl_seconds": lease_ttl_seconds},
                    timeout_seconds=min(5.0, http_timeout_seconds),
                )
            except Exception:
                continue

    thread = threading.Thread(target=_heartbeat_loop, name=f"agentcicd-pool-lease-heartbeat-{lease_id}", daemon=True)
    thread.start()
    return thread


def start_driver_rate_limit_server(
    *,
    host: str = "0.0.0.0",
    port: int | None = None,
    default_max_in_flight: int | None = None,
    pool_nodes: list[dict[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    return start_driver_runtime_control_server(
        host=host,
        port=port,
        default_max_in_flight=default_max_in_flight,
        pool_nodes=pool_nodes,
    )


def start_driver_runtime_control_server(
    *,
    host: str = "0.0.0.0",
    port: int | None = None,
    default_max_in_flight: int | None = None,
    pool_nodes: list[dict[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    resolved_port = (
        int(os.getenv("AGENTCICD_RATE_LIMITER_PORT", str(_DEFAULT_DRIVER_PORT)))
        if port is None
        else int(port)
    )
    rate_manager = DriverLeaseManager(runtime_max_in_flight(default_max_in_flight))
    pool_manager = DriverPoolLeaseManager()
    if pool_nodes:
        DriverPoolLeaseManager.validate_pool_nodes([dict(node) for node in pool_nodes])
    for node in pool_nodes or []:
        pool_manager.register_node(
            pool_name=str(node.get("pool_name") or ""),
            pool_kind=str(node.get("pool_kind") or ""),
            node_id=str(node.get("node_id") or ""),
            address=str(node.get("address") or ""),
            capacity=int(node.get("capacity") or 1),
            metadata=dict(node.get("metadata") or {}),
            heartbeat_ttl_seconds=float(node.get("heartbeat_ttl_seconds") or 3600.0),
        )
    handler_cls = _handler_for(rate_manager, pool_manager)
    server = ThreadingHTTPServer((host, resolved_port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, name="agentcicd-runtime-control-driver", daemon=True)
    thread.start()
    return server


def _node_fixture_ids(node: dict[str, Any]) -> set[str]:
    metadata = dict(node.get("metadata") or {})
    fixture_ids = metadata.get("fixture_ids")
    if isinstance(fixture_ids, list):
        return {str(item).strip() for item in fixture_ids if str(item).strip()}
    return set()


def _node_can_execute_fixture(node: dict[str, Any], fixture_id: str) -> bool:
    normalized_fixture_id = str(fixture_id or "").strip()
    if not normalized_fixture_id:
        return True
    return normalized_fixture_id in _node_fixture_ids(node)


def _node_preference_key(node: dict[str, Any]) -> tuple[str, str, int, float]:
    metadata = dict(node.get("metadata") or {})
    return (
        str(metadata.get("runtime_group_key") or ""),
        str(metadata.get("image_ref") or metadata.get("worker_image_ref") or ""),
        int(node.get("_leased") or 0),
        float(node.get("registered_at") or 0),
    )


# Private HTTP transport/server helpers.


def _handler_for(manager: DriverLeaseManager, pool_manager: DriverPoolLeaseManager | None = None) -> type[BaseHTTPRequestHandler]:
    pool_manager = pool_manager or DriverPoolLeaseManager()

    class RateLimitHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.rstrip("/")
            if path == "/rate-limit/status":
                self._write_json(manager.snapshot())
                return
            if path == "/pools/snapshot":
                self._write_json(pool_manager.snapshot())
                return
            if path not in {"/rate-limit/status", "/pools/snapshot"}:
                self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._read_json()
                path = self.path.rstrip("/")
                if path == "/rate-limit/acquire":
                    max_in_flight = payload.get("max_in_flight")
                    if max_in_flight is not None:
                        manager.configure(
                            str(payload.get("key") or _DEFAULT_LIMITER_KEY),
                            int(max_in_flight),
                        )
                    lease = manager.acquire(
                        key=str(payload.get("key") or _DEFAULT_LIMITER_KEY),
                        permits=int(payload.get("permits") or 1),
                        lease_ttl_seconds=float(
                            payload.get("lease_ttl_seconds") or _DEFAULT_LEASE_TTL_SECONDS
                        ),
                        wait_timeout_seconds=float(
                            payload.get("wait_timeout_seconds") or _DEFAULT_WAIT_TIMEOUT_SECONDS
                        ),
                        try_only=bool(payload.get("try_only")),
                    )
                    self._write_json(
                        {
                            "limiter_name": lease.limiter_name,
                            "key": lease.key,
                            "lease_id": lease.lease_id,
                            "permits": lease.permits,
                        }
                    )
                    return
                if path == "/rate-limit/release":
                    released = manager.release(str(payload.get("lease_id") or ""))
                    self._write_json({"released": released})
                    return
                if path == "/rate-limit/renew":
                    renewed = manager.renew(
                        str(payload.get("lease_id") or ""),
                        lease_ttl_seconds=float(payload.get("lease_ttl_seconds") or _DEFAULT_LEASE_TTL_SECONDS),
                    )
                    self._write_json({"renewed": renewed})
                    return
                if path == "/rate-limit/configure":
                    manager.configure(
                        str(payload.get("key") or _DEFAULT_LIMITER_KEY),
                        int(payload.get("max_in_flight") or manager.default_max_in_flight),
                    )
                    self._write_json({"configured": True})
                    return
                if path == "/pools/nodes/register":
                    node = pool_manager.register_node(
                        pool_name=str(payload.get("pool_name") or ""),
                        pool_kind=str(payload.get("pool_kind") or ""),
                        node_id=str(payload.get("node_id") or ""),
                        address=str(payload.get("address") or ""),
                        capacity=int(payload.get("capacity") or 1),
                        metadata=dict(payload.get("metadata") or {}),
                        heartbeat_ttl_seconds=float(payload.get("heartbeat_ttl_seconds") or 60.0),
                    )
                    self._write_json({"registered": True, "node": node})
                    return
                if path.startswith("/pools/nodes/") and path.endswith("/heartbeat"):
                    node_id = path.removeprefix("/pools/nodes/").removesuffix("/heartbeat").strip("/")
                    self._write_json(
                        {
                            "renewed": pool_manager.heartbeat_node(
                                node_id,
                                heartbeat_ttl_seconds=float(payload.get("heartbeat_ttl_seconds") or 60.0),
                            )
                        }
                    )
                    return
                if path == "/pools/leases/acquire":
                    try:
                        lease = pool_manager.acquire(
                            pool_name=str(payload.get("pool_name") or ""),
                            pool_kind=str(payload.get("pool_kind") or "session"),
                            request_id=str(payload.get("request_id") or ""),
                            executor_id=str(payload.get("executor_id") or ""),
                            fixture_id=str(payload.get("fixture_id") or ""),
                            lease_ttl_seconds=float(payload.get("lease_ttl_seconds") or _DEFAULT_LEASE_TTL_SECONDS),
                            wait_timeout_seconds=float(
                                payload.get("wait_timeout_seconds") or _DEFAULT_WAIT_TIMEOUT_SECONDS
                            ),
                            synthetic_address=str(payload.get("synthetic_address") or "") or None,
                            try_only=bool(payload.get("try_only")),
                        )
                    except TimeoutError as exc:
                        raise PoolLeaseUnavailable(str(exc)) from exc
                    self._write_json(
                        {
                            "lease_id": lease.lease_id,
                            "pool_name": lease.pool_name,
                            "pool_kind": lease.pool_kind,
                            "node_id": lease.node_id,
                            "manager_id": lease.manager_id,
                            "worker_slot_id": lease.worker_slot_id,
                            "generation": lease.generation,
                            "address": lease.address,
                            "expires_at": lease.expires_at,
                            "request_id": lease.request_id,
                            "fixture_id": lease.fixture_id,
                            "lease_decision": lease.lease_decision,
                        }
                    )
                    return
                if path == "/pools/leases/validate":
                    valid, reason = pool_manager.validate_lease(
                        lease_id=str(payload.get("lease_id") or ""),
                        pool_name=str(payload.get("pool_name") or ""),
                        pool_kind=str(payload.get("pool_kind") or ""),
                        fixture_id=str(payload.get("fixture_id") or ""),
                        manager_id=str(payload.get("manager_id") or ""),
                        worker_slot_id=str(payload.get("worker_slot_id") or ""),
                        generation=int(payload.get("generation") or 0),
                    )
                    self._write_json({"valid": valid, "reason": reason})
                    return
                if path.startswith("/pools/leases/") and path.endswith("/heartbeat"):
                    lease_id = path.removeprefix("/pools/leases/").removesuffix("/heartbeat").strip("/")
                    renewed = pool_manager.renew_lease(
                        lease_id,
                        lease_ttl_seconds=float(payload.get("lease_ttl_seconds") or _DEFAULT_LEASE_TTL_SECONDS),
                    )
                    self._write_json({"renewed": renewed})
                    return
                if path.startswith("/pools/leases/") and path.endswith("/return"):
                    lease_id = path.removeprefix("/pools/leases/").removesuffix("/return").strip("/")
                    returned = pool_manager.return_lease(
                        lease_id,
                        terminal_status=str(payload.get("terminal_status") or "completed"),
                        node_disposition=str(payload.get("node_disposition") or "available"),
                    )
                    self._write_json({"returned": returned})
                    return
                self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except RateLimitUnavailable as exc:
                self._write_json(
                    {"error": str(exc), "code": "RATE_LIMIT_UNAVAILABLE"},
                    status=HTTPStatus.CONFLICT,
                )
            except PoolLeaseUnavailable as exc:
                self._write_json(
                    {"error": str(exc), "code": "POOL_LEASE_UNAVAILABLE"},
                    status=HTTPStatus.CONFLICT,
                )
            except TimeoutError as exc:
                self._write_json(
                    {"error": str(exc), "code": "RATE_LIMIT_UNAVAILABLE"},
                    status=HTTPStatus.CONFLICT,
                )
            except Exception as exc:
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _write_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return RateLimitHandler


def _post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw_payload = response.read().decode("utf-8") or "{}"
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == HTTPStatus.CONFLICT:
            try:
                payload = json.loads(body or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and payload.get("code") == "POOL_LEASE_UNAVAILABLE":
                raise PoolLeaseUnavailable(str(payload.get("error") or "Pool lease unavailable")) from exc
            if isinstance(payload, dict) and payload.get("code") == "RATE_LIMIT_UNAVAILABLE":
                raise RateLimitUnavailable(str(payload.get("error") or "Rate limit unavailable")) from exc
        raise RuntimeError(f"Rate limiter request failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError("Rate limiter is unavailable") from exc
    response_payload = json.loads(raw_payload)
    if not isinstance(response_payload, dict):
        raise RuntimeError("Rate limiter returned a non-object response")
    return response_payload
