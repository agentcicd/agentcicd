from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


INSPECTION_SCHEMA_VERSION = "inspection-v1"
ArtifactSource = Literal["local", "live", "archive", "imported"]


@dataclass(frozen=True, slots=True)
class InspectionCapabilities:
    compare: bool = False
    rerun: bool = False
    cancel: bool = False
    annotate: bool = False
    open_external_resource: bool = False


@dataclass(frozen=True, slots=True)
class InspectionProject:
    id: str
    name: str
    source: ArtifactSource
    root_label: str | None = None


@dataclass(frozen=True, slots=True)
class InspectionResource:
    id: str
    name: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InspectionRun:
    id: str
    status: str
    started_at: str | None
    finished_at: str | None
    attempt: int
    source: ArtifactSource


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the stable protocol version to a JSON-safe response payload."""
    return {"schema_version": INSPECTION_SCHEMA_VERSION, **payload}


def record(value: object) -> dict[str, Any]:
    """Serialize the small inspection dataclasses without exposing implementation details."""
    return asdict(value)


def inspection_json_schema() -> dict[str, Any]:
    """Return the checked-in protocol schema used by the TypeScript viewer.

    The API deliberately uses envelopes for multiple resources.  This schema
    validates the invariant fields which every inspection response carries;
    endpoint-specific fields remain forward-compatible extension data.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://agentcicd.dev/schemas/inspection-v1.schema.json",
        "title": "AgentCICD inspection-v1 envelope",
        "type": "object",
        "required": ["schema_version"],
        "properties": {
            "schema_version": {"const": INSPECTION_SCHEMA_VERSION},
            "capabilities": {
                "type": "object",
                "properties": {
                    "compare": {"type": "boolean"},
                    "rerun": {"type": "boolean"},
                    "cancel": {"type": "boolean"},
                    "annotate": {"type": "boolean"},
                    "open_external_resource": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": True,
    }
