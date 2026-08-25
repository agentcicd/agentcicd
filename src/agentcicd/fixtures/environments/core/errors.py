from __future__ import annotations

from dataclasses import dataclass


class EnvironmentError(Exception):
    """Base class for environment-level failures."""


class PolicyViolation(EnvironmentError):
    """Raised when a typed environment policy denies an action."""


class EnvironmentTimeout(EnvironmentError):
    """Raised when infrastructure-level setup or teardown times out."""


class EnvironmentUnavailable(EnvironmentError):
    """Raised when an adapter dependency is unavailable."""


class ActionFailed(EnvironmentError):
    """Raised when an environment action cannot be completed."""


class OutputLimitExceeded(EnvironmentError):
    """Raised when output exceeds an enforced hard limit."""


@dataclass(frozen=True)
class EnvironmentErrorInfo:
    code: str
    message: str
    retryable: bool = False


def error_info(exc: Exception, *, code: str | None = None, retryable: bool = False) -> EnvironmentErrorInfo:
    error_code = code
    if error_code is None:
        error_code = exc.__class__.__name__.lower()
    return EnvironmentErrorInfo(code=error_code, message=str(exc), retryable=retryable)
