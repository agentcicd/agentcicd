from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def pretty_ir(value: Any) -> str:
    return _pretty(value, indent=0)


def _pretty(value: Any, indent: int) -> str:
    prefix = "  " * indent
    if is_dataclass(value):
        payload = asdict(value)
        lines = [f"{prefix}{value.__class__.__name__}("]
        for key, item in payload.items():
            lines.append(f"{prefix}  {key}={_pretty(item, indent + 1).lstrip()}")
        lines.append(f"{prefix})")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{prefix}[]"
        lines = [f"{prefix}["]
        for item in value:
            lines.append(_pretty(item, indent + 1))
        lines.append(f"{prefix}]")
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return f"{prefix}{{}}"
        lines = [f"{prefix}{{"]
        for key, item in value.items():
            lines.append(f"{prefix}  {key!r}: {_pretty(item, indent + 1).lstrip()}")
        lines.append(f"{prefix}}}")
        return "\n".join(lines)
    return f"{prefix}{value!r}"
