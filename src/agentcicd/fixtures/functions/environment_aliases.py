from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable, Tuple

from agentcicd.fixtures._attrs import callable_attr
from agentcicd.fixtures.core.function import AsyncRowFunction, RowFunction
from agentcicd.fixtures.core.types import ArrayType, BooleanType, DType, FloatType, FType, JsonType, StringType
from agentcicd.fixtures.core.udf import Param, Udf
from agentcicd.fixtures.functions.agent_harness import (
    DEFAULT_TIMEOUT_SECONDS,
    HARNESS_RUN_RESULT_TYPE_SQL,
    AgentHarnessRunTaskRowFunction,
)
from agentcicd.fixtures.functions.simulators import (
    EnvsAgentHarnessSpecUdf,
    EnvsMcpHttpSpecUdf,
    EnvsMcpPlaywrightSpecUdf,
    EnvsMcpStdioSpecUdf,
    MaterializedMcpHandle,
    MaterializedPlaywrightMcpHandle,
    McpHttpSpecRowFunction,
    McpPlaywrightSpecRowFunction,
    McpStdioSpecRowFunction,
    materialized_mcp_from_spec,
)


def _spec_metadata() -> dict[str, object]:
    return {"return_type_sql": "VARIANT", "pure": True}


def _operation_metadata(*, return_type_sql: str = "VARIANT") -> dict[str, object]:
    return {
        "return_type_sql": return_type_sql,
        "output_type": "variant",
        "execution_runtime": "function_runner",
        "sql_enabled": True,
    }


class AgentHarnessSpecUdf(EnvsAgentHarnessSpecUdf, name="agent_harness.spec"):
    def metadata(self) -> dict[str, object]:
        return _spec_metadata()


class McpsHttpSpecUdf(EnvsMcpHttpSpecUdf, name="mcps.http.spec"):
    def metadata(self) -> dict[str, object]:
        return _spec_metadata()


class McpsStdioSpecUdf(EnvsMcpStdioSpecUdf, name="mcps.stdio.spec"):
    def metadata(self) -> dict[str, object]:
        return _spec_metadata()


class McpsPlaywrightSpecUdf(EnvsMcpPlaywrightSpecUdf, name="mcps.playwright.spec"):
    def metadata(self) -> dict[str, object]:
        return _spec_metadata()


class AgentHarnessRunTaskAliasRowFunction(AsyncRowFunction):
    async def transform(
        self,
        spec: Any,
        task: str,
        timeout_seconds: float | None = None,
        transcript_file: str | None = None,
        pool: Any = None,
        limiter: Any = None,
    ) -> dict[str, Any]:
        return await AgentHarnessRunTaskRowFunction().transform(
            spec,
            task,
            timeout_seconds=timeout_seconds,
            transcript_file=transcript_file,
            pool=pool,
            limiter=limiter,
        )


