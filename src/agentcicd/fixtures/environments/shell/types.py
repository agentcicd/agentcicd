from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agentcicd.fixtures.environments.core.lifecycle import BaseEnvironmentSetupSpec, TeardownResult
from agentcicd.fixtures.environments.core.policies import ResourceLimits


@dataclass(frozen=True)
class EnvironmentVariable:
    name: str
    value: str


@dataclass(frozen=True)
class ShellPolicy:
    allowed_commands: tuple[str, ...] = ()
    blocked_commands: tuple[str, ...] = (
        "sudo",
        "su",
        "shutdown",
        "reboot",
    )
    allow_network: bool = False
    allow_background_processes: bool = True
    max_processes: int = 8
    timeout_seconds: float = 60.0
    max_stdout_bytes: int = 2_000_000
    max_stderr_bytes: int = 2_000_000
    env_allowlist: tuple[str, ...] = ()
    path_allowlist: tuple[str, ...] = ()
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass(frozen=True)
class ShellSetupSpec(BaseEnvironmentSetupSpec):
    cwd: str = "."
    env: tuple[EnvironmentVariable, ...] = ()
    policy: ShellPolicy = field(default_factory=ShellPolicy)


@dataclass(frozen=True)
class ShellCommand:
    argv: tuple[str, ...]
    cwd: str | None = None
    env: tuple[EnvironmentVariable, ...] = ()
    stdin: str | bytes | None = None
    timeout_seconds: float | None = None
    capture_stdout: bool = True
    capture_stderr: bool = True


@dataclass(frozen=True)
class ResourceUsage:
    user_cpu_seconds: float | None = None
    system_cpu_seconds: float | None = None
    max_rss_bytes: int | None = None


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    signal: str | None
    timed_out: bool
    duration_ms: int
    stdout: bytes | None
    stderr: bytes | None
    output_truncated: bool
    resource_usage: ResourceUsage | None = None


@dataclass(frozen=True)
class ProcessHandle:
    process_id: str
    argv: tuple[str, ...]
    cwd: str


ProcessSignal = Literal["terminate", "kill", "interrupt"]


@dataclass(frozen=True)
class ProcessSummary:
    process_id: str
    argv: tuple[str, ...]
    cwd: str
    returncode: int | None


@dataclass(frozen=True)
class ShellTeardownResult(TeardownResult):
    terminated_processes: tuple[str, ...] = ()
