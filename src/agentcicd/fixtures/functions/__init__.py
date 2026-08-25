from __future__ import annotations

import asyncio
import builtins
import inspect
import importlib
import json
import os
from functools import lru_cache
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
import pyarrow as pa

from agentcicd.fixtures._attrs import callable_attr, read_attr
from agentcicd.fixtures.core.function import AsyncRowFunction, RowFunction
from agentcicd.fixtures.core.runtime_control import PoolLease, runtime_pool_lease
from agentcicd.fixtures.core.tracing import wrap_runtime_traced_callable
from agentcicd.fixtures.core.types import FType
from agentcicd.fixtures.core.udf import Udf
from agentcicd.fixtures.functions.utils.runtime_context import RuntimeResolutionContext, _context_path_from_env, _load_context

_BUILTIN_MODULES = (
    "agentcicd.fixtures.functions.a2a",
    "agentcicd.fixtures.functions.data",
    "agentcicd.fixtures.functions.objectstore",
    "agentcicd.fixtures.functions.zip",
    "agentcicd.fixtures.functions.elo",
    "agentcicd.fixtures.functions.http",
    "agentcicd.fixtures.functions.llm_chat",
    "agentcicd.fixtures.functions.llm_responses",
    "agentcicd.fixtures.functions.ragas",
    "agentcicd.fixtures.functions.string",
    "agentcicd.fixtures.functions.tool",
    "agentcicd.fixtures.functions.trajectory",
    "agentcicd.fixtures.functions.simple_agent",
    "agentcicd.fixtures.functions.simulators",
    "agentcicd.fixtures.functions.agent_harness",
    "agentcicd.fixtures.functions.environment_aliases",
)
_IMPLEMENTATION_PREFIXES = ("agentcicd.",)
_BUILTINS_LOADED = False


def load_builtin_udfs() -> dict[str, type[Udf]]:
    _load_builtin_modules()
    discovered: dict[str, type[Udf]] = {}
    for udf_cls in _iter_udf_subclasses(Udf):
        udf_name = read_attr(udf_cls, "_udf_name", None)
        if not udf_name:
            continue
        name = _canonical_name(str(udf_name))
        if name in discovered and discovered[name] is not udf_cls:
            raise ValueError(f"UDF with name '{name}' is already registered")
        discovered[name] = udf_cls
    return discovered


def udf(name: str) -> Callable[..., Any]:
    """Return a cached Python callable for a AgentCICD UDF or runtime fixture."""
    return _cached_udf(_canonical_name(name))


@lru_cache(maxsize=256)
def _cached_udf(name: str) -> Callable[..., Any]:
    udf_cls = _find_udf(name)
    if udf_cls is not None:
        return wrap_runtime_traced_callable(
            _build_builtin_callable(name, udf_cls),
            span_name=f"udf.{name}",
            attributes={"udf_name": name},
        )
    runtime_fixture = _find_runtime_fixture(name)
    if runtime_fixture is not None:
        if _is_current_worker_fixture(runtime_fixture):
            return wrap_runtime_traced_callable(
                _build_local_runtime_fixture_callable(name, runtime_fixture),
                span_name=f"udf.{name}",
                attributes={"udf_name": name, "dispatch": "local_nested"},
            )
        return wrap_runtime_traced_callable(
            _build_remote_fixture_callable(name, runtime_fixture),
            span_name=f"udf.{name}",
            attributes={"udf_name": name},
        )
    raise ValueError(f"Unknown AgentCICD UDF '{name}'")


def _build_builtin_callable(name: str, udf_cls: type[Udf]) -> Callable[..., Any]:
    udf_instance = udf_cls()
    function_instance = _instantiate_function(udf_instance.function())
    transform = callable_attr(function_instance, "transform")
    if isinstance(function_instance, AsyncRowFunction) and callable(transform):
        async def _invoke_async(*args: Any, **kwargs: Any) -> Any:
            return _json_safe(await transform(*args, **kwargs))

        return _invoke_async
    if isinstance(function_instance, RowFunction) and callable(transform):
        def _invoke_sync(*args: Any, **kwargs: Any) -> Any:
            return _json_safe(transform(*args, **kwargs))

        return _invoke_sync

    param_names = tuple(parameter.name for parameter in udf_instance.signature())

    def _invoke(*args: Any, **kwargs: Any) -> Any:
        if len(args) > len(param_names):
            raise TypeError(f"{name} expected at most {len(param_names)} positional arguments")
        values = dict(builtins.zip(param_names, args))
        for key, value in kwargs.items():
            if key in values:
                raise TypeError(f"{name} got multiple values for argument '{key}'")
            values[key] = value
        ordered = [values.get(parameter) for parameter in param_names]
        return _execute_udf(udf_instance, ordered)

    return _invoke


@lru_cache(maxsize=1)
def _runtime_context() -> RuntimeResolutionContext:
    return RuntimeResolutionContext.from_environment()


def _instantiate_function(function_cls: type[Any]) -> Any:
    signature = inspect.signature(function_cls)
    if "runtime_context" in signature.parameters:
        return function_cls(runtime_context=_runtime_context())
    return function_cls()


