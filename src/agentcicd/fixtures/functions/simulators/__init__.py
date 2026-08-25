from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import inspect
import os
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple, get_args, get_origin

from agentcicd.fixtures._attrs import callable_attr, read_attr, type_display_name
from agentcicd.fixtures.core.function import AsyncRowFunction, Function, RowFunction
from agentcicd.fixtures.core.tracing import runtime_trace_span, wrap_runtime_traced_callable
from agentcicd.fixtures.core.types import ArrayType, BooleanType, DType, FType, FloatType, FunctionType, IntType, JsonType, StringType
from agentcicd.fixtures.core.udf import Param, Udf
from agentcicd.fixtures.functions.agent_harness_environment import AgentHarnessEnvironmentHandle


USER_FUNCTION_TYPE_SQL = (
    "FUNCTION<(agent_response VARIANT, state VARIANT, environments ANY, turn INTEGER) "
    "RETURNS STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>>"
)
AGENT_FUNCTION_TYPE_SQL = (
    "FUNCTION<(request VARIANT, state VARIANT, environments ANY, turn INTEGER) "
    "RETURNS STRUCT<response: VARIANT, state: VARIANT>>"
)
OBSERVER_FUNCTION_TYPE_SQL = (
    "FUNCTION<(event VARIANT, state VARIANT, environments ANY) "
    "RETURNS STRUCT<observation: VARIANT, artifacts: ARRAY<VARIANT>, state: VARIANT>>"
)
_RESOLVED_ENVIRONMENT_METHODS = frozenset(
    {
        "root",
        "path",
        "mkdir",
        "exists",
        "glob",
        "read_file",
        "write_file",
        "list_dir",
        "stat",
        "rm",
        "require_file",
        "require_glob",
        "start",
        "terminate",
        "signal",
        "kill",
        "running_processes",
        "observe",
        "click",
        "type_text",
        "run_task",
    }
)
SIMULATOR_RESULT_TYPE_SQL = (
    "STRUCT<"
    "ok: BOOLEAN, "
    "status: STRING, "
    "final_output: VARIANT, "
    "turns: ARRAY<STRUCT<"
    "turn: INTEGER, "
    "request: VARIANT, "
    "response: VARIANT, "
    "user_request: VARIANT, "
    "terminate: BOOLEAN, "
    "error: STRUCT<code: STRING, message: STRING, retryable: BOOLEAN>"
    ">>, "
    "observations: ARRAY<STRUCT<"
    "callback: STRING, "
    "schedule: STRING, "
    "turn: INTEGER, "
    "observation: VARIANT, "
    "artifacts: ARRAY<VARIANT>, "
    "error: STRUCT<code: STRING, message: STRING, retryable: BOOLEAN>"
    ">>, "
    "artifacts: ARRAY<VARIANT>, "
    "error: STRUCT<code: STRING, message: STRING, retryable: BOOLEAN>, "
    "duration_ms: BIGINT"
    ">"
)

SUPPORTED_OBSERVER_SCHEDULES = frozenset({"after_turn", "final"})
SUPPORTED_REUSE_MODES = frozenset({"none", "per_invocation"})
DEFAULT_LIMITS = {"max_turns": 32, "timeout_seconds": 300.0}


@dataclass(frozen=True)
class SimulatorError:
    code: str
    message: str
    retryable: bool = False

    @classmethod
    def from_exception(cls, exc: BaseException, *, code: str = "failed", retryable: bool = False) -> "SimulatorError":
        return cls(code=code, message=str(exc), retryable=retryable)


@dataclass(frozen=True)
class SimulatorLimits:
    max_turns: int = DEFAULT_LIMITS["max_turns"]
    timeout_seconds: float = DEFAULT_LIMITS["timeout_seconds"]


@dataclass(frozen=True)
class ObserverSpec:
    callback: Any
    schedule: tuple[str, ...] = ("after_turn",)
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnvironmentSpec:
    kind: str
    env_id: str
    config: Mapping[str, Any] = field(default_factory=dict)


class McpSpecDict(dict[str, Any]):
    """Dictionary-backed MCP spec with an explicit fixture-facing type."""


class MaterializedMcpHandle:
    """Fixture-facing handle for an MCP server attached to an agent harness."""

    def __init__(self, spec: Mapping[str, Any]) -> None:
        self._spec: McpSpecDict = McpSpecDict(_json_safe(dict(spec)))
        self._exit_stack: AsyncExitStack | None = None
        self._session: Any = None
        self._teardown_started = False

    def to_mcp_spec(self) -> McpSpecDict:
        return self._spec

    def to_agent_mcp_spec(self) -> McpSpecDict:
        return self.to_mcp_spec()

    @property
    def requires_setup_for_agent(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return str(self._spec.get("name") or "").strip()

    @property
    def start_mode(self) -> str:
        mode = str(self._spec.get("start_mode") or "lazy").strip().lower()
        return mode if mode in {"lazy", "early"} else "lazy"

    async def setup(self) -> Any:
        if self._session is not None:
            return self._session
        transport = str(self._spec.get("transport") or "").strip().lower()
        if transport == "stdio":
            self._session = await self._setup_stdio_session()
        elif transport == "http":
            self._session = await self._setup_http_session()
        else:
            raise ValueError(f"MCP server '{self.name}' has unsupported transport '{transport}'")
        return self._session

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        tool_name = str(name or "").strip()
        if not tool_name:
            raise ValueError("MCP tool name is required")
        session = await self.setup()
        result = await session.call_tool(tool_name, dict(arguments or {}))
        return _json_safe(_model_to_data(result))

    async def list_tools(self) -> list[dict[str, Any]]:
        session = await self.setup()
        result = await session.list_tools()
        data = _json_safe(_model_to_data(result))
        tools = data.get("tools") if isinstance(data, Mapping) else data
        if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
            return []
        return [dict(tool) for tool in tools if isinstance(tool, Mapping)]

    async def assert_tool_available(self, name: str) -> None:
        tool_name = str(name or "").strip()
        if not tool_name:
            raise ValueError("MCP tool name is required")
        tools = await self.list_tools()
        available = {str(tool.get("name") or "").strip() for tool in tools}
        if tool_name not in available:
            listed = ", ".join(sorted(item for item in available if item)) or "none"
            raise ValueError(f"MCP server '{self.name}' does not expose tool '{tool_name}'. Available tools: {listed}")

    async def teardown(self, reason: Any = None) -> None:
        if self._teardown_started:
            return
        self._teardown_started = True
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            await stack.aclose()

    async def _setup_stdio_session(self) -> Any:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:  # pragma: no cover - depends on optional runtime package
            raise RuntimeError("MCP stdio client support requires the 'mcp' package") from exc

        command = str(self._spec.get("command") or self._spec.get("program") or "").strip()
        if not command:
            raise ValueError(f"MCP server '{self.name}' requires command")
        stack = AsyncExitStack()
        self._exit_stack = stack
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(
                StdioServerParameters(
                    command=command,
                    args=_coerce_string_list(self._spec.get("args"), field_name="args"),
                    env=_coerce_optional_mapping(self._spec.get("env"), field_name="env") or None,
                )
            )
        )
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def _setup_http_session(self) -> Any:
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.streamable_http import streamablehttp_client
        except Exception as exc:  # pragma: no cover - depends on optional runtime package
            raise RuntimeError("MCP HTTP client support requires the 'mcp' package") from exc

        endpoint = str(self._spec.get("endpoint") or self._spec.get("url") or "").strip()
        if not endpoint:
            raise ValueError(f"MCP server '{self.name}' requires endpoint")
        headers = _coerce_optional_mapping(self._spec.get("headers"), field_name="headers") or None
        stack = AsyncExitStack()
        self._exit_stack = stack
        if endpoint.rstrip("/").endswith("/sse"):
            read_stream, write_stream = await stack.enter_async_context(sse_client(endpoint, headers=headers))
        else:
            read_stream, write_stream, _session_id = await stack.enter_async_context(
                streamablehttp_client(endpoint, headers=headers)
            )
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session


