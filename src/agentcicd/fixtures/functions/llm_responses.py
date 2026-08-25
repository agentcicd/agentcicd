from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Tuple

from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import (
    BooleanType,
    DType,
    FType,
    FloatType,
    IntType,
    JsonType,
    StringType,
)
from agentcicd.fixtures.core.udf import Param, Udf

from .utils.runtime_context import (
    AISystemRuntimeResolver,
    RuntimeResolutionContext,
    merge_litellm_payload_with_secret,
)

if TYPE_CHECKING:
    from agentcicd.fixtures.aisystem import ResponsesRequest


class LiteLLMResponsesRowFunction(AsyncRowFunction):
    """LiteLLM responses API wrapper."""

    def __init__(self, runtime_context: RuntimeResolutionContext | None = None) -> None:
        super().__init__()
        self._session = None
        self._runtime_context = runtime_context
        self._resolver = AISystemRuntimeResolver(runtime_context)

    async def transform(
        self,
        prompt: Optional[str],
        aisystem_id: Optional[str],
        input_value: Any = None,
        instructions: Optional[str] = None,
        request_timeout: Optional[float] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        stream: Optional[bool] = None,
        tools: Optional[list[Any]] = None,
        tool_choice: Any = None,
        user: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        response_format: Optional[Mapping[str, Any]] = None,
        base_url: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        secret_id: Optional[str] = None,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> Optional[dict[str, Any]]:
        if not aisystem_id:
            return None
        timeout = timeout or TimeoutConfig()
        from agentcicd.fixtures.aisystem import ResponsesRequest, aresponse, create_aiohttp_session

        if self._session is None:
            self._session = create_aiohttp_session(timeout)

        resolved_input = input_value
        if resolved_input is None:
            resolved_input = prompt
        if resolved_input is None:
            return None

        resolved = self._resolver.resolve_litellm_payload(
            aisystem_id=aisystem_id,
            expected_interface_type="llm.responses",
            secret_id=secret_id,
        )

        payload: dict[str, Any] = {
            "model": resolved.model,
            "input": resolved_input,
            "instructions": instructions,
            "timeout": request_timeout,
            "temperature": temperature,
            "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "stream": stream,
            "tools": list(tools) if isinstance(tools, list) else None,
            "tool_choice": tool_choice,
            "user": user,
            "metadata": dict(metadata) if isinstance(metadata, Mapping) else None,
            "response_format": dict(response_format) if isinstance(response_format, Mapping) else None,
            "base_url": base_url,
            "api_base": api_base,
            "api_key": api_key,
            "extra_headers": dict(extra_headers) if isinstance(extra_headers, Mapping) else None,
            "shared_session": self._session,
        }
        payload = merge_litellm_payload_with_secret(
            payload,
            resolved.secret_id,
            self._runtime_context.as_options() if self._runtime_context is not None else payload,
        )
        response = await aresponse(ResponsesRequest(**payload))
        return response.model_dump()


class AISystemsLLMResponsesUdf(Udf, name="aisystems.llm.responses"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            StringType(),
            JsonType(),
            StringType(),
            FloatType(),
            FloatType(),
            FloatType(),
            IntType(),
            BooleanType(),
            JsonType(),
            JsonType(),
            StringType(),
            JsonType(),
            JsonType(),
            StringType(),
            StringType(),
            StringType(),
            JsonType(),
            StringType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("prompt", required=False),
            Param("aisystem_id", required=True),
            Param("input_value", required=False),
            Param("instructions", required=False),
            Param("request_timeout", required=False),
            Param("temperature", required=False),
            Param("top_p", required=False),
            Param("max_output_tokens", required=False),
            Param("stream", required=False),
            Param("tools", required=False),
            Param("tool_choice", required=False),
            Param("user", required=False),
            Param("metadata", required=False),
            Param("response_format", required=False),
            Param("base_url", required=False),
            Param("api_base", required=False),
            Param("api_key", required=False),
            Param("extra_headers", required=False),
            Param("secret_id", required=False),
            Param("limiter", required=False, type_sql="RATELIMIT"),
            Param("pool", required=False, type_sql="POOL"),
        )

    def output_schema(self) -> DType:
        return JsonType()

    def metadata(self) -> dict[str, object]:
        return {"pool_kind": "service"}

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return LiteLLMResponsesRowFunction()
