from __future__ import annotations

import threading
from typing import Any


class SparkTableRegistry:
    """Thread-safe registry for materialized Spark table paths and schemas."""

    def __init__(self) -> None:
        self._paths: dict[str, str] = {}
        self._schemas: dict[str, Any] = {}
        self._lock = threading.RLock()

    def record(self, name: str, path: str, *, schema: Any | None = None) -> None:
        with self._lock:
            self._paths[name] = path
            if schema is not None:
                self._schemas[name] = schema

    def record_schema(self, name: str, schema: Any) -> None:
        with self._lock:
            self._schemas[name] = schema

    def snapshot(self) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        with self._lock:
            return list(self._paths.items()), dict(self._schemas)

    def entry(self, name: str) -> tuple[str | None, Any | None]:
        with self._lock:
            return self._paths.get(name), self._schemas.get(name)

    def schema(self, name: str) -> Any | None:
        with self._lock:
            return self._schemas.get(name)


class SparkTableRegistryMixin:
    def _record_known_table(self, name: str, path: str, *, schema: Any | None = None) -> None:
        self._table_registry.record(name, path, schema=schema)

    def _record_known_table_schema(self, name: str, schema: Any) -> None:
        self._table_registry.record_schema(name, schema)

    def _snapshot_known_tables(self) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        return self._table_registry.snapshot()

    def _known_table_entry(self, name: str) -> tuple[str | None, Any | None]:
        return self._table_registry.entry(name)

    def _known_table_schema(self, name: str) -> Any | None:
        return self._table_registry.schema(name)