class MaterializedPlaywrightMcpHandle(MaterializedMcpHandle):
    def __init__(self, spec: Mapping[str, Any]) -> None:
        super().__init__(spec)
        self._server_process: asyncio.subprocess.Process | None = None
        self._http_endpoint: str | None = None
        self._http_spec: McpSpecDict | None = None
        if str(self._spec.get("transport") or "").strip().lower() == "stdio":
            port = int(self._spec.get("port") or _allocate_local_port())
            self._http_endpoint = f"http://localhost:{port}/mcp"
            self._http_spec = McpSpecDict({
                **{key: value for key, value in self._spec.items() if key not in {"command", "args", "env", "transport"}},
                "transport": "http",
                "endpoint": self._http_endpoint,
            })
            self._spec["port"] = port

    @property
    def requires_setup_for_agent(self) -> bool:
        return self._http_spec is not None

    def to_agent_mcp_spec(self) -> McpSpecDict:
        return self._http_spec or self.to_mcp_spec()

    async def setup(self) -> Any:
        if self._http_spec is not None:
            await self._ensure_http_server()
            original = self._spec
            try:
                self._spec = self._http_spec
                return await super().setup()
            finally:
                self._spec = original
        return await super().setup()

    async def browser_take_screenshot(
        self,
        *,
        filename: str | None = None,
        path: str | None = None,
        fullPage: bool | None = None,
        full_page: bool | None = None,
        **arguments: Any,
    ) -> Any:
        normalized_path = str(filename or path or "").strip()
        if not normalized_path:
            raise ValueError("browser_take_screenshot requires filename")
        payload = dict(arguments)
        payload["filename"] = normalized_path
        payload["fullPage"] = bool(full_page if full_page is not None else (fullPage if fullPage is not None else True))
        result = await self.call_tool("browser_take_screenshot", payload)
        self._raise_on_tool_error(result, f"Playwright screenshot failed for {normalized_path}")
        return result

    async def browser_navigate(self, *, url: str, **arguments: Any) -> Any:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            raise ValueError("browser_navigate requires url")
        result = await self.call_tool("browser_navigate", {"url": normalized_url, **dict(arguments)})
        self._raise_on_tool_error(result, f"Playwright navigation failed for {normalized_url}")
        return result

    async def browser_wait_for(
        self,
        *,
        text: str | None = None,
        time: float | None = None,
        **arguments: Any,
    ) -> Any:
        payload = dict(arguments)
        if text is not None and str(text).strip():
            payload["text"] = str(text).strip()
        if time is not None:
            payload["time"] = float(time)
        if not payload:
            raise ValueError("browser_wait_for requires at least one argument")
        result = await self.call_tool("browser_wait_for", payload)
        target = payload.get("text") or payload.get("time") or "condition"
        self._raise_on_tool_error(result, f"Playwright wait failed for {target}")
        return result

    async def browser_tabs(self, *, action: str = "list", index: int | None = None, url: str | None = None, **arguments: Any) -> Any:
        normalized_action = str(action or "").strip()
        if normalized_action not in {"list", "new", "close", "select"}:
            raise ValueError("browser_tabs action must be one of: list, new, close, select")
        payload = {"action": normalized_action, **dict(arguments)}
        if index is not None:
            payload["index"] = int(index)
        if url is not None and str(url).strip():
            payload["url"] = str(url).strip()
        result = await self.call_tool("browser_tabs", payload)
        self._raise_on_tool_error(result, f"Playwright tabs action failed: {normalized_action}")
        return result

    async def _call_browser_tool(self, tool_name: str, **arguments: Any) -> Any:
        result = await self.call_tool(tool_name, dict(arguments))
        self._raise_on_tool_error(result, f"Playwright tool failed: {tool_name}")
        return result

    def _raise_on_tool_error(self, result: Any, fallback: str) -> None:
        if isinstance(result, Mapping) and result.get("isError"):
            raise RuntimeError(_mcp_result_text(result) or fallback)

    async def browser_navigate_back(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_navigate_back", **arguments)

    async def browser_navigate_forward(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_navigate_forward", **arguments)

    async def browser_reload(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_reload", **arguments)

    async def browser_snapshot(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_snapshot", **arguments)

    async def browser_click(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_click", **arguments)

    async def browser_hover(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_hover", **arguments)

    async def browser_drag(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_drag", **arguments)

    async def browser_type(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_type", **arguments)

    async def browser_fill_form(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_fill_form", **arguments)

    async def browser_select_option(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_select_option", **arguments)

    async def browser_check(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_check", **arguments)

    async def browser_uncheck(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_uncheck", **arguments)

    async def browser_press_key(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_press_key", **arguments)

    async def browser_handle_dialog(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_handle_dialog", **arguments)

    async def browser_file_upload(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_file_upload", **arguments)

    async def browser_close(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_close", **arguments)

    async def browser_resize(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_resize", **arguments)

    async def browser_network_requests(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_network_requests", **arguments)

    async def browser_route(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_route", **arguments)

    async def browser_route_list(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_route_list", **arguments)

    async def browser_unroute(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_unroute", **arguments)

    async def browser_storage_state(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_storage_state", **arguments)

    async def browser_set_storage_state(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_set_storage_state", **arguments)

    async def browser_run_code(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_run_code", **arguments)

    async def browser_evaluate(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_evaluate", **arguments)

    async def browser_console_messages(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_console_messages", **arguments)

    async def browser_generate_locator(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_generate_locator", **arguments)

    async def browser_verify_element_visible(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_verify_element_visible", **arguments)

    async def browser_verify_text_visible(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_verify_text_visible", **arguments)

    async def browser_start_tracing(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_start_tracing", **arguments)

    async def browser_stop_tracing(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_stop_tracing", **arguments)

    async def browser_start_video(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_start_video", **arguments)

    async def browser_stop_video(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_stop_video", **arguments)

    async def browser_pdf_save(self, **arguments: Any) -> Any:
        return await self._call_browser_tool("browser_pdf_save", **arguments)

    async def screenshot(self, path: str, *, full_page: bool = True) -> Any:
        return await self.browser_take_screenshot(filename=path, full_page=full_page)

    async def navigate(self, url: str) -> Any:
        return await self.browser_navigate(url=url)

    async def wait_for(self, *, text: str | None = None, time: float | None = None) -> Any:
        return await self.browser_wait_for(text=text, time=time)

    async def tabs(self, action: str = "list", *, index: int | None = None, url: str | None = None) -> Any:
        return await self.browser_tabs(action=action, index=index, url=url)

    async def teardown(self, reason: Any = None) -> None:
        await super().teardown(reason)
        process = self._server_process
        self._server_process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _ensure_http_server(self) -> None:
        if self._server_process is not None and self._server_process.returncode is None:
            return
        command = str(self._spec.get("command") or self._spec.get("program") or "").strip()
        if not command:
            raise ValueError(f"MCP server '{self.name}' requires command")
        port = int(self._spec["port"])
        args = _coerce_string_list(self._spec.get("args"), field_name="args")
        args = _replace_cli_option(args, "--port", str(port))
        env = None
        raw_env = _coerce_optional_mapping(self._spec.get("env"), field_name="env")
        if raw_env:
            env = {**os.environ, **{str(key): str(value) for key, value in raw_env.items()}}
        self._server_process = await asyncio.create_subprocess_exec(
            command,
            *args,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await _wait_for_tcp_port("localhost", port, process=self._server_process)


class EnvironmentSpecConfig:
    def __init__(self, payload: "EnvironmentSpecDict") -> None:
        self._payload = payload

    def add_mcp(self, key: str, spec: Mapping[str, Any] | MaterializedMcpHandle) -> "EnvironmentSpecDict":
        config = self._config()
        mcps = config.setdefault("mcps", {})
        if not isinstance(mcps, dict):
            raise ValueError("environment config field 'mcps' must be a map")
        name = str(key or "").strip()
        if not name:
            raise ValueError("MCP server key is required")
        payload = _json_safe(dict(_mcp_spec_payload(spec)))
        payload["name"] = name
        mcps[name] = payload
        return self._payload

    def _config(self) -> dict[str, Any]:
        config = self._payload["config"]
        if not isinstance(config, dict):
            raise ValueError("environment spec config must be an object")
        return config


class EnvironmentSpecDict(dict[str, Any]):
    """Dictionary-backed environment spec with typed convenience methods."""

    @property
    def config(self) -> EnvironmentSpecConfig:
        return EnvironmentSpecConfig(self)


@dataclass(frozen=True)
class TurnRecord:
    turn: int
    request: Any
    response: Any = None
    user_request: Any = None
    terminate: bool = False
    error: SimulatorError | None = None


@dataclass(frozen=True)
class ObservationRecord:
    callback: str
    schedule: str
    turn: int
    observation: Any = None
    artifacts: tuple[Any, ...] = ()
    error: SimulatorError | None = None


@dataclass(frozen=True)
class SimulatorResult:
    ok: bool
    status: str
    final_output: Any
    turns: tuple[TurnRecord, ...]
    observations: tuple[ObservationRecord, ...]
    artifacts: tuple[Any, ...]
    error: SimulatorError | None
    duration_ms: int


@dataclass(frozen=True)
class RuntimeEnvironmentEntry:
    kind: str
    env_id: str
    spec: Any
    session: Any


@dataclass(frozen=True)
class EnvFamilyPolicy:
    allow_multiple_sessions: bool
    identity_fields: tuple[str, ...] = ("session_id",)


ENV_FAMILY_POLICIES: dict[str, EnvFamilyPolicy] = {
    "agent_harness": EnvFamilyPolicy(allow_multiple_sessions=True, identity_fields=("session_id",)),
    "browser": EnvFamilyPolicy(allow_multiple_sessions=True, identity_fields=("session_id",)),
    "shell": EnvFamilyPolicy(allow_multiple_sessions=True, identity_fields=("session_id",)),
}


@dataclass
class RuntimeEnvironments:
    entries: dict[str, RuntimeEnvironmentEntry] = field(default_factory=dict)

    def get(self, env_id: str, default: Any = None) -> Any:
        entry = self.entries.get(env_id)
        return default if entry is None else entry.session

    def require(self, env_id: str) -> Any:
        entry = self.entries.get(env_id)
        if entry is None:
            raise KeyError(f"Unknown runtime environment '{env_id}'")
        return entry.session

    def by_kind(self, kind: str) -> tuple[Any, ...]:
        normalized = kind.strip().lower()
        return tuple(entry.session for entry in self.entries.values() if entry.kind == normalized)

    def __getitem__(self, env_id: str) -> Any:
        return self.require(env_id)

    def __contains__(self, env_id: object) -> bool:
        return isinstance(env_id, str) and env_id in self.entries

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.require(name)
        except KeyError as exc:
            raise AttributeError(name) from exc


class LazyEnvironmentHandle:
    __agentcicd_lazy_environment__ = True

    def __init__(self, *, kind: str, env_id: str, setup_spec: Any, environment: Any) -> None:
        self.kind = kind
        self.env_id = env_id
        self.setup_spec = setup_spec
        self._environment = environment
        self._session: Any = None
        self.initialized = False

    @property
    def session(self) -> Any | None:
        return self._session

    async def setup(self) -> Any:
        if not self.initialized:
            with runtime_trace_span(f"{self.kind}.setup", {"environment_kind": self.kind}):
                self._session = await self._environment.setup(self.setup_spec)
            self.initialized = True
        return self._session

    async def teardown(self, reason: Any = None) -> None:
        if not self.initialized:
            return
        session = self._session
        self._session = None
        self.initialized = False
        teardown = callable_attr(session, "teardown")
        if teardown is not None:
            result = teardown(reason or type("Reason", (), {"code": "teardown", "message": None})())
            if inspect.isawaitable(result):
                await result

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in _RESOLVED_ENVIRONMENT_METHODS:
            raise AttributeError(name)

        async def _method(*args: Any, **kwargs: Any) -> Any:
            session = await self.setup()
            method = callable_attr(session, name)
            if method is None:
                raise AttributeError(name)
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        return wrap_runtime_traced_callable(
            _method,
            span_name=f"{self.kind}.{name}",
            attributes={"environment_kind": self.kind, "method": name},
        )


class EnvironmentHandleRegistry:
    def __init__(self) -> None:
        self._handles: dict[tuple[Any, ...], tuple[dict[str, Any], Any]] = {}

    def get_or_create(self, spec: EnvironmentSpec) -> Any:
        key = _environment_registry_key(spec)
        fingerprint = _environment_spec_fingerprint(spec)
        existing = self._handles.get(key)
        if existing is not None:
            existing_fingerprint, handle = existing
            if existing_fingerprint != fingerprint:
                policy = ENV_FAMILY_POLICIES[spec.kind]
                scope = f"session_id '{spec.env_id}'" if policy.allow_multiple_sessions else "singleton environment"
                raise ValueError(f"Conflicting {spec.kind} spec for {scope}")
            return handle
        handle = _lazy_environment_from_spec(spec)
        self._handles[key] = (fingerprint, handle)
        return handle


class EnvironmentProvider(Protocol):
    async def setup(self, specs: Sequence[EnvironmentSpec]) -> RuntimeEnvironments: ...
    async def teardown(self, environments: RuntimeEnvironments, reason_code: str, message: str | None = None) -> None: ...


class DefaultEnvironmentProvider:
    async def setup(self, specs: Sequence[EnvironmentSpec]) -> RuntimeEnvironments:
        runtime = RuntimeEnvironments()
        registry = EnvironmentHandleRegistry()
        for spec in specs:
            session = registry.get_or_create(spec)
            runtime.entries[spec.env_id] = RuntimeEnvironmentEntry(
                kind=spec.kind,
                env_id=spec.env_id,
                spec=read_attr(session, "setup_spec", spec),
                session=session,
            )
        return runtime

    async def teardown(self, environments: RuntimeEnvironments, reason_code: str, message: str | None = None) -> None:
        if not environments.entries:
            return
        from agentcicd.fixtures.environments.core.lifecycle import TeardownReason

        reason = TeardownReason(code=reason_code, message=message)
        for entry in reversed(tuple(environments.entries.values())):
            teardown = callable_attr(entry.session, "teardown")
            if teardown is not None:
                result = teardown(reason)
                if inspect.isawaitable(result):
                    await result


class SimulatorObserverSpecRowFunction(RowFunction):
    def transform(self, callback: Any, schedule: Any = None, config: Any = None) -> dict[str, Any]:
        schedules = _coerce_schedule(schedule)
        return {
            "spec_type": "simulator_observer",
            "callback": callback,
            "schedule": list(schedules),
            "config": _coerce_optional_mapping(config, field_name="config"),
        }


class SimulatorLimitsRowFunction(RowFunction):
    def transform(self, max_turns: Optional[int] = None, timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        limits = _coerce_limits({"max_turns": max_turns, "timeout_seconds": timeout_seconds})
        return {
            "spec_type": "simulator_limits",
            "max_turns": limits.max_turns,
            "timeout_seconds": limits.timeout_seconds,
        }


class BrowserEnvironmentSpecRowFunction(RowFunction):
    def transform(
        self,
        env_id: str,
        start_url: Optional[str] = None,
        policy: Any = None,
        viewport: Any = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        storage_state_path: Optional[str] = None,
    ) -> dict[str, Any]:
        config = _drop_none(
            {
                "start_url": start_url,
                "policy": policy,
                "viewport": viewport,
                "locale": locale,
                "timezone_id": timezone_id,
                "storage_state_path": storage_state_path,
            }
        )
        return _environment_spec_payload("browser", env_id, config)


class ShellEnvironmentSpecRowFunction(RowFunction):
    def transform(self, session_id: str, cwd: Optional[str] = None, policy: Any = None, env: Any = None) -> dict[str, Any]:
        return _environment_spec_payload("shell", session_id, _drop_none({"cwd": cwd, "policy": policy, "env": env}))


class AgentHarnessEnvironmentSpecRowFunction(RowFunction):
    def transform(
        self,
        session_id: str,
        aisystem: str,
        workdir: str,
        secret_id: Optional[str] = None,
        mcps: Any = None,
    ) -> dict[str, Any]:
        return _environment_spec_payload(
            "agent_harness",
            session_id,
            _drop_none({
                "session_id": str(session_id or "").strip(),
                "aisystem": str(aisystem or "").strip(),
                "workdir": str(workdir or "."),
                "secret_id": str(secret_id).strip() if secret_id is not None and str(secret_id).strip() else None,
                "mcps": _coerce_mcp_spec_map(mcps) if mcps is not None else None,
            }),
        )


class McpHttpSpecRowFunction(RowFunction):
    def transform(
        self,
        name: str,
        endpoint: str,
        required: Optional[bool] = None,
        secret_id: Optional[str] = None,
        allow_tools: Any = None,
        deny_tools: Any = None,
        headers: Any = None,
        start_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("envs.mcp.http.spec requires name")
        normalized_endpoint = str(endpoint or "").strip()
        if not normalized_endpoint:
            raise ValueError("envs.mcp.http.spec requires endpoint")
        return McpSpecDict({
            "spec_type": "mcp",
            "transport": "http",
            "name": normalized_name,
            "endpoint": normalized_endpoint,
            "required": bool(required) if required is not None else False,
            "secret_id": str(secret_id).strip() if secret_id is not None and str(secret_id).strip() else None,
            "allow_tools": _coerce_string_list(allow_tools, field_name="allow_tools"),
            "deny_tools": _coerce_string_list(deny_tools, field_name="deny_tools"),
            "headers": _coerce_optional_mapping(headers, field_name="headers"),
            "start_mode": _coerce_mcp_start_mode(start_mode),
        })


class McpStdioSpecRowFunction(RowFunction):
    def transform(
        self,
        name: str,
        command: str,
        args: Any = None,
        required: Optional[bool] = None,
        allow_tools: Any = None,
        deny_tools: Any = None,
        env: Any = None,
        default_tools_approval_mode: Optional[str] = None,
        start_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("envs.mcp.stdio.spec requires name")
        normalized_command = str(command or "").strip()
        if not normalized_command:
            raise ValueError("envs.mcp.stdio.spec requires command")
        return McpSpecDict({
            "spec_type": "mcp",
            "transport": "stdio",
            "name": normalized_name,
            "command": normalized_command,
            "args": _coerce_string_list(args, field_name="args"),
            "required": bool(required) if required is not None else False,
            "allow_tools": _coerce_string_list(allow_tools, field_name="allow_tools"),
            "deny_tools": _coerce_string_list(deny_tools, field_name="deny_tools"),
            "env": _coerce_optional_mapping(env, field_name="env"),
            "default_tools_approval_mode": (
                str(default_tools_approval_mode).strip()
                if default_tools_approval_mode is not None and str(default_tools_approval_mode).strip()
                else None
            ),
            "start_mode": _coerce_mcp_start_mode(start_mode),
        })


class McpPlaywrightSpecRowFunction(RowFunction):
    def transform(
        self,
        output_dir: Optional[str] = None,
        name: str = "playwright",
        headless: Optional[bool] = None,
        isolated: Optional[bool] = None,
        allowed_hosts: Any = None,
        allow_tools: Any = None,
        deny_tools: Any = None,
        capture_final_screenshot: Optional[bool] = None,
        final_screenshot_filename: Optional[str] = None,
        start_mode: Optional[str] = None,
        command: Optional[str] = None,
        args: Any = None,
    ) -> dict[str, Any]:
        cli_args: list[str] = _coerce_string_list(args, field_name="args")
        if headless is not False:
            cli_args.append("--headless")
        if isolated is not False:
            cli_args.append("--isolated")
        hosts = _coerce_string_list(allowed_hosts, field_name="allowed_hosts") or ["*"]
        for host in hosts:
            cli_args.extend(["--allowed-hosts", host])
        cli_args.append("--no-sandbox")
        if output_dir is not None and str(output_dir).strip():
            cli_args.extend(["--output-dir", str(output_dir).strip()])
        chromium_executable = _playwright_chromium_executable()
        if chromium_executable is not None:
            cli_args.extend(["--executable-path", str(chromium_executable)])
        spec = McpStdioSpecRowFunction().transform(
            name=name,
            command=str(command).strip() if command is not None and str(command).strip() else "playwright-mcp",
            args=cli_args,
            required=True,
            allow_tools=allow_tools,
            deny_tools=deny_tools if deny_tools is not None else ["browser_install"],
            env={},
            default_tools_approval_mode="approve",
            start_mode=start_mode,
        )
        spec["playwright"] = {
            "output_dir": str(output_dir).strip() if output_dir is not None and str(output_dir).strip() else None,
            "capture_final_screenshot": bool(capture_final_screenshot),
            "final_screenshot_filename": (
                str(final_screenshot_filename).strip()
                if final_screenshot_filename is not None and str(final_screenshot_filename).strip()
                else "fixture-final.png"
            ),
        }
        return spec


class SimulatorRunRowFunction(AsyncRowFunction):
    def __init__(self, environment_provider: EnvironmentProvider | None = None) -> None:
        self.environment_provider = environment_provider or DefaultEnvironmentProvider()

    async def transform(
        self,
        input: Any,
        user: Any,
        agent: Any,
        observers: Any = None,
        environments: Any = None,
        reuse: Optional[str] = None,
        limits: Any = None,
        limiter: Any = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        parsed_limits = _coerce_limits(limits)
        try:
            return await _run_with_optional_limiter(
                limiter,
                asyncio.wait_for(
                    self._execute(
                        input=input,
                        user=user,
                        agent=agent,
                        observers=observers,
                        environments=environments,
                        reuse=reuse,
                        limits=parsed_limits,
                        started=started,
                    ),
                    timeout=parsed_limits.timeout_seconds,
                ),
            )
        except asyncio.TimeoutError:
            return _result_to_dict(
                SimulatorResult(
                    ok=False,
                    status="timeout",
                    final_output=None,
                    turns=(),
                    observations=(),
                    artifacts=(),
                    error=SimulatorError(code="timeout", message="Simulator run timed out", retryable=True),
                    duration_ms=_duration_ms(started),
                )
            )

    async def _execute(
        self,
        *,
        input: Any,
        user: Any,
        agent: Any,
        observers: Any,
        environments: Any,
        reuse: Optional[str],
        limits: SimulatorLimits,
        started: float,
    ) -> dict[str, Any]:
        reuse_mode = _coerce_reuse(reuse)
        observer_specs = _coerce_observer_specs(observers)
        environment_specs = _coerce_environment_specs(environments)
        user_callable = _resolve_function(user, expected_name="user")
        agent_callable = _resolve_function(agent, expected_name="agent")
        runtime_environments = RuntimeEnvironments()
        status = "completed"
        ok = True
        error: SimulatorError | None = None
        final_output: Any = None
        turns: list[TurnRecord] = []
        observations: list[ObservationRecord] = []
        artifacts: list[Any] = []
        state: dict[str, Any] = {
            "turns": [],
            "observations": [],
            "metadata": {"reuse": reuse_mode},
        }

        try:
            runtime_environments = await self.environment_provider.setup(environment_specs)
            current_request = input
            for turn_index in range(1, limits.max_turns + 1):
                try:
                    agent_raw = await _call_function(
                        agent_callable,
                        current_request,
                        _state_snapshot(state, turns, observations),
                        runtime_environments,
                        turn_index,
                        function_name=_function_display_name(agent),
                    )
                    agent_result = _coerce_mapping(agent_raw, function_name=_function_display_name(agent))
                    agent_response = _require_field(agent_result, "response", function_name=_function_display_name(agent))
                    state = _merge_callback_state(state, agent_result.get("state"), turns, observations)

                    user_raw = await _call_function(
                        user_callable,
                        agent_response,
                        _state_snapshot(state, turns, observations),
                        runtime_environments,
                        turn_index,
                        function_name=_function_display_name(user),
                    )
                    user_result = _coerce_mapping(user_raw, function_name=_function_display_name(user))
                    next_request = _require_field(user_result, "request", function_name=_function_display_name(user))
                    terminate = _coerce_terminate(user_result)
                    state = _merge_callback_state(state, user_result.get("state"), turns, observations)
                    turn = TurnRecord(
                        turn=turn_index,
                        request=current_request,
                        response=agent_response,
                        user_request=next_request,
                        terminate=terminate,
                    )
                    turns.append(turn)
                    state = _state_snapshot(state, turns, observations)
                    await self._run_observers(
                        observer_specs,
                        schedule="after_turn",
                        turn=turn_index,
                        state=state,
                        runtime_environments=runtime_environments,
                        observations=observations,
                        artifacts=artifacts,
                    )
                    state = _state_snapshot(state, turns, observations)
                    final_output = next_request if terminate else agent_response
                    if terminate:
                        status = "completed"
                        ok = True
                        break
                    current_request = next_request
                except Exception as exc:
                    callback_error = SimulatorError.from_exception(exc)
                    turns.append(
                        TurnRecord(
                            turn=turn_index,
                            request=current_request,
                            error=callback_error,
                        )
                    )
                    status = "failed"
                    ok = False
                    error = callback_error
                    break
            else:
                status = "max_turns"
                ok = False
                error = SimulatorError(code="max_turns", message=f"Simulator reached max_turns={limits.max_turns}")

            state = _state_snapshot(state, turns, observations)
            await self._run_observers(
                observer_specs,
                schedule="final",
                turn=turns[-1].turn if turns else 0,
                state=state,
                runtime_environments=runtime_environments,
                observations=observations,
                artifacts=artifacts,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = "failed"
            ok = False
            error = SimulatorError.from_exception(exc)
        finally:
            await self.environment_provider.teardown(
                runtime_environments,
                reason_code=status,
                message=error.message if error else None,
            )

        return _result_to_dict(
            SimulatorResult(
                ok=ok,
                status=status,
                final_output=final_output,
                turns=tuple(turns),
                observations=tuple(observations),
                artifacts=tuple(artifacts),
                error=error,
                duration_ms=_duration_ms(started),
            )
        )

    async def _run_observers(
        self,
        observer_specs: Sequence[ObserverSpec],
        *,
        schedule: str,
        turn: int,
        state: dict[str, Any],
        runtime_environments: RuntimeEnvironments,
        observations: list[ObservationRecord],
        artifacts: list[Any],
    ) -> None:
        for spec in observer_specs:
            if schedule not in spec.schedule:
                continue
            callback_name = _function_display_name(spec.callback)
            event = {"schedule": schedule, "turn": turn, "config": dict(spec.config)}
            try:
                observer_callable = _resolve_function(spec.callback, expected_name="observer")
                raw = await _call_function(
                    observer_callable,
                    event,
                    _state_snapshot(state, [], observations, preserve_turns=True),
                    runtime_environments,
                    function_name=callback_name,
                )
                result = _coerce_mapping(raw, function_name=callback_name)
                emitted_artifacts = tuple(_coerce_list(result.get("artifacts"), field_name="artifacts"))
                observations.append(
                    ObservationRecord(
                        callback=callback_name,
                        schedule=schedule,
                        turn=turn,
                        observation=result.get("observation"),
                        artifacts=emitted_artifacts,
                    )
                )
                artifacts.extend(emitted_artifacts)
                merged_state = _merge_callback_state(state, result.get("state"), [], observations, preserve_turns=True)
                state.clear()
                state.update(merged_state)
            except Exception as exc:
                observations.append(
                    ObservationRecord(
                        callback=callback_name,
                        schedule=schedule,
                        turn=turn,
                        error=SimulatorError.from_exception(exc),
                    )
                )


class SimulatorRunUdf(Udf, name="simulators.run"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            JsonType(),
            FunctionType(
                arguments=(
                    ("agent_response", "VARIANT"),
                    ("state", "VARIANT"),
                    ("environments", "ANY"),
                    ("turn", "INTEGER"),
                ),
                return_type_sql="STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>",
            ),
            FunctionType(
                arguments=(
                    ("request", "VARIANT"),
                    ("state", "VARIANT"),
                    ("environments", "ANY"),
                    ("turn", "INTEGER"),
                ),
                return_type_sql="STRUCT<response: VARIANT, state: VARIANT>",
            ),
            ArrayType(),
            ArrayType(),
            StringType(),
            JsonType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("input", required=True, type_sql="VARIANT"),
            Param("user", required=True, type_sql=USER_FUNCTION_TYPE_SQL),
            Param("agent", required=True, type_sql=AGENT_FUNCTION_TYPE_SQL),
            Param("observers", required=False, type_sql="ARRAY<VARIANT>", default_value=None),
            Param("environments", required=False, type_sql="ARRAY<VARIANT>", default_value=None),
            Param("reuse", required=False, type_sql="STRING", default_value="none"),
            Param("limits", required=False, type_sql="VARIANT", default_value=None),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": SIMULATOR_RESULT_TYPE_SQL}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return SimulatorRunRowFunction()


class SimulatorObserverUdf(Udf, name="simulators.observer"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            FunctionType(
                arguments=(("event", "VARIANT"), ("state", "VARIANT"), ("environments", "ANY")),
                return_type_sql="STRUCT<observation: VARIANT, artifacts: ARRAY<VARIANT>, state: VARIANT>",
            ),
            ArrayType(),
            JsonType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("callback", required=True, type_sql=OBSERVER_FUNCTION_TYPE_SQL),
            Param("schedule", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("config", required=False, type_sql="VARIANT", default_value=None),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return SimulatorObserverSpecRowFunction


class SimulatorLimitsUdf(Udf, name="simulators.limits"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (IntType(), FloatType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("max_turns", required=False, type_sql="INTEGER", default_value=DEFAULT_LIMITS["max_turns"]),
            Param("timeout_seconds", required=False, type_sql="DOUBLE", default_value=DEFAULT_LIMITS["timeout_seconds"]),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return SimulatorLimitsRowFunction


class EnvsBrowserSpecUdf(Udf, name="envs.browser.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), JsonType(), JsonType(), JsonType(), JsonType(), JsonType(), JsonType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("session_id", required=True, type_sql="STRING"),
            Param("start_url", required=False, type_sql="STRING", default_value=None),
            Param("policy", required=False, type_sql="VARIANT", default_value=None),
            Param("viewport", required=False, type_sql="VARIANT", default_value=None),
            Param("locale", required=False, type_sql="STRING", default_value=None),
            Param("timezone_id", required=False, type_sql="STRING", default_value=None),
            Param("storage_state_path", required=False, type_sql="STRING", default_value=None),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return BrowserEnvironmentSpecRowFunction


class EnvsShellSpecUdf(Udf, name="envs.shell.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), JsonType(), JsonType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("session_id", required=True, type_sql="STRING"),
            Param("cwd", required=False, type_sql="STRING", default_value=None),
            Param("policy", required=False, type_sql="VARIANT", default_value=None),
            Param("env", required=False, type_sql="ARRAY<VARIANT>", default_value=None),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return ShellEnvironmentSpecRowFunction


class EnvsAgentHarnessSpecUdf(Udf, name="envs.agent_harness.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType(), StringType(), JsonType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("session_id", required=True, type_sql="STRING"),
            Param("aisystem", required=True, type_sql="AISYSTEM"),
            Param("workdir", required=True, type_sql="STRING"),
            Param("secret_id", required=False, type_sql="SECRET", default_value=None),
            Param("mcps", required=False, type_sql="VARIANT", default_value=None),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return AgentHarnessEnvironmentSpecRowFunction


class EnvsMcpHttpSpecUdf(Udf, name="envs.mcp.http.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), BooleanType(), StringType(), ArrayType(), ArrayType(), JsonType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("name", required=True, type_sql="STRING"),
            Param("endpoint", required=True, type_sql="STRING"),
            Param("required", required=False, type_sql="BOOLEAN", default_value=False),
            Param("secret_id", required=False, type_sql="SECRET", default_value=None),
            Param("allow_tools", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("deny_tools", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("headers", required=False, type_sql="VARIANT", default_value=None),
            Param("start_mode", required=False, type_sql="STRING", default_value="lazy"),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return McpHttpSpecRowFunction


class EnvsMcpStdioSpecUdf(Udf, name="envs.mcp.stdio.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), ArrayType(), BooleanType(), ArrayType(), ArrayType(), JsonType(), StringType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("name", required=True, type_sql="STRING"),
            Param("command", required=True, type_sql="STRING"),
            Param("args", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("required", required=False, type_sql="BOOLEAN", default_value=False),
            Param("allow_tools", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("deny_tools", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("env", required=False, type_sql="VARIANT", default_value=None),
            Param("default_tools_approval_mode", required=False, type_sql="STRING", default_value=None),
            Param("start_mode", required=False, type_sql="STRING", default_value="lazy"),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return McpStdioSpecRowFunction


class EnvsMcpPlaywrightSpecUdf(Udf, name="envs.mcp.playwright.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            StringType(),
            BooleanType(),
            BooleanType(),
            ArrayType(),
            ArrayType(),
            ArrayType(),
            BooleanType(),
            StringType(),
            StringType(),
            StringType(),
            ArrayType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("output_dir", required=False, type_sql="STRING", default_value=None),
            Param("name", required=False, type_sql="STRING", default_value="playwright"),
            Param("headless", required=False, type_sql="BOOLEAN", default_value=True),
            Param("isolated", required=False, type_sql="BOOLEAN", default_value=True),
            Param("allowed_hosts", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("allow_tools", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("deny_tools", required=False, type_sql="ARRAY<STRING>", default_value=None),
            Param("capture_final_screenshot", required=False, type_sql="BOOLEAN", default_value=False),
            Param("final_screenshot_filename", required=False, type_sql="STRING", default_value="fixture-final.png"),
            Param("start_mode", required=False, type_sql="STRING", default_value="lazy"),
            Param("command", required=False, type_sql="STRING", default_value="playwright-mcp"),
            Param("args", required=False, type_sql="ARRAY<STRING>", default_value=None),
        )

    def metadata(self) -> dict[str, object]:
        return {"return_type_sql": "VARIANT", "pure": True}

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return McpPlaywrightSpecRowFunction


def _environment_registry_key(spec: EnvironmentSpec) -> tuple[Any, ...]:
    policy = ENV_FAMILY_POLICIES.get(spec.kind)
    if policy is None:
        raise ValueError(f"Unknown environment kind '{spec.kind}'")
    if not policy.allow_multiple_sessions:
        return (spec.kind,)
    values = []
    for field_name in policy.identity_fields:
        if field_name == "session_id":
            values.append(spec.env_id)
        else:
            values.append(spec.config.get(field_name))
    return (spec.kind, *values)


def _environment_spec_fingerprint(spec: EnvironmentSpec) -> dict[str, Any]:
    return {
        "kind": spec.kind,
        "env_id": spec.env_id,
        "config": _json_safe(dict(spec.config)),
    }


def _playwright_chromium_executable() -> Path | None:
    browser_root = Path("/ms-playwright")
    candidates = sorted(browser_root.glob("chromium-*/chrome-linux/chrome"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _lazy_environment_from_spec(spec: EnvironmentSpec) -> Any:
    if spec.kind == "agent_harness":
        return AgentHarnessEnvironmentHandle(env_id=spec.env_id, payload=dict(spec.config))
    setup_spec, environment = _environment_from_spec(spec)
    return LazyEnvironmentHandle(kind=spec.kind, env_id=spec.env_id, setup_spec=setup_spec, environment=environment)


def materialized_mcp_from_spec(spec: Mapping[str, Any]) -> MaterializedMcpHandle:
    normalized = _mcp_spec_payload(spec)
    command = str(normalized.get("command") or "").strip()
    metadata = normalized.get("playwright")
    if command == "playwright-mcp" or isinstance(metadata, Mapping):
        return MaterializedPlaywrightMcpHandle(normalized)
    return MaterializedMcpHandle(normalized)


def materialized_mcp_map(value: Any) -> dict[str, MaterializedMcpHandle]:
    return {name: materialized_mcp_from_spec(spec) for name, spec in _coerce_mcp_spec_map(value).items()}


def _mcp_spec_payload(spec: Mapping[str, Any] | MaterializedMcpHandle) -> Mapping[str, Any]:
    if isinstance(spec, MaterializedMcpHandle):
        return spec.to_mcp_spec()
    return spec


def _coerce_mcp_spec_map(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        items = value.items()
    else:
        items = ((None, item) for item in _coerce_list(value, field_name="mcps"))
    specs: dict[str, dict[str, Any]] = {}
    for raw_name, raw_spec in items:
        if isinstance(raw_spec, MaterializedMcpHandle):
            spec = dict(raw_spec.to_mcp_spec())
        elif isinstance(raw_spec, Mapping):
            spec = dict(raw_spec)
        else:
            raise ValueError("mcps values must be MCP spec objects")
        name = str(raw_name or spec.get("name") or "").strip()
        if not name:
            raise ValueError("MCP server name is required")
        spec["name"] = name
        specs[name] = _json_safe(spec)
    return specs


def _environment_from_spec(spec: EnvironmentSpec) -> tuple[Any, Any]:
    config = dict(spec.config)
    session_id = str(config.pop("session_id", f"simulator-{spec.env_id}"))
    if spec.kind == "browser":
        from agentcicd.fixtures.environments.browser.playwright_adapter import PlaywrightBrowserEnvironment
        from agentcicd.fixtures.environments.browser.types import BrowserPolicy, BrowserSetupSpec, Viewport

        setup_spec = BrowserSetupSpec(
            env_id=spec.env_id,
            session_id=session_id,
            start_url=str(config.get("start_url") or "about:blank"),
            viewport=_coerce_dataclass(Viewport, config.get("viewport")),
            locale=_optional_string(config.get("locale")),
            timezone_id=_optional_string(config.get("timezone_id")),
            storage_state_path=_optional_string(config.get("storage_state_path")),
            policy=_coerce_dataclass(BrowserPolicy, config.get("policy")),
        )
        return setup_spec, PlaywrightBrowserEnvironment()
    if spec.kind == "shell":
        from agentcicd.fixtures.environments.shell.subprocess_adapter import SubprocessShellEnvironment
        from agentcicd.fixtures.environments.shell.types import EnvironmentVariable, ShellPolicy, ShellSetupSpec

        setup_spec = ShellSetupSpec(
            env_id=spec.env_id,
            session_id=session_id,
            cwd=str(config.get("cwd") or "."),
            env=tuple(_coerce_dataclass(EnvironmentVariable, item) for item in _coerce_list(config.get("env"), field_name="env")),
            policy=_coerce_dataclass(ShellPolicy, config.get("policy")),
        )
        return setup_spec, SubprocessShellEnvironment()
    if spec.kind == "agent_harness":
        raise ValueError("agent_harness environments are lazy and do not use eager setup")
    raise ValueError(f"Unknown environment kind '{spec.kind}'")


def _coerce_limits(value: Any) -> SimulatorLimits:
    if value is None:
        data = {}
    elif isinstance(value, SimulatorLimits):
        return value
    elif isinstance(value, Mapping):
        data = dict(value)
    else:
        raise ValueError("limits must be a simulators.limits spec object")
    max_turns = data.get("max_turns", DEFAULT_LIMITS["max_turns"])
    timeout_seconds = data.get("timeout_seconds", DEFAULT_LIMITS["timeout_seconds"])
    try:
        parsed_max_turns = int(max_turns)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_turns must be an integer") from exc
    try:
        parsed_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be a number") from exc
    if parsed_max_turns < 1:
        raise ValueError("max_turns must be greater than or equal to 1")
    if parsed_timeout <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    return SimulatorLimits(max_turns=parsed_max_turns, timeout_seconds=parsed_timeout)


def _coerce_observer_specs(value: Any) -> tuple[ObserverSpec, ...]:
    specs = []
    for item in _coerce_list(value, field_name="observers"):
        if isinstance(item, ObserverSpec):
            specs.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ValueError("observers must contain simulator observer spec objects")
        if item.get("spec_type") != "simulator_observer":
            raise ValueError("observers must contain simulator observer spec objects")
        specs.append(
            ObserverSpec(
                callback=_require_field(item, "callback", function_name="simulators.observer"),
                schedule=_coerce_schedule(item.get("schedule")),
                config=_coerce_optional_mapping(item.get("config"), field_name="config"),
            )
        )
    return tuple(specs)


def _coerce_environment_specs(value: Any) -> tuple[EnvironmentSpec, ...]:
    specs = []
    seen_ids: set[str] = set()
    for item in _coerce_list(value, field_name="environments"):
        if isinstance(item, EnvironmentSpec):
            spec = item
        elif isinstance(item, Mapping):
            if item.get("spec_type") != "environment":
                raise ValueError("environments must contain env.* spec objects")
            spec = EnvironmentSpec(
                kind=_normalize_environment_kind(item.get("kind")),
                env_id=_coerce_env_id(item.get("env_id")),
                config=_coerce_optional_mapping(item.get("config"), field_name="config"),
            )
        else:
            raise ValueError("environments must contain env.* spec objects")
        if spec.env_id in seen_ids:
            raise ValueError(f"Duplicate environment env_id '{spec.env_id}'")
        seen_ids.add(spec.env_id)
        specs.append(spec)
    return tuple(specs)


def _coerce_reuse(value: Optional[str]) -> str:
    mode = str(value or "none").strip().lower()
    if mode not in SUPPORTED_REUSE_MODES:
        raise ValueError(f"reuse must be one of {sorted(SUPPORTED_REUSE_MODES)}")
    return mode


async def _run_with_optional_limiter(limiter: Any, awaitable: Any) -> Any:
    acquire = callable_attr(limiter, "acquire") if limiter is not None else None
    if acquire is None:
        return await awaitable
    async with acquire(permits=1):
        return await awaitable


def _coerce_mcp_start_mode(value: Optional[str]) -> str:
    mode = str(value or "lazy").strip().lower()
    if mode not in {"lazy", "early"}:
        raise ValueError("start_mode must be one of ['early', 'lazy']")
    return mode


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _replace_cli_option(args: list[str], option: str, value: str) -> list[str]:
    replaced: list[str] = []
    skip_next = False
    found = False
    prefix = f"{option}="
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == option:
            replaced.extend([option, value])
            found = True
            skip_next = index + 1 < len(args)
            continue
        if arg.startswith(prefix):
            replaced.append(f"{option}={value}")
            found = True
            continue
        replaced.append(arg)
    if not found:
        replaced.extend([option, value])
    return replaced


async def _wait_for_tcp_port(
    host: str,
    port: int,
    *,
    process: asyncio.subprocess.Process,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"MCP server exited before opening {host}:{port}")
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"Timed out waiting for MCP server at {host}:{port}") from last_error


def _coerce_schedule(value: Any) -> tuple[str, ...]:
    if value is None:
        raw_values = ["after_turn"]
    elif isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, Sequence):
        raw_values = list(value)
    else:
        raise ValueError("schedule must be an array of strings")
    schedules: list[str] = []
    for raw in raw_values:
        schedule = str(raw).strip().lower()
        if schedule not in SUPPORTED_OBSERVER_SCHEDULES:
            raise ValueError(f"Unsupported observer schedule '{raw}'")
        if schedule not in schedules:
            schedules.append(schedule)
    return tuple(schedules)


def _coerce_mapping(value: Any, *, function_name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"Function '{function_name}' returned {type(value).__name__}; expected an object/struct")


def _coerce_optional_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name} must be an object")


def _coerce_list(value: Any, *, field_name: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    raise ValueError(f"{field_name} must be an array")


def _coerce_json_safe_sequence(value: Any, *, field_name: str) -> list[Any]:
    return [_json_safe(item) for item in _coerce_list(value, field_name=field_name)]


def _coerce_string_list(value: Any, *, field_name: str) -> list[str]:
    values = _coerce_list(value, field_name=field_name)
    result: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if text:
            result.append(text)
    return result


def _coerce_terminate(value: Mapping[str, Any]) -> bool:
    if "terminate" not in value or value.get("terminate") is None:
        return True
    return bool(value.get("terminate"))


def _require_field(value: Mapping[str, Any], field: str, *, function_name: str) -> Any:
    if field not in value:
        raise ValueError(f"Function '{function_name}' returned no {field}")
    return value.get(field)


def _environment_spec_payload(kind: str, env_id: str, config: Mapping[str, Any]) -> EnvironmentSpecDict:
    return EnvironmentSpecDict({
        "spec_type": "environment",
        "kind": _normalize_environment_kind(kind),
        "env_id": _coerce_env_id(env_id),
        "config": _json_safe(dict(config)),
    })


def _normalize_environment_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in {"browser", "shell", "agent_harness"}:
        raise ValueError(f"Unknown environment kind '{value}'")
    return kind


def _coerce_env_id(value: Any) -> str:
    env_id = str(value or "").strip()
    if not env_id:
        raise ValueError("env_id is required")
    return env_id


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _drop_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _coerce_dataclass(cls: type[Any], value: Any) -> Any:
    if isinstance(value, cls):
        return value
    if value is None:
        return cls()
    if not isinstance(value, Mapping):
        raise ValueError(f"{cls.__name__} value must be an object")
    kwargs = {}
    field_map = {item.name: item for item in fields(cls)}
    for key, raw in value.items():
        if key not in field_map:
            raise ValueError(f"Unknown {cls.__name__} field '{key}'")
        kwargs[key] = _coerce_field_value(field_map[key].type, raw)
    return cls(**kwargs)


def _coerce_field_value(type_hint: Any, value: Any) -> Any:
    origin = get_origin(type_hint)
    args = get_args(type_hint)
    if origin is tuple and args:
        if value is None:
            return ()
        values = _coerce_list(value, field_name="tuple field")
        item_type = args[0]
        return tuple(_coerce_field_value(item_type, item) for item in values)
    if inspect.isclass(type_hint) and is_dataclass(type_hint):
        return _coerce_dataclass(type_hint, value)
    return value


def _merge_callback_state(
    current: Mapping[str, Any],
    returned: Any,
    turns: Sequence[TurnRecord],
    observations: Sequence[ObservationRecord],
    *,
    preserve_turns: bool = False,
) -> dict[str, Any]:
    if returned is None:
        merged = dict(current)
    elif isinstance(returned, Mapping):
        merged = dict(returned)
    else:
        raise ValueError("callback state must be an object when provided")
    if not preserve_turns:
        merged["turns"] = [_turn_to_dict(turn) for turn in turns]
    else:
        merged["turns"] = list(current.get("turns") or [])
    merged["observations"] = [_observation_to_dict(observation) for observation in observations]
    metadata = merged.get("metadata")
    if not isinstance(metadata, Mapping):
        merged["metadata"] = {}
    return merged


def _state_snapshot(
    state: Mapping[str, Any],
    turns: Sequence[TurnRecord],
    observations: Sequence[ObservationRecord],
    *,
    preserve_turns: bool = False,
) -> dict[str, Any]:
    return _merge_callback_state(state, None, turns, observations, preserve_turns=preserve_turns)


def _resolve_function(value: Any, *, expected_name: str) -> Callable[..., Any]:
    if callable(value):
        return value
    if isinstance(value, str) and value.strip():
        try:
            from agentcicd.sql.udf_registry import get_registered_udf, load_builtin_udfs
        except ImportError as exc:
            raise ValueError(f"Unknown {expected_name} function reference '{value}'") from exc

        load_builtin_udfs()
        udf_cls = get_registered_udf(value.strip())
        if udf_cls is None:
            raise ValueError(f"Unknown {expected_name} function reference '{value}'")
        udf = udf_cls()
        instance = udf.function()()
        transform = callable_attr(instance, "transform")
        if transform is None:
            raise ValueError(f"Function reference '{value}' cannot be called row-by-row")
        return transform
    raise ValueError(f"{expected_name} must be a callable or registered function reference")


async def _call_function(
    function: Callable[..., Any],
    *args: Any,
    function_name: str,
) -> Any:
    result = function(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _function_display_name(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return type_display_name(value)


def _result_to_dict(result: SimulatorResult) -> dict[str, Any]:
    return _json_safe(
        {
            "ok": result.ok,
            "status": result.status,
            "final_output": result.final_output,
            "turns": [_turn_to_dict(turn) for turn in result.turns],
            "observations": [_observation_to_dict(observation) for observation in result.observations],
            "artifacts": list(result.artifacts),
            "error": _error_to_dict(result.error),
            "duration_ms": result.duration_ms,
        }
    )


def _turn_to_dict(turn: TurnRecord) -> dict[str, Any]:
    return {
        "turn": turn.turn,
        "request": turn.request,
        "response": turn.response,
        "user_request": turn.user_request,
        "terminate": turn.terminate,
        "error": _error_to_dict(turn.error),
    }


def _observation_to_dict(observation: ObservationRecord) -> dict[str, Any]:
    return {
        "callback": observation.callback,
        "schedule": observation.schedule,
        "turn": observation.turn,
        "observation": observation.observation,
        "artifacts": list(observation.artifacts),
        "error": _error_to_dict(observation.error),
    }


def _error_to_dict(error: SimulatorError | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {"code": error.code, "message": error.message, "retryable": error.retryable}


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _model_to_data(value: Any) -> Any:
    model_dump = callable_attr(value, "model_dump")
    if model_dump is not None:
        return model_dump(mode="json")
    dict_method = callable_attr(value, "dict")
    if dict_method is not None:
        return dict_method()
    return value


def _mcp_result_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
