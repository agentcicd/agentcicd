from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeErrorInfo:
    code: str
    message: str
    source: str
    path: str | None = None
    recoverable: bool = True
    cause_code: str | None = None
    cause_message: str | None = None
    details: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "path": self.path,
            "recoverable": self.recoverable,
            "cause_code": self.cause_code,
            "cause_message": self.cause_message,
            "details": self.details or {},
        }


def runtime_error_info(code: str, message: str, source: str, *, cause: Exception | None = None) -> dict[str, Any]:
    return RuntimeErrorInfo(
        code=code,
        message=message,
        source=source,
        cause_code=type(cause).__name__ if cause is not None else None,
        cause_message=str(cause) if cause is not None else None,
    ).to_dict()
