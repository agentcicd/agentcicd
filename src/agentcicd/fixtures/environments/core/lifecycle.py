from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar


@dataclass(frozen=True)
class Label:
    key: str
    value: str


@dataclass(frozen=True)
class BaseEnvironmentSetupSpec:
    env_id: str
    session_id: str
    run_id: str | None = None
    task_id: str | None = None
    seed: int | None = None
    labels: tuple[Label, ...] = ()


@dataclass(frozen=True)
class TeardownReason:
    code: str
    message: str | None = None


@dataclass(frozen=True)
class TeardownResult:
    session_id: str
    env_id: str
    ok: bool
    reason: TeardownReason
    preserved_hashes: tuple[tuple[str, str], ...] = ()


class EnvironmentSession(Protocol):
    session_id: str
    env_id: str

    async def teardown(self, reason: TeardownReason) -> TeardownResult: ...


SetupSpecT = TypeVar("SetupSpecT", bound=BaseEnvironmentSetupSpec)
SessionT = TypeVar("SessionT", bound=EnvironmentSession)


class Environment(Protocol[SetupSpecT, SessionT]):
    async def setup(self, spec: SetupSpecT) -> SessionT: ...
