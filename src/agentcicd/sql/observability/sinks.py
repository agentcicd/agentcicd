from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class DiagnosticSink(Protocol):
    def emit(self, payload: dict[str, Any]) -> None:
        ...


class NoopDiagnosticSink:
    def emit(self, payload: dict[str, Any]) -> None:
        return None


class LocalJsonlSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_drop_empty(payload), separators=(",", ":"), sort_keys=True) + "\n")


class ObjectStoreJsonlSink:
    def __init__(self, store: Any, uri: str) -> None:
        self._store = store
        self._uri = uri

    def emit(self, payload: dict[str, Any]) -> None:
        try:
            existing = self._store.get_text(self._uri)
        except Exception:
            existing = ""
        self._store.put_text(
            self._uri,
            existing + json.dumps(_drop_empty(payload), separators=(",", ":"), sort_keys=True) + "\n",
            content_type="application/x-ndjson",
        )


class FanoutDiagnosticSink:
    def __init__(self, *sinks: DiagnosticSink) -> None:
        self._sinks = sinks

    def emit(self, payload: dict[str, Any]) -> None:
        for sink in self._sinks:
            sink.emit(payload)


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", {})}
