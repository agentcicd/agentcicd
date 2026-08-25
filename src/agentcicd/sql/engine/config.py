from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable


ObjectStoreFactory = Callable[[], Any]


ENGINE_ENV_ADAPTER_EXCEPTIONS = {
    "engine.runner": "CLI/Spark app adapter; converts process env into typed runtime services.",
    "engine.progress_reporter": "Backward-compatible adapter for AGENTCICD_PROGRESS_EVENTS_URI when no config is supplied.",
    "engine.backends.spark.session": "Spark image adapter for Hadoop/S3A credentials.",
    "engine.backends.spark.stage_artifacts": "Spark artifact adapter for run/reuse env supplied by DP worker.",
    "engine.backends.spark.debug_streams": "Spark artifact adapter for run object URI supplied by DP worker.",
    "engine.backends.spark.reuse": "Spark reuse adapter for previous/current run object URIs.",
    "engine.publication_store": "DP annotation/publish HTTP adapter.",
    "engine.source_loader": "CP dataset download HTTP adapter.",
}


@dataclass(frozen=True)
class RateLimiterConfig:
    key: str = "default"
    max_in_flight: int | None = None


@dataclass(frozen=True)
class RunRuntimeConfig:
    """Typed run configuration for engine code.

    New core engine behavior should receive this object from an outer adapter instead
    of reading process environment directly. The small set of Spark-image and HTTP
    adapter exceptions is documented in ``ENGINE_ENV_ADAPTER_EXCEPTIONS`` because
    those modules still bridge existing DP worker environment contracts.
    """

    run_id: str | None = None
    attempt: int | None = None
    organization_id: str | None = None
    run_object_uri: str | None = None
    previous_run_object_uri: str | None = None
    progress_events_uri: str | None = None
    debug_enabled: bool = False
    rate_limiter: RateLimiterConfig = RateLimiterConfig()
    object_store_factory: ObjectStoreFactory | None = None

    @classmethod
    def from_env(cls) -> "RunRuntimeConfig":
        return cls(
            run_id=_optional_str("AGENTCICD_RUN_ID"),
            attempt=_optional_int("AGENTCICD_RUN_ATTEMPT"),
            organization_id=_optional_str("AGENTCICD_ORGANIZATION_ID"),
            run_object_uri=_optional_str("AGENTCICD_RUN_OBJECT_URI"),
            previous_run_object_uri=_optional_str("AGENTCICD_PREVIOUS_RUN_OBJECT_URI"),
            progress_events_uri=_optional_str("AGENTCICD_PROGRESS_EVENTS_URI"),
            debug_enabled=_truthy(os.getenv("AGENTCICD_DEBUG", "")),
        )


def _optional_str(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
