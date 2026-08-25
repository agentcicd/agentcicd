from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable


FUNCTION_MARKER = "__agentcicd_fixture_function__"


@dataclass(frozen=True)
class FunctionRegistration:
    callable_object: Callable[..., Any]
    name: str | None = None
    namespace: str | None = None
    requirements: tuple[str, ...] = ()
    manifest_entry: dict[str, Any] | None = None


@dataclass(frozen=True)
class EnvironmentRegistration:
    class_object: type
    name: str | None = None
    namespace: str | None = None
    requirements: tuple[str, ...] = ()
    manifest_entry: dict[str, Any] | None = None


@dataclass
class FixtureRegistry:
    functions: list[FunctionRegistration] = field(default_factory=list)
    environments: list[EnvironmentRegistration] = field(default_factory=list)

    def register_function(self, registration: FunctionRegistration) -> None:
        for existing in self.functions:
            if existing.callable_object is registration.callable_object:
                return
        self.functions.append(registration)

    def register_environment(self, registration: EnvironmentRegistration) -> None:
        for existing in self.environments:
            if existing.class_object is registration.class_object:
                return
        self.environments.append(registration)


REGISTRY = FixtureRegistry()


def function(
    target: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    namespace: str | None = None,
    requirements: tuple[str, ...] | list[str] = (),
    manifest_entry: dict[str, Any] | None = None,
    registry: FixtureRegistry | None = None,
) -> Callable[..., Any]:
    target_registry = registry or REGISTRY

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.isfunction(func):
            raise TypeError("@function can only decorate Python functions")
        func.__dict__[FUNCTION_MARKER] = True
        if "." not in func.__qualname__:
            target_registry.register_function(
                FunctionRegistration(
                    callable_object=func,
                    name=name,
                    namespace=namespace,
                    requirements=tuple(requirements),
                    manifest_entry=manifest_entry,
                )
            )
        return func

    if target is None:
        return decorate
    return decorate(target)


def environment(
    target: type | None = None,
    *,
    name: str | None = None,
    namespace: str | None = None,
    requirements: tuple[str, ...] | list[str] = (),
    manifest_entry: dict[str, Any] | None = None,
    registry: FixtureRegistry | None = None,
) -> type:
    target_registry = registry or REGISTRY

    def decorate(cls: type) -> type:
        if not inspect.isclass(cls):
            raise TypeError("@environment can only decorate classes")
        _validate_environment_methods_are_functions(cls)
        target_registry.register_environment(
            EnvironmentRegistration(
                class_object=cls,
                name=name,
                namespace=namespace,
                requirements=tuple(requirements),
                manifest_entry=manifest_entry,
            )
        )
        return cls

    if target is None:
        return decorate
    return decorate(target)


def is_function_decorated(value: Any) -> bool:
    try:
        return bool(vars(value).get(FUNCTION_MARKER, False))
    except TypeError:
        return False


def _validate_environment_methods_are_functions(cls: type) -> None:
    for method_name, raw_value in cls.__dict__.items():
        if method_name.startswith("_"):
            continue
        value = raw_value
        if isinstance(raw_value, (staticmethod, classmethod)):
            value = raw_value.__func__
        if not callable(value):
            continue
        if not is_function_decorated(value):
            raise TypeError(
                f"Environment {cls.__name__}.{method_name} must be decorated with @function"
            )


def clear_registry() -> None:
    REGISTRY.functions.clear()
    REGISTRY.environments.clear()
