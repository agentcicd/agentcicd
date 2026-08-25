from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Iterator


class NoopRuntimeTrace:
    @contextmanager
    def span(self, _name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        yield None

    def event(self, _name: str, attributes: dict[str, Any] | None = None) -> None:
        return None


_NOOP_TRACE = NoopRuntimeTrace()
_current_runtime_trace: ContextVar[Any] = ContextVar("agentcicd_current_runtime_trace", default=_NOOP_TRACE)
_TRACED_CALLABLE_MARKER = "__agentcicd_runtime_traced_callable__"


def current_runtime_trace() -> Any:
    return _current_runtime_trace.get()


def runtime_trace_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and isinstance(value, (str, int, float, bool))
    }


@contextmanager
def runtime_trace_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    with current_runtime_trace().span(name, runtime_trace_attributes(attributes or {})):
        yield None


def runtime_trace_call_attributes(
    attributes: dict[str, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    return {
        **attributes,
        "arg_count": len(args),
        "kwarg_count": len(kwargs),
    }


def wrap_runtime_traced_callable(
    func: Callable[..., Any],
    *,
    span_name: str,
    attributes: dict[str, Any] | None = None,
) -> Callable[..., Any]:
    try:
        if vars(func).get(_TRACED_CALLABLE_MARKER):
            return func
    except TypeError:
        pass

    span_attributes = dict(attributes or {})
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with runtime_trace_span(span_name, runtime_trace_call_attributes(span_attributes, args, kwargs)):
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

        _async_wrapper.__dict__[_TRACED_CALLABLE_MARKER] = True
        return _async_wrapper

    @wraps(func)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with runtime_trace_span(span_name, runtime_trace_call_attributes(span_attributes, args, kwargs)):
            return func(*args, **kwargs)

    _sync_wrapper.__dict__[_TRACED_CALLABLE_MARKER] = True
    return _sync_wrapper


@contextmanager
def use_runtime_trace(trace: Any) -> Iterator[Any]:
    token = _current_runtime_trace.set(trace or _NOOP_TRACE)
    try:
        yield trace
    finally:
        _current_runtime_trace.reset(token)
