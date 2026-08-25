from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ReusableStageRegistry:
    """Tracks materialized stages that should be loaded from prior artifacts."""

    requested_tables: set[str] = field(default_factory=set)
    registered_tables: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "ReusableStageRegistry":
        return cls(requested_tables=_parse_table_names(os.getenv("AGENTCICD_COMPLETED_BATCH_TABLES", "")))

    @classmethod
    def from_table_names(cls, table_names: Iterable[str]) -> "ReusableStageRegistry":
        return cls(requested_tables=_normalize_table_names(table_names))

    def mark_registered(self, table_name: str) -> None:
        normalized = _normalize_table_name(table_name)
        if normalized:
            self.registered_tables.add(normalized)

    def should_skip_materialized_table(self, table_name: str) -> bool:
        normalized = _normalize_table_name(table_name)
        return bool(normalized and normalized in self.registered_tables)


def reusable_table_names_from_env() -> set[str]:
    return ReusableStageRegistry.from_env().requested_tables


def _parse_table_names(raw_value: str) -> set[str]:
    return _normalize_table_names(raw_value.split(","))


def _normalize_table_names(table_names: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for table_name in table_names:
        value = _normalize_table_name(table_name)
        if value:
            normalized.add(value)
    return normalized


def _normalize_table_name(table_name: str) -> str:
    return str(table_name or "").strip().lower()