class AgentHarnessRunTaskUdf(Udf, name="agent_harness.run_task"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (JsonType(), StringType(), FloatType(), StringType(), JsonType(), JsonType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("spec", required=True, type_sql="VARIANT"),
            Param("task", required=True, type_sql="STRING"),
            Param("timeout_seconds", required=False, type_sql="DOUBLE", default_value=DEFAULT_TIMEOUT_SECONDS),
            Param("transcript_file", required=False, type_sql="STRING", default_value=None),
            Param("pool", required=False, type_sql="POOL"),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def metadata(self) -> dict[str, object]:
        return {
            **_operation_metadata(return_type_sql=HARNESS_RUN_RESULT_TYPE_SQL),
            "entrypoint_name": "run_task",
            "pool_kind": "session",
        }

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., AsyncRowFunction]:
        return AgentHarnessRunTaskAliasRowFunction


class McpsSpecRowFunction(RowFunction):
    def transform(self, mcps: Any) -> dict[str, Any]:
        return _coerce_mcp_spec_map_public(mcps)


class McpsSpecUdf(Udf, name="mcps.spec"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (JsonType(),)

    def input_args(self) -> Tuple[str, ...]:
        return ("mcps",)

    def metadata(self) -> dict[str, object]:
        return _spec_metadata()

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return McpsSpecRowFunction


class PlaywrightNavigateRowFunction(AsyncRowFunction):
    async def transform(self, spec: Any, url: str, arguments: Any = None) -> Any:
        return await _with_playwright_handle(
            spec,
            lambda handle: handle.browser_navigate(url=url, **_coerce_mapping(arguments, "arguments")),
        )


class PlaywrightWaitForRowFunction(AsyncRowFunction):
    async def transform(self, spec: Any, text: str | None = None, time: float | None = None, arguments: Any = None) -> Any:
        return await _with_playwright_handle(
            spec,
            lambda handle: handle.browser_wait_for(text=text, time=time, **_coerce_mapping(arguments, "arguments")),
        )


class PlaywrightScreenshotRowFunction(AsyncRowFunction):
    async def transform(
        self,
        spec: Any,
        path: str | None = None,
        full_page: bool | None = None,
        arguments: Any = None,
    ) -> Any:
        return await _with_playwright_handle(
            spec,
            lambda handle: handle.browser_take_screenshot(
                path=path,
                full_page=True if full_page is None else bool(full_page),
                **_coerce_mapping(arguments, "arguments"),
            ),
        )


class PlaywrightTabsRowFunction(AsyncRowFunction):
    async def transform(
        self,
        spec: Any,
        action: str = "list",
        index: int | None = None,
        url: str | None = None,
        arguments: Any = None,
    ) -> Any:
        return await _with_playwright_handle(
            spec,
            lambda handle: handle.browser_tabs(
                action=action,
                index=index,
                url=url,
                **_coerce_mapping(arguments, "arguments"),
            ),
        )


class PlaywrightCallToolRowFunction(AsyncRowFunction):
    async def transform(self, spec: Any, tool_name: str, arguments: Any = None) -> Any:
        normalized_tool = str(tool_name or "").strip()
        if not normalized_tool:
            raise ValueError("tool_name is required")
        return await _with_playwright_handle(
            spec,
            lambda handle: handle._call_browser_tool(normalized_tool, **_coerce_mapping(arguments, "arguments")),
        )


class _PlaywrightOperationUdf(Udf):
    row_function: type[AsyncRowFunction]
    signature_items: Tuple[Param, ...]

    def input_schema(self) -> Tuple[DType, ...]:
        return tuple(_dtype_for_param(parameter) for parameter in self.signature())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return self.signature_items

    def metadata(self) -> dict[str, object]:
        return _operation_metadata()

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., AsyncRowFunction]:
        return self.row_function


class McpsPlaywrightBrowserNavigateUdf(_PlaywrightOperationUdf, name="mcps.playwright.browser.navigate"):
    row_function = PlaywrightNavigateRowFunction
    signature_items = (
        Param("spec", required=True, type_sql="VARIANT"),
        Param("url", required=True, type_sql="STRING"),
        Param("arguments", required=False, type_sql="VARIANT", default_value=None),
    )


class McpsPlaywrightBrowserWaitForUdf(_PlaywrightOperationUdf, name="mcps.playwright.browser.wait_for"):
    row_function = PlaywrightWaitForRowFunction
    signature_items = (
        Param("spec", required=True, type_sql="VARIANT"),
        Param("text", required=False, type_sql="STRING", default_value=None),
        Param("time", required=False, type_sql="DOUBLE", default_value=None),
        Param("arguments", required=False, type_sql="VARIANT", default_value=None),
    )


class McpsPlaywrightBrowserScreenshotUdf(_PlaywrightOperationUdf, name="mcps.playwright.browser.screenshot"):
    row_function = PlaywrightScreenshotRowFunction
    signature_items = (
        Param("spec", required=True, type_sql="VARIANT"),
        Param("path", required=False, type_sql="STRING", default_value=None),
        Param("full_page", required=False, type_sql="BOOLEAN", default_value=True),
        Param("arguments", required=False, type_sql="VARIANT", default_value=None),
    )


class McpsPlaywrightBrowserTabsUdf(_PlaywrightOperationUdf, name="mcps.playwright.browser.tabs"):
    row_function = PlaywrightTabsRowFunction
    signature_items = (
        Param("spec", required=True, type_sql="VARIANT"),
        Param("action", required=False, type_sql="STRING", default_value="list"),
        Param("index", required=False, type_sql="BIGINT", default_value=None),
        Param("url", required=False, type_sql="STRING", default_value=None),
        Param("arguments", required=False, type_sql="VARIANT", default_value=None),
    )


class McpsPlaywrightBrowserCallToolUdf(_PlaywrightOperationUdf, name="mcps.playwright.browser.call_tool"):
    row_function = PlaywrightCallToolRowFunction
    signature_items = (
        Param("spec", required=True, type_sql="VARIANT"),
        Param("tool_name", required=True, type_sql="STRING"),
        Param("arguments", required=False, type_sql="VARIANT", default_value=None),
    )


async def _with_playwright_handle(spec: Any, operation: Callable[[MaterializedPlaywrightMcpHandle], Any]) -> Any:
    handle, one_shot = _coerce_playwright_handle(spec)
    try:
        result = operation(handle)
        if hasattr(result, "__await__"):
            result = await result
        return result
    finally:
        if one_shot:
            await handle.teardown(type("Reason", (), {"code": "completed", "message": None})())


def _coerce_playwright_handle(value: Any) -> tuple[MaterializedPlaywrightMcpHandle, bool]:
    if isinstance(value, MaterializedPlaywrightMcpHandle):
        return value, False
    if isinstance(value, MaterializedMcpHandle):
        raise ValueError("mcps.playwright.browser functions require a Playwright MCP spec")
    handle = materialized_mcp_from_spec(_mcp_runtime_spec(value))
    if not isinstance(handle, MaterializedPlaywrightMcpHandle):
        raise ValueError("mcps.playwright.browser functions require a Playwright MCP spec")
    return handle, True


def _coerce_payload(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    as_dict = callable_attr(value, "asDict")
    if as_dict is not None:
        try:
            return as_dict(recursive=True)
        except TypeError:
            return as_dict()
    to_dict = callable_attr(value, "to_dict")
    if to_dict is not None:
        return to_dict()
    return value


def _mcp_runtime_spec(value: Any) -> Mapping[str, Any]:
    payload = _coerce_payload(value)
    if isinstance(payload, Mapping) and payload.get("spec_type") == "environment":
        kind = str(payload.get("kind") or "").strip().lower()
        config = payload.get("config")
        if not isinstance(config, Mapping):
            raise ValueError("MCP environment spec requires config")
        if kind == "mcp.http":
            return McpHttpSpecRowFunction().transform(**dict(config))
        if kind == "mcp.stdio":
            return McpStdioSpecRowFunction().transform(**dict(config))
        if kind == "mcp.playwright":
            return McpPlaywrightSpecRowFunction().transform(**dict(config))
    if not isinstance(payload, Mapping):
        raise ValueError("MCP spec must be an object")
    return payload


def _coerce_mapping(value: Any, field_name: str) -> dict[str, Any]:
    value = _coerce_payload(value)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return dict(value)


def _coerce_mcp_spec_map_public(value: Any) -> dict[str, Any]:
    from agentcicd.fixtures.functions.simulators import _coerce_mcp_spec_map

    payload = _coerce_payload(value)
    if isinstance(payload, Mapping):
        payload = {
            key: _mcp_runtime_spec(item)
            for key, item in payload.items()
        }
    return _coerce_mcp_spec_map(payload)


def _dtype_for_param(parameter: Param) -> DType:
    type_sql = parameter.type_sql.strip().upper()
    if type_sql == "STRING":
        return StringType()
    if type_sql == "BOOLEAN":
        return BooleanType()
    if type_sql in {"DOUBLE", "FLOAT"}:
        return FloatType()
    if type_sql.startswith("ARRAY"):
        return ArrayType()
    return JsonType()
