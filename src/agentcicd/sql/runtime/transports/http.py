from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.request import Request


@dataclass(frozen=True)
class HttpRuntimeRequest:
    url: str
    args: dict[str, Any]
    timeout_seconds: int

    def body(self) -> bytes:
        return json.dumps({"args": self.args}).encode("utf-8")

    def to_urllib_request(self) -> Request:
        return Request(
            self.url,
            data=self.body(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )


def http_timeout_seconds(metadata: dict[str, object], *, default: int) -> int:
    raw_value = metadata.get("timeout_seconds")
    if raw_value is None:
        raw_value = metadata.get("http_timeout_seconds")
    try:
        timeout = int(raw_value) if raw_value is not None else default
    except (TypeError, ValueError):
        return default
    return timeout if timeout > 0 else default


def runtime_http_request(
    *,
    base_url: str,
    invoke_path: str,
    args: dict[str, Any],
    timeout_seconds: int,
) -> HttpRuntimeRequest:
    return HttpRuntimeRequest(
        url=f"{base_url.rstrip('/')}/{invoke_path.lstrip('/')}",
        args=args,
        timeout_seconds=timeout_seconds,
    )
