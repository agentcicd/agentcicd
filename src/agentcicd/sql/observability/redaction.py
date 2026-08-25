from __future__ import annotations

import json
from typing import Any


SECRET_KEY_PARTS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "credential",
    "cookie",
    "set-cookie",
)


def redacted_preview(value: Any, *, max_preview_bytes: int = 4096) -> Any:
    return _truncate(_redact(value), max_preview_bytes=max_preview_bytes)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_secret_key(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes", "size_bytes": len(value)}
    if hasattr(value, "asDict"):
        return _redact(value.asDict(recursive=True))
    return value


def _truncate(value: Any, *, max_preview_bytes: int) -> Any:
    if max_preview_bytes <= 0:
        return None
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= max_preview_bytes:
            return value
        return encoded[:max_preview_bytes].decode("utf-8", errors="ignore") + "...[truncated]"
    if isinstance(value, dict):
        return _truncate_json_like(value, max_preview_bytes=max_preview_bytes)
    if isinstance(value, list):
        return _truncate_json_like(value, max_preview_bytes=max_preview_bytes)
    return value


def _truncate_json_like(value: Any, *, max_preview_bytes: int) -> Any:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw.encode("utf-8")) <= max_preview_bytes:
        return value
    return raw.encode("utf-8")[:max_preview_bytes].decode("utf-8", errors="ignore") + "...[truncated]"


def _is_secret_key(key: str) -> bool:
    lowered = key.strip().lower().replace("-", "_")
    return any(part in lowered for part in SECRET_KEY_PARTS)
