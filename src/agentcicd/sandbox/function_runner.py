from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import runpy
import secrets
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, get_type_hints, runtime_checkable

from agentcicd.fixtures.environments.shell import ShellCommand
from agentcicd.fixtures.functions import objectstore

_REGISTRY: dict[str, Callable[..., Any]] = {}
_USER_GLOBALS: dict[str, Any] = {}
_CURRENT_ENVS: contextvars.ContextVar["_InvocationEnvs | None"] = contextvars.ContextVar("agentcicd_invocation_envs", default=None)
_CURRENT_SECRETS: contextvars.ContextVar["_InvocationSecrets | None"] = contextvars.ContextVar("agentcicd_invocation_secrets", default=None)
_FUNCTION_MARKER = "__agentcicd_fixture_function__"


@runtime_checkable
class _McpSpecProvider(Protocol):
    def to_mcp_spec(self) -> Any:
        ...


@runtime_checkable
class _AttachedMcpSetupProvider(Protocol):
    def setup_attached_mcps(self) -> Any:
        ...


class _EnvironmentSpecConfig:
    def __init__(self, payload: "_RuntimeEnvironmentSpecDict") -> None:
        self._payload = payload

    def add_mcp(self, key: str, spec: Any) -> "_RuntimeEnvironmentSpecDict":
        config = self._payload.setdefault("config", {})
        if not isinstance(config, dict):
            raise ValueError("environment spec config must be an object")
        mcps = config.setdefault("mcps", {})
        if not isinstance(mcps, dict):
            raise ValueError("environment config field 'mcps' must be a map")
        name = str(key or "").strip()
        if not name:
            raise ValueError("MCP server key is required")
        payload = spec.to_mcp_spec() if isinstance(spec, _McpSpecProvider) else spec
        serialized = _to_jsonable(dict(payload))
        serialized["name"] = name
        mcps[name] = serialized
        return self._payload


class _RuntimeEnvironmentSpecDict(dict[str, Any]):
    @property
    def config(self) -> _EnvironmentSpecConfig:
        return _EnvironmentSpecConfig(self)


class _RemoteRuntimeTrace:
    def __init__(self, context: dict[str, Any], emit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.trace_id = str(context.get("trace_id") or "")
        self.parent_span_id = str(context.get("parent_span_id") or "") or None
        self.parent_call_id = str(context.get("parent_call_id") or "") or None
        self._records: list[dict[str, Any]] = []
        self._record_index: dict[str, int] = {}
        self._span_stack: list[str] = []
        self._lock = threading.Lock()
        self._emit = emit

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        if not self.trace_id:
            yield None
            return
        span_id = secrets.token_hex(8)
        parent_span_id = self._span_stack[-1] if self._span_stack else self.parent_span_id
        started_at = time.perf_counter()
        started_at_iso = _utc_now()
        status = "ok"
        error_message = None
        record = {
            "record_type": "span",
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "call_id": f"rtcall_{secrets.token_hex(12)}",
            "name": name,
            "kind": "span",
            "status": "running",
            "started_at": started_at_iso,
            "attributes": _primitive_attributes(attributes or {}),
        }
        with self._lock:
            self._upsert_record_locked(_drop_none(record))
        self._emit_record(record)
        try:
            self._span_stack.append(span_id)
            yield None
        except Exception as exc:
            status = "error"
            error_message = str(exc)
            raise
        finally:
            if self._span_stack and self._span_stack[-1] == span_id:
                self._span_stack.pop()
            with self._lock:
                record["status"] = status
                record["duration_ms"] = int((time.perf_counter() - started_at) * 1000)
                if error_message:
                    record["error_message"] = error_message
                self._upsert_record_locked(_drop_none(record))
            self._emit_record(record)

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        if not self.trace_id:
            return
        with self._lock:
            record = {
                "record_type": "event",
                "trace_id": self.trace_id,
                "span_id": self.parent_span_id,
                "name": name,
                "timestamp": _utc_now(),
                "attributes": _primitive_attributes(attributes or {}),
            }
            self._records.append(record)
        self._emit_record(record)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._records]

    def _upsert_record_locked(self, record: dict[str, Any]) -> None:
        span_id = str(record.get("span_id") or "")
        if span_id and span_id in self._record_index:
            self._records[self._record_index[span_id]] = record
            return
        if span_id:
            self._record_index[span_id] = len(self._records)
        self._records.append(record)

    def _emit_record(self, record: dict[str, Any]) -> None:
        if self._emit is None:
            return
        self._emit(_drop_none(dict(record)))


