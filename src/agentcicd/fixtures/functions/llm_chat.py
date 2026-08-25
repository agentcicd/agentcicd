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
    from agentcicd.fixtures.aisystem import CompletionRequest


class LiteLLMChatRowFunction(AsyncRowFunction):
    """LiteLLM chat/completions API wrapper."""

    expected_interface_type = "llm.chat"

    def __init__(self, runtime_context: RuntimeResolutionContext | None = None) -> None:
        super().__init__()
        self._session = None
        self._runtime_context = runtime_context
        self._resolver = AISystemRuntimeResolver(runtime_context)

    @staticmethod
    def _coerce_messages(
        messages: Optional[Mapping[str, Any]] | Optional[list[Mapping[str, Any]]],
    ) -> list[Mapping[str, Any]]:
        if isinstance(messages, list):
            return [dict(item) for item in messages if isinstance(item, Mapping)]
        if isinstance(messages, Mapping):
            return [dict(messages)]
        return []

    async def transform(
        self,
        aisystem_id: Optional[str],
        messages: Optional[Mapping[str, Any]] | Optional[list[Mapping[str, Any]]],
        request_timeout: Optional[float] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        n: Optional[int] = None,
        stream: Optional[bool] = None,
        stream_options: Optional[Mapping[str, Any]] = None,
        stop: Any = None,
        max_completion_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        logit_bias: Optional[Mapping[str, Any]] = None,
        user: Optional[str] = None,
        response_format: Optional[Mapping[str, Any]] = None,
        seed: Optional[int] = None,
        tools: Optional[list[Any]] = None,
        tool_choice: Optional[str] = None,
        parallel_tool_calls: Optional[bool] = None,
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
        safety_identifier: Optional[str] = None,
        deployment_id: Optional[str] = None,
        functions: Optional[list[Any]] = None,
        function_call: Optional[str] = None,
        base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        api_key: Optional[str] = None,
        model_list: Optional[list[Any]] = None,
        api_base: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        secret_id: Optional[str] = None,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
        system: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not aisystem_id:
            return None
        timeout = timeout or TimeoutConfig()
        from agentcicd.fixtures.aisystem import CompletionRequest, acompletion, create_aiohttp_session

        if self._session is None:
            self._session = create_aiohttp_session(timeout)

        resolved_messages = self._coerce_messages(messages)
        if not resolved_messages:
            return None

        resolved = self._resolver.resolve_litellm_payload(
            aisystem_id=aisystem_id,
            expected_interface_type=self.expected_interface_type,
            secret_id=secret_id,
        )

        payload: dict[str, Any] = {
            "model": resolved.model,
            "messages": resolved_messages,
            "system": system,
            "timeout": request_timeout,
            "temperature": temperature,
            "top_p": top_p,
            "n": n,
            "stream": stream,
            "stream_options": dict(stream_options) if isinstance(stream_options, Mapping) else None,
            "stop": stop,
            "max_completion_tokens": max_completion_tokens,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "logit_bias": dict(logit_bias) if isinstance(logit_bias, Mapping) else None,
            "user": user,
            "response_format": dict(response_format) if isinstance(response_format, Mapping) else None,
            "seed": seed,
            "tools": list(tools) if isinstance(tools, list) else None,
            "tool_choice": tool_choice,
            "parallel_tool_calls": parallel_tool_calls,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
            "safety_identifier": safety_identifier,
            "deployment_id": deployment_id,
            "functions": list(functions) if isinstance(functions, list) else None,
            "function_call": function_call,
            "base_url": base_url,
            "api_version": api_version,
            "api_key": api_key,
            "model_list": list(model_list) if isinstance(model_list, list) else None,
            "api_base": api_base,
            "extra_headers": dict(extra_headers) if isinstance(extra_headers, Mapping) else None,
            "shared_session": self._session,
        }
        payload = merge_litellm_payload_with_secret(
            payload,
            resolved.secret_id,
            self._runtime_context.as_options() if self._runtime_context is not None else payload,
        )
        response = await acompletion(CompletionRequest(**payload))
        return response.model_dump()


class AISystemsLLMChatUdf(Udf, name="aisystems.llm.chat"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            JsonType(),
            FloatType(),
            FloatType(),
            FloatType(),
            IntType(),
            BooleanType(),
            JsonType(),
            JsonType(),
            IntType(),
            IntType(),
            StringType(),
            FloatType(),
            FloatType(),
            JsonType(),
            StringType(),
            JsonType(),
            IntType(),
            JsonType(),
            StringType(),
            BooleanType(),
            BooleanType(),
            IntType(),
            StringType(),
            StringType(),
            JsonType(),
            StringType(),
            StringType(),
            StringType(),
            StringType(),
            JsonType(),
            StringType(),
            JsonType(),
            StringType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("aisystem_id", required=True),
            Param("messages", required=True),
            Param("request_timeout", required=False),
            Param("temperature", required=False),
            Param("top_p", required=False),
            Param("n", required=False),
            Param("stream", required=False),
            Param("stream_options", required=False),
            Param("stop", required=False),
            Param("max_completion_tokens", required=False),
            Param("max_tokens", required=False),
            Param("reasoning_effort", required=False),
            Param("presence_penalty", required=False),
            Param("frequency_penalty", required=False),
            Param("logit_bias", required=False),
            Param("user", required=False),
            Param("response_format", required=False),
            Param("seed", required=False),
            Param("tools", required=False),
            Param("tool_choice", required=False),
            Param("parallel_tool_calls", required=False),
            Param("logprobs", required=False),
            Param("top_logprobs", required=False),
            Param("safety_identifier", required=False),
            Param("deployment_id", required=False),
            Param("functions", required=False),
            Param("function_call", required=False),
            Param("base_url", required=False),
            Param("api_version", required=False),
            Param("api_key", required=False),
            Param("model_list", required=False),
            Param("api_base", required=False),
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
        return LiteLLMChatRowFunction()


class LiteLLMMessagesRowFunction(LiteLLMChatRowFunction):
    expected_interface_type = "llm.messages"


class AISystemsLLMMessagesUdf(Udf, name="aisystems.llm.messages"):
    def input_schema(self) -> Tuple[DType, ...]:
        return AISystemsLLMChatUdf().input_schema()

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return AISystemsLLMChatUdf().signature()

    def output_schema(self) -> DType:
        return JsonType()

    def metadata(self) -> dict[str, object]:
        return AISystemsLLMChatUdf().metadata()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return LiteLLMMessagesRowFunction()
