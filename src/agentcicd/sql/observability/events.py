from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DiagnosticEvent:
    event: str
    severity: str = "info"
    run_id: str | None = None
    attempt: int | str | None = None
    organization_id: str | None = None
    stage_name: str | None = None
    stage_kind: str | None = None
    table_name: str | None = None
    timestamp: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "timestamp": self.timestamp or datetime.utcnow().isoformat() + "Z",
            "event": self.event,
            "severity": self.severity,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "organization_id": self.organization_id,
            "stage_name": self.stage_name,
            "stage_kind": self.stage_kind,
            "table_name": self.table_name,
            "details": self.details,
        }
        return {key: value for key, value in payload.items() if value not in (None, "", {})}
