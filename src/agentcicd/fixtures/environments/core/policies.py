from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceLimits:
    max_cpu_seconds: float | None = None
    max_memory_bytes: int | None = None
    max_open_files: int | None = None