@contextmanager
def _remote_runtime_trace_context(context: Any) -> Iterator[_RemoteRuntimeTrace | None]:
    if isinstance(context, _RemoteRuntimeTrace):
        try:
            from agentcicd.fixtures.core.tracing import use_runtime_trace
        except Exception:
            yield None
            return
        with use_runtime_trace(context):
            yield context
        return
    if not isinstance(context, dict):
        context = {}
    if not str(context.get("trace_id") or "").strip():
        context = {**context, "trace_id": f"fixture-{secrets.token_hex(16)}"}
    trace = _RemoteRuntimeTrace(context)
    try:
        from agentcicd.fixtures.core.tracing import use_runtime_trace
    except Exception:
        yield None
        return
    with use_runtime_trace(trace):
        yield trace


@contextmanager
def _remote_runtime_trace_jsonl_context(context: Any) -> Iterator[_RemoteRuntimeTrace | None]:
    if not isinstance(context, dict):
        context = {}
    if not str(context.get("trace_id") or "").strip():
        context = {**context, "trace_id": f"fixture-{secrets.token_hex(16)}"}

    def _emit(record: dict[str, Any]) -> None:
        _write_jsonl_frame({"type": "trace_record", "record": record})

    trace = _RemoteRuntimeTrace(context, emit=_emit)
    try:
        from agentcicd.fixtures.core.tracing import use_runtime_trace
    except Exception:
        yield None
        return
    with use_runtime_trace(trace):
        yield trace


