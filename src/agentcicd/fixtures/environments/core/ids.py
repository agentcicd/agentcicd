from __future__ import annotations

from itertools import count
from threading import Lock


class StableIdFactory:
    def __init__(self, prefix: str) -> None:
        if not prefix:
            raise ValueError("prefix is required")
        self._prefix = prefix
        self._counter = count(1)
        self._lock = Lock()

    def next_id(self) -> str:
        with self._lock:
            value = next(self._counter)
        return f"{self._prefix}.{value}"
