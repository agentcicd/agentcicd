from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeRateLimit:
    key: str
    max_in_flight: int | None


def rate_limit_payload(value: object) -> dict[str, object] | None:
    if isinstance(value, dict) and str(value.get("kind") or "").lower() == "ratelimit":
        return value
    return None


def limiter_from_control_values(
    values: list[Any],
    *,
    fallback_key: str = "default",
    default_max_in_flight: int | None = None,
) -> RuntimeRateLimit:
    for value in values:
        payload = rate_limit_payload(value)
        if payload is None:
            continue
        key = str(payload.get("key") or fallback_key).strip() or fallback_key
        raw_max = payload.get("max_in_flight")
        try:
            max_in_flight = int(raw_max) if raw_max is not None else default_max_in_flight
        except (TypeError, ValueError):
            max_in_flight = default_max_in_flight
        return RuntimeRateLimit(key=key, max_in_flight=max_in_flight)
    return RuntimeRateLimit(key=fallback_key, max_in_flight=default_max_in_flight)