def _find_udf(name: str) -> type[Udf] | None:
    _load_builtin_modules()
    normalized = _canonical_name(name)
    for udf_cls in _iter_udf_subclasses(Udf):
        udf_name = read_attr(udf_cls, "_udf_name", None)
        if not udf_name:
            continue
        if _canonical_name(str(udf_name)) == normalized:
            return udf_cls
    return None


def _find_runtime_fixture(name: str) -> dict[str, Any] | None:
    normalized = _canonical_name(name).lower()
    context = _runtime_payload()
    for item in _runtime_fixture_items(context):
        keys = {
            _canonical_name(str(item.get(field) or "")).lower()
            for field in ("call_name", "name", "id", "runtime_alias")
            if str(item.get(field) or "").strip()
        }
        if normalized in keys:
            return item
    return None


@lru_cache(maxsize=1)
def _runtime_payload() -> dict[str, Any]:
    payload = _load_context(_context_path_from_env())
    return dict(payload) if isinstance(payload, dict) else {}


def _runtime_fixture_items(context: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw_fixtures = context.get("fixtures")
    if isinstance(raw_fixtures, list):
        items.extend(dict(item) for item in raw_fixtures if isinstance(item, dict))
    for key in ("fixtures_by_id", "fixtures_by_name", "fixtures_by_call_name"):
        raw_mapping = context.get(key)
        if isinstance(raw_mapping, dict):
            items.extend(dict(item) for item in raw_mapping.values() if isinstance(item, dict))
    return items


def _build_remote_fixture_callable(name: str, fixture: dict[str, Any]) -> Callable[..., Any]:
    base_url = str(fixture.get("base_url") or "").strip().rstrip("/")
    invoke_path = str(fixture.get("invoke_path") or "").strip()
    if not base_url or not invoke_path:
        raise ValueError(f"Runtime fixture '{name}' is missing an invoke endpoint")
    param_names = _fixture_param_names(fixture)
    is_async = bool(fixture.get("async"))

    def _argument_payload(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) > len(param_names):
            raise TypeError(f"{name} expected at most {len(param_names)} positional arguments")
        values = dict(builtins.zip(param_names, args))
        for key, value in kwargs.items():
            if key in values:
                raise TypeError(f"{name} got multiple values for argument '{key}'")
            values[key] = value
        return values

    if is_async:
        async def _invoke_async(*args: Any, **kwargs: Any) -> Any:
            payload = _argument_payload(args, kwargs)
            return await asyncio.to_thread(_invoke_remote_fixture_for_payload, base_url, invoke_path, payload, name, fixture)

        return _invoke_async

    def _invoke_sync(*args: Any, **kwargs: Any) -> Any:
        payload = _argument_payload(args, kwargs)
        return _invoke_remote_fixture_for_payload(base_url, invoke_path, payload, name, fixture)

    return _invoke_sync


def _is_current_worker_fixture(fixture: dict[str, Any]) -> bool:
    fixture_id = str(fixture.get("id") or "").strip()
    if not fixture_id:
        return False
    group_fixture_ids = _json_env_set("AGENTCICD_FUNCTION_GROUP_FIXTURE_IDS")
    if fixture_id not in group_fixture_ids:
        return False
    return bool(_local_function_name(fixture))


def _build_local_runtime_fixture_callable(name: str, fixture: dict[str, Any]) -> Callable[..., Any]:
    param_names = _fixture_param_names(fixture)
    function_name = _local_function_name(fixture)
    if not function_name:
        raise ValueError(f"Runtime fixture '{name}' is not available for local dispatch")

    def _argument_payload(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) > len(param_names):
            raise TypeError(f"{name} expected at most {len(param_names)} positional arguments")
        values = dict(builtins.zip(param_names, args))
        for key, value in kwargs.items():
            if key in values:
                raise TypeError(f"{name} got multiple values for argument '{key}'")
            values[key] = value
        return values

    async def _invoke_async(*args: Any, **kwargs: Any) -> Any:
        payload = _argument_payload(args, kwargs)
        return await _invoke_local_runtime_fixture(function_name, payload)

    def _invoke_sync(*args: Any, **kwargs: Any) -> Any:
        payload = _argument_payload(args, kwargs)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            return _invoke_async(*args, **kwargs)
        return asyncio.run(_invoke_local_runtime_fixture(function_name, payload))

    return _invoke_async if bool(fixture.get("async")) else _invoke_sync


async def _invoke_local_runtime_fixture(function_name: str, payload: dict[str, Any]) -> Any:
    try:
        from agentcicd.sandbox.function_runner import invoke_function
    except Exception as exc:
        raise RuntimeError("Local nested fixture dispatch requires agentcicd.sandbox.function_runner") from exc
    return _json_safe(await invoke_function(function_name, payload))


def _local_function_name(fixture: dict[str, Any]) -> str:
    for key in ("entrypoint_name", "runtime_alias"):
        value = str(fixture.get(key) or "").strip()
        if value:
            return value
    call_name = str(fixture.get("call_name") or fixture.get("name") or "").strip()
    return call_name.split(".")[-1] if call_name else ""


def _json_env_set(name: str) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except Exception:
        return {item.strip() for item in raw.split(",") if item.strip()}
    if isinstance(parsed, list):
        return {str(item).strip() for item in parsed if str(item).strip()}
    return set()


def _fixture_param_names(fixture: dict[str, Any]) -> tuple[str, ...]:
    signature = fixture.get("signature")
    if not isinstance(signature, dict):
        return ()
    parameters = signature.get("parameters")
    if not isinstance(parameters, list):
        return ()
    return tuple(
        str(item.get("name") or "").strip()
        for item in parameters
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    )


def _invoke_remote_fixture_for_payload(
    base_url: str,
    invoke_path: str,
    args: dict[str, Any],
    name: str,
    fixture: dict[str, Any],
) -> Any:
    pool_payload = _remote_fixture_pool_payload(args)
    fallback_address = base_url
    fixture_id = str(fixture.get("id") or "").strip()
    with runtime_pool_lease(pool_payload, fallback_address=fallback_address, fixture_id=fixture_id) as lease:
        selected_base_url = lease.address if lease is not None and lease.address else fallback_address
        return _invoke_remote_fixture(selected_base_url, invoke_path, args, name, lease)


def _remote_fixture_pool_payload(args: dict[str, Any]) -> dict[str, Any] | None:
    value = args.get("pool")
    if isinstance(value, dict):
        return value
    return None


def _lease_payload(lease: PoolLease | None) -> dict[str, Any] | None:
    if lease is None:
        return None
    return {
        "lease_id": lease.lease_id,
        "pool_name": lease.pool_name,
        "pool_kind": lease.pool_kind,
        "node_id": lease.node_id,
        "manager_id": lease.manager_id,
        "worker_slot_id": lease.worker_slot_id,
        "generation": lease.generation if lease.generation is not None else 1,
        "address": lease.address,
        "request_id": lease.request_id,
        "fixture_id": lease.fixture_id,
    }


def _invoke_remote_fixture(
    base_url: str,
    invoke_path: str,
    args: dict[str, Any],
    name: str,
    lease: PoolLease | None = None,
) -> Any:
    body: dict[str, Any] = {"args": _json_safe(args)}
    lease_body = _lease_payload(lease)
    if lease_body:
        body["lease"] = lease_body
    request = Request(
        f"{base_url}{invoke_path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=900) as response:  # noqa: S310
            raw_payload = response.read().decode("utf-8") or "{}"
    except HTTPError as exc:
        detail = _read_http_error_body(exc)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Runtime fixture '{name}' failed with HTTP {exc.code}{suffix}") from exc
    except URLError as exc:
        raise RuntimeError(f"Runtime fixture '{name}' could not be reached") from exc
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict) or "result" not in payload:
        raise ValueError(f"Runtime fixture '{name}' returned invalid JSON")
    return _json_safe(payload.get("result"))


def _read_http_error_body(exc: HTTPError, *, limit: int = 2000) -> str:
    try:
        body = exc.read(limit + 1)
    except Exception:
        return ""
    if not body:
        return ""
    decoded = body[:limit].decode("utf-8", errors="replace").strip()
    if len(body) > limit:
        return f"{decoded}..."
    return decoded


def _load_builtin_modules() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    for module_name in _BUILTIN_MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError:
            continue
    _BUILTINS_LOADED = True


def _iter_udf_subclasses(base_cls: type[Udf]) -> list[type[Udf]]:
    discovered: list[type[Udf]] = []
    for subclass in base_cls.__subclasses__():
        discovered.append(subclass)
        discovered.extend(_iter_udf_subclasses(subclass))
    return discovered


def _canonical_name(name: str) -> str:
    normalized = name.strip()
    for prefix in _IMPLEMENTATION_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def _execute_udf(udf_instance: Udf, values: list[Any]) -> Any:
    function_instance = udf_instance.function()()
    function_type = udf_instance.ftype()
    if function_type in {FType.BATCH_FUNCTION, FType.ROW_EXPLODE_FUNCTION}:
        arrays = [pa.array([value]) for value in values]
        result_batches = list(function_instance(*arrays))
        if not result_batches:
            return None
        result_values = result_batches[0].to_pylist()
        return _json_safe(result_values[0] if result_values else None)
    if function_type == FType.AGGREGATE_FUNCTION:
        series = [pd.Series([value]) for value in values]
        return _json_safe(function_instance(*series))
    raise ValueError(f"Unsupported AgentCICD UDF function type: {function_type}")


def _json_safe(value: Any) -> Any:
    from agentcicd.fixtures.functions.simulators import EnvironmentSpecDict, McpSpecDict

    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump()
    if isinstance(value, EnvironmentSpecDict):
        return EnvironmentSpecDict({str(key): _json_safe(item) for key, item in value.items()})
    if isinstance(value, McpSpecDict):
        return McpSpecDict({str(key): _json_safe(item) for key, item in value.items()})
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


__all__ = ["udf"]
