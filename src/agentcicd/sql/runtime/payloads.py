from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError


@dataclass(frozen=True)
class RemoteFunctionResponse:
    result: object
    trace_records: list[dict[str, Any]] | None = None
    trace_summary: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw_payload: str, *, runtime_alias: str) -> "RemoteFunctionResponse":
        try:
            response_payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Remote function '{runtime_alias}' returned invalid JSON") from exc
        if not isinstance(response_payload, dict) or "result" not in response_payload:
            raise ValueError(f"Invalid response from remote function '{runtime_alias}'")
        trace_records = response_payload.get("trace_records")
        trace_summary = response_payload.get("trace_summary")
        return cls(
            result=response_payload.get("result"),
            trace_records=trace_records if isinstance(trace_records, list) else None,
            trace_summary=trace_summary if isinstance(trace_summary, dict) else None,
        )


@dataclass(frozen=True)
class RemoteFunctionErrorPayload:
    text: str
    payload: dict[str, Any] | None = None

    @property
    def trace_records(self) -> list[dict[str, Any]] | None:
        records = (self.payload or {}).get("trace_records")
        return records if isinstance(records, list) else None

    @property
    def trace_summary(self) -> dict[str, Any] | None:
        summary = (self.payload or {}).get("trace_summary")
        return summary if isinstance(summary, dict) else None


def read_http_error_payload(exc: HTTPError, *, limit: int = 200_000) -> RemoteFunctionErrorPayload:
    try:
        body = exc.read(limit + 1)
    except Exception:
        return RemoteFunctionErrorPayload("")
    if not body:
        return RemoteFunctionErrorPayload("")
    decoded = body[:limit].decode("utf-8", errors="replace").strip()
    display = f"{decoded}..." if len(body) > limit else decoded
    payload = None
    try:
        parsed = json.loads(decoded)
        payload = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        payload = None
    return RemoteFunctionErrorPayload(display, payload)


def _read_http_error_body(exc: HTTPError, *, limit: int = 2000) -> str:
    try:
        body = exc.read(limit + 1)
    except Exception:
        return ""
    if not body:
        return ""
    decoded = body[:limit].decode("utf-8", errors="replace").strip()
    if len(body) > limit:
        return f"{decoded}..."
    return decoded

def _json_payload_value(value: Any) -> Any:
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return _json_payload_value(value.tolist())
        except Exception:
            pass
    to_json = getattr(value, "toJson", None)
    if callable(to_json):
        try:
            return json.loads(to_json())
        except Exception:
            return str(value)
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return str(value)
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    as_dict = getattr(value, "asDict", None)
    if callable(as_dict):
        return {
            str(key): _json_payload_value(item)
            for key, item in as_dict(recursive=True).items()
        }
    if isinstance(value, dict):
        return {str(key): _json_payload_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_payload_value(item) for item in value]
    return value

def _render_stub_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return str(value)