def _primitive_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in attributes.items()
        if value is not None and isinstance(value, (str, int, float, bool))
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _write_jsonl_frame(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


@contextmanager
def _runtime_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    try:
        from agentcicd.fixtures.core.tracing import runtime_trace_span
    except Exception:
        yield None
        return
    with runtime_trace_span(name, attributes):
        yield None


def _environment_kind(spec: Any) -> str:
    if isinstance(spec, dict):
        return str(spec.get("kind") or spec.get("spec_type") or "environment")
    try:
        kind = object.__getattribute__(spec, "kind")
    except AttributeError:
        kind = None
    if kind:
        return str(kind)
    return type(spec).__name__


def _wrap_traced_callable(func: Callable[..., Any], *, span_name: str, attributes: dict[str, Any]) -> Callable[..., Any]:
    try:
        from agentcicd.fixtures.core.tracing import wrap_runtime_traced_callable
    except Exception:
        return func
    return wrap_runtime_traced_callable(func, span_name=span_name, attributes=attributes)


class _TracedEnvironmentHandle:
    def __init__(self, handle: Any, *, environment_kind: str) -> None:
        object.__setattr__(self, "_handle", handle)
        object.__setattr__(self, "_environment_kind", environment_kind)

    def __getattr__(self, name: str) -> Any:
        handle = object.__getattribute__(self, "_handle")
        try:
            value = type(handle).__getattribute__(handle, name)
        except AttributeError:
            value = type(handle).__getattr__(handle, name)
        if not callable(value) or name.startswith("_"):
            return value
        environment_kind = object.__getattribute__(self, "_environment_kind")
        return _wrap_traced_callable(
            value,
            span_name=f"{environment_kind}.{name}",
            attributes={"environment_kind": environment_kind, "method": name},
        )

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(object.__getattribute__(self, "_handle"), "__setattr__")(name, value)


class _GenericSchemaType:
    def __init__(self, name: str, args: tuple[Any, ...]) -> None:
        self.name = name
        self.args = args


class _SchemaGenericFactory:
    name: str

    def __class_getitem__(cls, item: Any) -> _GenericSchemaType:
        args = item if isinstance(item, tuple) else (item,)
        return _GenericSchemaType(cls.name, args)


class Str:
    pass


class Int:
    pass


class Float:
    pass


class Bool:
    pass


class Variant:
    pass


class SecretId:
    pass


class EnvSpec(_SchemaGenericFactory):
    name = "EnvSpec"


class Environment(_SchemaGenericFactory):
    name = "Environment"


class Session:
    def __init__(self, workspace_dir: str | os.PathLike[str]) -> None:
        self.workspace_dir = Path(workspace_dir)


class ShellEnv:
    pass


class AgentHarnessEnv:
    pass


class McpSpec:
    pass


class DirectoryEntry:
    pass


class Directory:
    pass


class Array(_SchemaGenericFactory):
    name = "Array"


class Map(_SchemaGenericFactory):
    name = "Map"


class Required(_SchemaGenericFactory):
    name = "Required"


class Optional(_SchemaGenericFactory):
    name = "Optional"


class BrowserSpec:
    pass


class ShellSpec:
    pass


class AgentHarnessSpec:
    pass


class McpHttpSpec:
    pass


class McpStdioSpec:
    pass


class McpPlaywrightSpec:
    pass


class NamedStruct:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update({name: values.get(name) for name in self.__annotations__})

    def to_dict(self) -> dict[str, Any]:
        return {name: _to_jsonable(self.__dict__.get(name)) for name in self.__annotations__}


class _InvocationEnvs:
    def __init__(self) -> None:
        self._registry: Any = None
        self._handles: list[Any] = []

    async def resolve(self, spec: Any) -> Any:
        from agentcicd.fixtures.functions.simulators import EnvironmentHandleRegistry, materialized_mcp_from_spec, _coerce_environment_specs

        if self._registry is None:
            self._registry = EnvironmentHandleRegistry()
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError as exc:
                raise ValueError("envs.resolve requires an envs.*.spec object or serialized envs.*.spec object") from exc
        if isinstance(spec, dict) and str(spec.get("spec_type") or "").strip().lower() == "mcp":
            handle = materialized_mcp_from_spec(spec)
            if handle.start_mode == "early":
                await handle.setup()
            if not any(existing is handle for existing in self._handles):
                self._handles.append(handle)
            return _TracedEnvironmentHandle(handle, environment_kind="mcp")
        specs = _coerce_environment_specs([spec])
        if len(specs) != 1:
            raise ValueError("envs.resolve requires exactly one envs.*.spec object")
        environment_kind = _environment_kind(specs[0])
        with _runtime_span("envs.resolve", {"environment_kind": environment_kind}):
            handle = self._registry.get_or_create(specs[0])
            if isinstance(handle, _AttachedMcpSetupProvider):
                result = handle.setup_attached_mcps()
                if inspect.isawaitable(result):
                    await result
        if not any(existing is handle for existing in self._handles):
            self._handles.append(handle)
        return _TracedEnvironmentHandle(handle, environment_kind=environment_kind)

    async def teardown_all(self) -> None:
        reason = type("Reason", (), {"code": "fixture_complete", "message": None})()
        for handle in reversed(self._handles):
            teardown = handle.__dict__.get("teardown") if hasattr(handle, "__dict__") else None
            if teardown is None:
                teardown_function = type(handle).__dict__.get("teardown")
                if callable(teardown_function):
                    def _bound_teardown(reason_arg: Any, teardown_function: Callable[..., Any] = teardown_function, handle: Any = handle) -> Any:
                        return teardown_function(handle, reason_arg)

                    teardown = _bound_teardown
            if callable(teardown):
                result = teardown(reason)
                if inspect.isawaitable(result):
                    await result
        self._handles.clear()


class _EnvsProxy:
    async def resolve(self, spec: Any) -> Any:
        resolver = _CURRENT_ENVS.get()
        if resolver is None:
            resolver = _InvocationEnvs()
            _CURRENT_ENVS.set(resolver)
        return await resolver.resolve(spec)


envs = _EnvsProxy()


class _InvocationSecrets:
    def __init__(self, records: Any = None) -> None:
        self._values: dict[str, str] = {}
        if isinstance(records, list):
            for item in records:
                if isinstance(item, dict):
                    secret_id = str(item.get("id") or item.get("secret_id") or "").strip()
                    value = item.get("value")
                    if secret_id and isinstance(value, str):
                        self._values[secret_id] = value

    @classmethod
    def from_runtime_context(cls) -> "_InvocationSecrets":
        context = _runner_runtime_payload()
        records = context.get("secrets")
        return cls(records)

    def merged(self, records: Any) -> "_InvocationSecrets":
        merged = _InvocationSecrets()
        merged._values.update(self._values)
        incoming = _InvocationSecrets(records)
        merged._values.update(incoming._values)
        return merged

    def get(self, secret_id: str) -> str:
        normalized = str(secret_id or "").strip()
        if normalized in self._values:
            return self._values[normalized]
        raise KeyError(normalized)


class _SecretsProxy:
    def get(self, secret_id: str) -> str:
        resolver = _CURRENT_SECRETS.get()
        if resolver is None:
            resolver = _InvocationSecrets.from_runtime_context()
            _CURRENT_SECRETS.set(resolver)
        return resolver.get(secret_id)


secrets_global = _SecretsProxy()


@lru_cache(maxsize=1)
def _runner_runtime_payload() -> dict[str, Any]:
    try:
        from agentcicd.fixtures.functions.utils.runtime_context import _context_path_from_env, _load_context
    except Exception:
        return {}
    payload = _load_context(_context_path_from_env())
    return dict(payload) if isinstance(payload, dict) else {}


def udf(name: str) -> Callable[..., Any]:
    from agentcicd.fixtures.functions import udf as resolve_udf

    return _wrap_traced_callable(
        resolve_udf(name),
        span_name=f"udf.{name}",
        attributes={"udf_name": name},
    )


AUTO_IMPORTS = {
    "function": function if "function" in globals() else None,
    "environment": environment if "environment" in globals() else None,
    "NamedStruct": NamedStruct,
    "Str": Str,
    "Int": Int,
    "Float": Float,
    "Bool": Bool,
    "Variant": Variant,
    "SecretId": SecretId,
    "Session": Session,
    "EnvSpec": EnvSpec,
    "Environment": Environment,
    "ShellEnv": ShellEnv,
    "ShellCommand": ShellCommand,
    "AgentHarnessEnv": AgentHarnessEnv,
    "McpSpec": McpSpec,
    "BrowserSpec": BrowserSpec,
    "ShellSpec": ShellSpec,
    "AgentHarnessSpec": AgentHarnessSpec,
    "McpHttpSpec": McpHttpSpec,
    "McpStdioSpec": McpStdioSpec,
    "McpPlaywrightSpec": McpPlaywrightSpec,
    "DirectoryEntry": DirectoryEntry,
    "Directory": Directory,
    "Array": Array,
    "Map": Map,
    "Required": Required,
    "Optional": Optional,
    "envs": envs,
    "secrets": secrets_global,
    "objectstore": objectstore,
    "udf": udf,
}


def function(
    target: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    namespace: str | None = None,
    **_ignored: Any,
) -> Callable[..., Any]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.isfunction(func):
            raise TypeError("@function can only decorate Python functions")
        func.__dict__[_FUNCTION_MARKER] = True
        if "." in func.__qualname__:
            return func
        registered_name = _runtime_function_name(func, name=name, namespace=namespace)
        _REGISTRY[registered_name] = _wrap_traced_callable(
            func,
            span_name=f"function.{registered_name}",
            attributes={"function_name": registered_name},
        )
        return func

    if target is None:
        return decorate
    return decorate(target)


def environment(target: type | None = None, **_ignored: Any) -> type:
    def decorate(cls: type) -> type:
        if not inspect.isclass(cls):
            raise TypeError("@environment can only decorate classes")
        _validate_environment_methods(cls)
        return cls

    if target is None:
        return decorate
    return decorate(target)


def _runtime_function_name(func: Callable[..., Any], *, name: str | None, namespace: str | None) -> str:
    if name and "." in name:
        return name
    leaf = name or func.__name__
    return ".".join(part for part in (namespace, leaf) if part)


def _is_function_decorated(value: Any) -> bool:
    try:
        return bool(vars(value).get(_FUNCTION_MARKER, False))
    except TypeError:
        return False


def _validate_environment_methods(cls: type) -> None:
    for method_name, raw_value in cls.__dict__.items():
        if method_name.startswith("_"):
            continue
        value = raw_value
        if isinstance(raw_value, (staticmethod, classmethod)):
            value = raw_value.__func__
        if not callable(value):
            continue
        if not _is_function_decorated(value):
            raise TypeError(f"Environment {cls.__name__}.{method_name} must be decorated with @function")


AUTO_IMPORTS["function"] = function
AUTO_IMPORTS["environment"] = environment


def _install_authoring_module(module_name: str) -> None:
    module = sys.modules.get(module_name)
    if module is None:
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
    for name, value in AUTO_IMPORTS.items():
        module.__dict__[name] = value
    module.__all__ = sorted(AUTO_IMPORTS)


def _install_agentcicd_module() -> None:
    _install_authoring_module("agentcicd")


_install_agentcicd_module()


@contextmanager
def _temporary_authoring_module(module_name: str) -> Iterator[None]:
    module = sys.modules.get(module_name)
    created = module is None
    if module is None:
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
    previous = {name: module.__dict__.get(name, None) for name in AUTO_IMPORTS}
    had_attr = {name: name in module.__dict__ for name in AUTO_IMPORTS}
    previous_all = module.__dict__.get("__all__", None)
    had_all = "__all__" in module.__dict__
    for name, value in AUTO_IMPORTS.items():
        module.__dict__[name] = value
    existing_all = module.__dict__.get("__all__", ())
    module.__all__ = sorted(set(existing_all) | set(AUTO_IMPORTS))
    try:
        yield
    finally:
        for name in AUTO_IMPORTS:
            if had_attr[name]:
                module.__dict__[name] = previous[name]
            else:
                module.__dict__.pop(name, None)
        if had_all:
            module.__dict__["__all__"] = previous_all
        else:
            module.__dict__.pop("__all__", None)
        if created:
            sys.modules.pop(module_name, None)


def load_user_source() -> None:
    source_paths = _function_source_paths()
    if not source_paths:
        return

    for index, source_path in enumerate(source_paths):
        with _temporary_authoring_module("agentcicd_fixtures"):
            with _temporary_authoring_module("agentcicd.fixtures"):
                loaded_globals = runpy.run_path(
                    source_path,
                    init_globals={
                        "udf": udf,
                        **AUTO_IMPORTS,
                    },
                    run_name=f"agentcicd_user_function_{index}",
                )
        _USER_GLOBALS.update(loaded_globals)


def _function_source_paths() -> list[str]:
    raw_paths = os.getenv("AGENTCICD_FUNCTION_SOURCE_PATHS", "").strip()
    if raw_paths:
        try:
            parsed = json.loads(raw_paths)
        except json.JSONDecodeError:
            parsed = raw_paths.split(os.pathsep)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    source_path = os.getenv("AGENTCICD_FUNCTION_SOURCE_PATH", "").strip()
    return [source_path] if source_path else []


def load_builtin_function() -> None:
    builtin_call_name = os.getenv("AGENTCICD_FUNCTION_BUILTIN_CALL_NAME", "").strip()
    if not builtin_call_name:
        return
    entrypoint_name = (
        os.getenv("AGENTCICD_FUNCTION_BUILTIN_ENTRYPOINT", "").strip()
        or os.getenv("AGENTCICD_FUNCTION_RUNTIME_ALIAS", "").strip()
        or builtin_call_name.split(".")[-1]
    )
    if not entrypoint_name:
        raise ValueError("AGENTCICD_FUNCTION_BUILTIN_ENTRYPOINT is required for builtin function runtime")

    builtin = udf(builtin_call_name)

    async def _invoke_builtin(**kwargs: Any) -> Any:
        result = builtin(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    _invoke_builtin.__name__ = entrypoint_name
    _REGISTRY[entrypoint_name] = _wrap_traced_callable(
        _invoke_builtin,
        span_name=f"function.{builtin_call_name}",
        attributes={"function_name": builtin_call_name},
    )


def _contract_from_hint(hint: Any) -> dict[str, Any]:
    if isinstance(hint, _GenericSchemaType) and hint.name == "Required":
        parsed = _contract_from_hint(hint.args[0])
        parsed["nullable"] = False
        return parsed
    if isinstance(hint, _GenericSchemaType) and hint.name == "Optional":
        parsed = _contract_from_hint(hint.args[0])
        parsed["nullable"] = True
        return parsed
    if hint is Str:
        return {"type_sql": "STRING", "nullable": True, "schema": {"kind": "scalar", "name": "Str"}, "json_schema": {"type": "string"}, "manifest_type": {"type": "Str"}}
    if hint is SecretId:
        return {"type_sql": "STRING", "nullable": True, "schema": {"kind": "scalar", "name": "Str"}, "json_schema": {"type": "string"}, "manifest_type": {"type": "Str"}}
    if _is_session_hint(hint):
        return {
            "type_sql": "VARIANT",
            "nullable": True,
            "schema": {"kind": "session"},
            "json_schema": {"type": "object", "additionalProperties": True, "x-agentcicd-type": "session"},
            "manifest_type": {"type": "Session"},
        }
    if hint is Int:
        return {"type_sql": "BIGINT", "nullable": True, "schema": {"kind": "scalar", "name": "Int"}, "json_schema": {"type": "integer", "format": "int64"}, "manifest_type": {"type": "Int"}}
    if hint is Float:
        return {"type_sql": "DOUBLE", "nullable": True, "schema": {"kind": "scalar", "name": "Float"}, "json_schema": {"type": "number"}, "manifest_type": {"type": "Float"}}
    if hint is Bool:
        return {"type_sql": "BOOLEAN", "nullable": True, "schema": {"kind": "scalar", "name": "Bool"}, "json_schema": {"type": "boolean"}, "manifest_type": {"type": "Bool"}}
    if hint is Variant:
        return {"type_sql": "VARIANT", "nullable": True, "schema": {"kind": "scalar", "name": "Variant"}, "json_schema": {"type": "variant"}, "manifest_type": {"type": "Variant"}}
    if isinstance(hint, _GenericSchemaType) and hint.name == "Array":
        element = _contract_from_hint(hint.args[0])
        return {
            "type_sql": f"ARRAY<{element['type_sql']}>",
            "nullable": True,
            "schema": {"kind": "array", "element": element["schema"]},
            "json_schema": {"type": "array", "items": element["json_schema"]},
            "manifest_type": {"type": "Array", "element": element["manifest_type"]},
        }
    if isinstance(hint, _GenericSchemaType) and hint.name == "Map":
        key = _contract_from_hint(hint.args[0])
        if key["type_sql"] != "STRING":
            raise TypeError("Map keys must be Str")
        value = _contract_from_hint(hint.args[1])
        return {
            "type_sql": f"MAP<STRING, {value['type_sql']}>",
            "nullable": True,
            "schema": {"kind": "map", "key": {"kind": "scalar", "name": "Str"}, "value": value["schema"]},
            "json_schema": {"type": "object", "additionalProperties": value["json_schema"]},
            "manifest_type": {"type": "Map", "key": {"type": "Str"}, "value": value["manifest_type"]},
        }
    if isinstance(hint, _GenericSchemaType) and hint.name == "EnvSpec":
        return {
            "type_sql": "VARIANT",
            "nullable": True,
            "schema": {"kind": "env_spec", "spec": str(hint.args[0])},
            "json_schema": {"type": "object", "additionalProperties": True, "x-agentcicd-type": "env_spec"},
            "manifest_type": {"type": "EnvSpec", "spec": str(hint.args[0])},
        }
    if hint is DirectoryEntry:
        return {
            "type_sql": "STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>",
            "nullable": True,
            "schema": {"kind": "directory_entry"},
            "json_schema": {"type": "object", "additionalProperties": True, "x-agentcicd-type": "directory_entry"},
            "manifest_type": {"type": "DirectoryEntry"},
        }
    if hint is Directory:
        entry = _contract_from_hint(DirectoryEntry)
        return {
            "type_sql": f"ARRAY<{entry['type_sql']}>",
            "nullable": True,
            "schema": {"kind": "directory", "element": entry["schema"]},
            "json_schema": {"type": "array", "items": entry["json_schema"], "x-agentcicd-type": "directory"},
            "manifest_type": {"type": "Directory", "element": entry["manifest_type"]},
        }
    if isinstance(hint, type) and issubclass(hint, NamedStruct):
        fields = []
        properties = {}
        required = []
        manifest_fields = []
        module = sys.modules.get(hint.__module__)
        module_globals = vars(module) if module is not None else {}
        for name, field_hint in get_type_hints(hint, globalns={**AUTO_IMPORTS, **module_globals, **_USER_GLOBALS}).items():
            field = _contract_from_hint(field_hint)
            fields.append({"name": name, "type": field["schema"], "nullable": field["nullable"]})
            properties[name] = field["json_schema"]
            field_required = not bool(field["nullable"])
            manifest_fields.append({"name": name, "type": field["manifest_type"], "required": field_required})
            if not field["nullable"]:
                required.append(name)
        json_schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            json_schema["required"] = required
        return {
            "type_sql": "STRUCT<" + ", ".join(f"{field['name']}: {_schema_type_sql(field['type'])}" for field in fields) + ">",
            "nullable": True,
            "schema": {"kind": "struct", "fields": fields},
            "json_schema": json_schema,
            "manifest_type": {"type": "NamedStruct", "name": hint.__name__, "fields": manifest_fields},
        }
    raise TypeError(f"Unsupported fixture annotation: {hint!r}")


def _schema_type_sql(schema: dict[str, Any]) -> str:
    kind = schema.get("kind")
    if kind == "scalar":
        return {"Str": "STRING", "Int": "BIGINT", "Float": "DOUBLE", "Bool": "BOOLEAN", "Variant": "VARIANT"}[str(schema.get("name"))]
    if kind == "array":
        return f"ARRAY<{_schema_type_sql(schema['element'])}>"
    if kind == "map":
        return f"MAP<STRING, {_schema_type_sql(schema['value'])}>"
    if kind == "struct":
        return "STRUCT<" + ", ".join(f"{field['name']}: {_schema_type_sql(field['type'])}" for field in schema.get("fields", [])) + ">"
    raise TypeError("Unsupported schema")


def build_manifest() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for name, func in _REGISTRY.items():
        target = _unwrap_runtime_callable(func)
        signature = inspect.signature(target)
        type_hints = get_type_hints(target, globalns={**AUTO_IMPORTS, **target.__globals__})
        parameters = []
        manifest_parameters = []
        injected_parameters = []
        inputs = {}
        for parameter_name, parameter in signature.parameters.items():
            if _is_session_hint(type_hints.get(parameter_name)):
                injected_parameters.append({"name": parameter_name, "kind": "session"})
                continue
            parsed = _contract_from_hint(type_hints.get(parameter_name))
            inputs[parameter_name] = parsed["json_schema"]
            has_default = parameter.default is not inspect.Parameter.empty
            parameters.append(
                {
                    "name": parameter_name,
                    "type_sql": parsed["type_sql"],
                    "nullable": parsed["nullable"],
                    "has_default": has_default,
                }
            )
            manifest_parameters.append(
                {
                    "name": parameter_name,
                    "type": parsed["manifest_type"],
                    "required": not has_default,
                    "nullable": parsed["nullable"],
                    "has_default": has_default,
                    "type_sql": parsed["type_sql"],
                }
            )
        returned = _contract_from_hint(type_hints.get("return"))
        runtime_alias = name.replace(".", "_")
        runtime_signature = {
            "parameters": parameters,
            "return": {
                "type_sql": returned["type_sql"],
                "nullable": returned["nullable"],
                "schema": returned["schema"],
            },
        }
        metadata = {
            "execution_runtime": "function_runner",
            "entrypoint_name": str(target.__name__),
            "module": str(target.__module__),
            "object": str(target.__name__),
            "shape": "1:1",
            "return_type_sql": returned["type_sql"],
            "output_schema": returned["json_schema"],
            "signature": runtime_signature,
        }
        if injected_parameters:
            metadata["injected_parameters"] = injected_parameters
        items.append(
            {
                "name": name,
                "module": str(target.__module__),
                "object": str(target.__name__),
                "shape": "1:1",
                "async": inspect.iscoroutinefunction(target),
                "parameters": manifest_parameters,
                "returns": returned["manifest_type"],
                "runtime": {
                    "kind": "python",
                    "runtime_alias": runtime_alias,
                    "entrypoint": f"{func.__module__}:{func.__name__}",
                },
                "inputs": inputs,
                "output": returned["json_schema"],
                "signature": runtime_signature,
                "metadata": metadata,
            }
        )
    return {
        "schema_version": "agentcicd.fixtures.manifest.v1",
        "package": {
            "name": os.getenv("AGENTCICD_FIXTURE_PACKAGE_NAME", "runtime-fixture"),
            "version": os.getenv("AGENTCICD_FIXTURE_PACKAGE_VERSION", "0.0.0"),
            "namespace": os.getenv("AGENTCICD_FIXTURE_PACKAGE_NAMESPACE", "runtime"),
        },
        "requires": {"agentcicd_fixtures": ">=0.1.0"},
        "functions": items,
        "environments": [],
        "product_types": [],
    }


async def invoke_function(name: str, arguments: dict[str, Any], secret_records: Any = None) -> Any:
    func = _REGISTRY.get(name)
    if func is None:
        raise KeyError(name)
    call_arguments = _filter_runtime_control_arguments(func, arguments)
    call_arguments = _coerce_runtime_call_arguments(func, call_arguments)
    call_arguments = _inject_runtime_session_arguments(func, call_arguments)
    resolver = _InvocationEnvs()
    secret_resolver = _InvocationSecrets.from_runtime_context().merged(secret_records)
    token = _CURRENT_ENVS.set(resolver)
    secret_token = _CURRENT_SECRETS.set(secret_resolver)
    try:
        if inspect.iscoroutinefunction(func):
            return _to_jsonable(await func(**call_arguments))
        return _to_jsonable(func(**call_arguments))
    finally:
        try:
            await resolver.teardown_all()
        finally:
            _CURRENT_ENVS.reset(token)
            _CURRENT_SECRETS.reset(secret_token)


def _filter_runtime_control_arguments(func: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(_unwrap_runtime_callable(func))
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return arguments
    return {
        name: value
        for name, value in arguments.items()
        if name in signature.parameters
    }


def _coerce_runtime_call_arguments(func: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    target = _unwrap_runtime_callable(func)
    type_hints = get_type_hints(target, globalns={**AUTO_IMPORTS, **target.__globals__})
    coerced = dict(arguments)
    for name, value in list(coerced.items()):
        if _is_environment_spec_hint(type_hints.get(name)):
            coerced[name] = _coerce_runtime_environment_spec(value)
    return coerced


def _inject_runtime_session_arguments(func: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    target = _unwrap_runtime_callable(func)
    type_hints = get_type_hints(target, globalns={**AUTO_IMPORTS, **target.__globals__})
    session_parameters = [
        name
        for name, parameter in inspect.signature(func).parameters.items()
        if _is_session_hint(type_hints.get(name)) and name not in arguments and parameter.default is inspect.Parameter.empty
    ]
    if not session_parameters:
        return arguments
    session = _runtime_session(arguments)
    injected = dict(arguments)
    for name in session_parameters:
        injected[name] = session
    return injected


def _unwrap_runtime_callable(func: Callable[..., Any]) -> Callable[..., Any]:
    try:
        return inspect.unwrap(func)
    except Exception:
        return func


def _is_session_hint(hint: Any) -> bool:
    if hint is Session:
        return True
    try:
        hint_name = hint.__name__
        hint_module = hint.__module__
    except AttributeError:
        return False
    return hint_name == "Session" and str(hint_module).startswith(("agentcicd.fixtures", "agentcicd_fixtures"))


def _runtime_session(arguments: dict[str, Any]) -> Session:
    context = _runner_runtime_payload()
    pool_kind = _runtime_pool_kind(context)
    if pool_kind != "session":
        label = pool_kind or "unknown"
        raise RuntimeError(f"Session injection requires a session pool; current pool kind is '{label}'")
    workspace_dir = _runtime_workspace_dir(context, arguments)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return Session(workspace_dir)


def _runtime_pool_kind(context: dict[str, Any]) -> str:
    for key in ("AGENTCICD_FUNCTION_POOL_KIND", "AGENTCICD_POOL_KIND", "AGENTCICD_RUNTIME_POOL_KIND"):
        value = os.getenv(key)
        if value:
            return str(value).strip().lower()
    for value in (
        context.get("pool_kind"),
        context.get("pool", {}).get("kind") if isinstance(context.get("pool"), dict) else None,
        context.get("runtime_pool", {}).get("kind") if isinstance(context.get("runtime_pool"), dict) else None,
    ):
        if value:
            return str(value).strip().lower()
    return ""


def _runtime_workspace_dir(context: dict[str, Any], arguments: dict[str, Any]) -> Path:
    for key in ("AGENTCICD_SESSION_WORKSPACE_DIR", "AGENTCICD_WORKSPACE_DIR", "AGENTCICD_FUNCTION_WORKSPACE_DIR"):
        value = os.getenv(key)
        if value:
            return Path(value).expanduser()
    for value in (
        context.get("workspace_dir"),
        context.get("workspace", {}).get("dir") if isinstance(context.get("workspace"), dict) else None,
        context.get("pool", {}).get("workspace_dir") if isinstance(context.get("pool"), dict) else None,
    ):
        if value:
            return Path(str(value)).expanduser()
    from_arguments = _workspace_dir_from_arguments(arguments)
    if from_arguments is not None:
        return from_arguments
    return Path(tempfile.mkdtemp(prefix="agentcicd-session-workspace-"))


def _workspace_dir_from_arguments(arguments: dict[str, Any]) -> Path | None:
    candidates: list[str] = []
    for value in arguments.values():
        payload = _coerce_runtime_environment_spec(value)
        if not isinstance(payload, dict) or payload.get("spec_type") != "environment":
            continue
        config = payload.get("config")
        if not isinstance(config, dict):
            continue
        for key in ("workdir", "cwd", "root"):
            candidate = str(config.get(key) or "").strip()
            if candidate:
                candidates.append(candidate)
                break
    normalized = {str(Path(candidate).expanduser()) for candidate in candidates}
    if not normalized:
        return None
    if len(normalized) > 1:
        raise RuntimeError("Session workspace_dir is ambiguous because environment specs use different workdirs")
    return Path(next(iter(normalized))).expanduser()


def _is_environment_spec_hint(hint: Any) -> bool:
    if hint in {ShellEnv, AgentHarnessEnv}:
        return True
    return isinstance(hint, _GenericSchemaType) and hint.name == "EnvSpec"


def _coerce_runtime_environment_spec(value: Any) -> Any:
    if isinstance(value, _RuntimeEnvironmentSpecDict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, dict) and value.get("spec_type") == "environment":
        return _RuntimeEnvironmentSpecDict({str(key): _to_jsonable(item) for key, item in value.items()})
    return value


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, NamedStruct):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


class FunctionRequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentCICDFunctionRuntime/0.1"

    def _write_json(self, payload: dict[str, Any], status_code: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/manifest":
            self._write_json(build_manifest())
            return
        self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/invoke/"):
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        function_name = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            arguments = payload.get("args", {})
            if not isinstance(arguments, dict):
                raise ValueError("args must be an object")
            with _remote_runtime_trace_context(payload.get("trace")) as trace:
                result = asyncio.run(invoke_function(function_name, arguments, payload.get("secrets")))
                trace_records = trace.records() if trace else None
        except KeyError:
            self._write_json({"error": "unknown_function", "name": function_name}, HTTPStatus.NOT_FOUND)
            return
        except Exception as exc:  # pragma: no cover
            self._write_json({"error": "invoke_failed", "detail": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        response_payload = {"result": result}
        if trace_records:
            response_payload["trace_records"] = trace_records
        self._write_json(response_payload)


def serve(port: int = 8080) -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", port), FunctionRequestHandler)
    httpd.serve_forever()


def invoke_jsonl() -> int:
    raw_body = sys.stdin.read()
    try:
        payload = json.loads(raw_body or "{}")
        function_name = str(payload.get("function_name") or "").strip()
        if not function_name:
            raise ValueError("function_name is required")
        arguments = payload.get("args", {})
        if not isinstance(arguments, dict):
            raise ValueError("args must be an object")
        load_user_source()
        load_builtin_function()
        with _remote_runtime_trace_jsonl_context(payload.get("trace")) as trace:
            result = asyncio.run(invoke_function(function_name, arguments, payload.get("secrets")))
            trace_records = trace.records() if trace else None
        frame: dict[str, Any] = {"type": "result", "result": result}
        if trace_records:
            frame["trace_records"] = trace_records
        _write_jsonl_frame(frame)
        return 0
    except KeyError as exc:
        _write_jsonl_frame({"type": "error", "error": "unknown_function", "detail": str(exc)})
        return 1
    except Exception as exc:
        _write_jsonl_frame({"type": "error", "error": "invoke_failed", "detail": str(exc)})
        return 1


def main() -> int:
    if "--invoke-jsonl" in sys.argv:
        return invoke_jsonl()
    load_user_source()
    load_builtin_function()
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
