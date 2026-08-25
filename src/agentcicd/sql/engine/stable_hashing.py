from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value: Mapping[str, Any], *, length: int = 32) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:length]
