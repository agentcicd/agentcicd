from __future__ import annotations

from typing import Any


_MISSING = object()


def read_attr(value: Any, name: str, default: Any = _MISSING) -> Any:
    try:
        return object.__getattribute__(value, name)
    except AttributeError:
        if default is _MISSING:
            raise
        return default


def callable_attr(value: Any, name: str) -> Any | None:
    candidate = read_attr(value, name, None)
    if callable(candidate):
        return candidate
    return None


def type_display_name(value: Any) -> str:
    try:
        return str(object.__getattribute__(value, "__name__"))
    except AttributeError:
        return type(value).__name__
