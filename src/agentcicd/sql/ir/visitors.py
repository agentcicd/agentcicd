from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any, Callable


def walk_ir(value: Any, visit: Callable[[Any], None]) -> None:
    visit(value)
    if is_dataclass(value):
        for field in fields(value):
            walk_ir(getattr(value, field.name), visit)
        return
    if isinstance(value, list):
        for item in value:
            walk_ir(item, visit)
        return
    if isinstance(value, dict):
        for item in value.values():
            walk_ir(item, visit)
