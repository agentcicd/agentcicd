from __future__ import annotations


class AgentCICDError(ValueError):
    """Base error for local AgentCICD project handling."""


class ProjectLoadError(AgentCICDError):
    """Raised when a local project folder cannot be loaded."""


class InputCoercionError(AgentCICDError):
    """Raised when inputs.yaml cannot be coerced to declared recipe inputs."""


class BackendNotSupportedError(AgentCICDError):
    """Raised when the selected backend is unavailable for local execution."""
