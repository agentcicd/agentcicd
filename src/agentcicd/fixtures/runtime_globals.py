from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol


class EnvironmentResolver(Protocol):
    def resolve(self, spec: Any) -> Any:
        ...


class SecretResolver(Protocol):
    def get(self, secret_id: str) -> str:
        ...


class Tracer(Protocol):
    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        ...


class UnboundRuntimeGlobal(RuntimeError):
    pass


class _UnboundSecrets:
    def get(self, secret_id: str) -> str:
        raise UnboundRuntimeGlobal("agentcicd.fixtures.secrets is not bound by the fixture runtime")


class _UnboundEnvs:
    def resolve(self, spec: Any) -> Any:
        raise UnboundRuntimeGlobal("agentcicd.fixtures.envs is not bound by the fixture runtime")


class _NoopTracing:
    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        yield None


@dataclass
class RuntimeGlobals:
    envs: EnvironmentResolver = _UnboundEnvs()
    secrets: SecretResolver = _UnboundSecrets()
    tracing: Tracer = _NoopTracing()
    log: logging.Logger = logging.getLogger("agentcicd.fixtures")


_GLOBALS = RuntimeGlobals()


class _EnvsProxy:
    def resolve(self, spec: Any) -> Any:
        return _GLOBALS.envs.resolve(spec)


class _SecretsProxy:
    def get(self, secret_id: str) -> str:
        return _GLOBALS.secrets.get(secret_id)


class _TracingProxy:
    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        with _GLOBALS.tracing.span(name, attributes):
            yield None


envs: EnvironmentResolver = _EnvsProxy()
secrets: SecretResolver = _SecretsProxy()
tracing: Tracer = _TracingProxy()
log: logging.Logger = _GLOBALS.log


def bind_runtime_globals(
    *,
    envs_impl: EnvironmentResolver | None = None,
    secrets_impl: SecretResolver | None = None,
    tracing_impl: Tracer | None = None,
    logger: logging.Logger | None = None,
) -> None:
    global log
    if envs_impl is not None:
        _GLOBALS.envs = envs_impl
    if secrets_impl is not None:
        _GLOBALS.secrets = secrets_impl
    if tracing_impl is not None:
        _GLOBALS.tracing = tracing_impl
    if logger is not None:
        _GLOBALS.log = logger
    log = _GLOBALS.log


def reset_runtime_globals() -> None:
    global log
    _GLOBALS.envs = _UnboundEnvs()
    _GLOBALS.secrets = _UnboundSecrets()
    _GLOBALS.tracing = _NoopTracing()
    _GLOBALS.log = logging.getLogger("agentcicd.fixtures")
    log = _GLOBALS.log
