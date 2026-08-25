from __future__ import annotations

import json
from typing import Any, Mapping
from unittest.mock import AsyncMock, patch

import pytest

from agentcicd.fixtures.aisystem import CompletionRequest, CompletionResponse
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import FType, JsonType, StringType
from agentcicd.fixtures.functions.llm_chat import AISystemsLLMChatUdf, LiteLLMChatRowFunction
from agentcicd.fixtures.functions.utils.runtime_context import resolve_litellm_payload_from_aisystem
from agentcicd.fixtures.functions.utils.runtime_context import resolve_aisystem


@pytest.fixture()
def timeout_config() -> TimeoutConfig:
    return TimeoutConfig()


@pytest.fixture()
def retry_config() -> RetryConfig:
    return RetryConfig()


def test_litellm_chat_udf_metadata() -> None:
    udf = AISystemsLLMChatUdf()
    input_schema = udf.input_schema()
    assert udf.input_args() == (
        "aisystem_id",
        "messages",
        "request_timeout",
        "temperature",
        "top_p",
        "n",
        "stream",
        "stream_options",
        "stop",
        "max_completion_tokens",
        "max_tokens",
        "reasoning_effort",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "user",
        "response_format",
        "seed",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "logprobs",
        "top_logprobs",
        "safety_identifier",
        "deployment_id",
        "functions",
        "function_call",
        "base_url",
        "api_version",
        "api_key",
        "model_list",
        "api_base",
        "extra_headers",
        "secret_id",
        "limiter",
        "pool",
    )
    assert len(input_schema) == len(udf.input_args()) - 2
    assert udf.signature()[-2].type_sql == "RATELIMIT"
    assert udf.signature()[-1].type_sql == "POOL"
    assert isinstance(input_schema[0], StringType)
    assert isinstance(input_schema[1], JsonType)
    assert isinstance(udf.output_schema(), JsonType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), LiteLLMChatRowFunction)


@pytest.mark.asyncio
async def test_litellm_chat_row_function_reuses_session(
    tmp_path,
    monkeypatch,
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    session_sentinel = object()
    captured_payloads: list[Mapping[str, Any]] = []

    async def _fake_completion(request: CompletionRequest) -> CompletionResponse:
        payload = request.model_dump()
        captured_payloads.append(payload)
        return CompletionResponse(
            model=request.model,
            choices=[{"message": {"content": "response"}}],
        )

    row_function = LiteLLMChatRowFunction()
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.openai",
                        "name": "openai/gpt-4",
                        "target": "openai/gpt-4",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "aisystem_secret_bindings": [
                    {
                        "id": "aisystem_secret_binding.openai",
                        "organization_id": "org.test",
                        "aisystem_id": "aisystem.openai",
                        "secret_id": "secret.1",
                        "is_default": True,
                        "status": "active",
                    }
                ],
                "secrets": [
                    {
                        "id": "secret.1",
                        "key": "openai",
                        "secret": {"type": "api_key", "api_key": "sk-test"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    with patch(
        "agentcicd_fixtures.aisystem.create_aiohttp_session",
        return_value=session_sentinel,
    ) as create_session, patch(
        "agentcicd_fixtures.aisystem.acompletion",
        new=AsyncMock(side_effect=_fake_completion),
    ):
        first = await row_function.transform(
            aisystem_id="aisystem.openai",
            messages=[{"role": "user", "content": "Say hi"}],
            timeout=timeout_config,
            retry=retry_config,
            temperature=0.2,
            reasoning_effort="medium",
            extra_headers={"x-test": "1"},
            secret_id="secret.1",
        )
        second = await row_function.transform(
            aisystem_id="aisystem.openai",
            messages=[{"role": "user", "content": "Say hi"}],
            timeout=timeout_config,
            retry=retry_config,
            secret_id="secret.1",
        )

    assert (first or {}).get("choices", [{}])[0].get("message", {}).get("content") == "response"
    assert (second or {}).get("choices", [{}])[0].get("message", {}).get("content") == "response"
    assert create_session.call_count == 1
    assert captured_payloads[0]["model"] == "openai/gpt-4"
    assert captured_payloads[0]["messages"] == [{"role": "user", "content": "Say hi"}]
    assert captured_payloads[0]["temperature"] == 0.2
    assert captured_payloads[0]["reasoning_effort"] == "medium"
    assert captured_payloads[0]["extra_headers"] == {"x-test": "1"}
    assert captured_payloads[0]["api_key"] == "sk-test"


@pytest.mark.asyncio
async def test_litellm_chat_row_function_canonicalizes_unprefixed_claude_model(
    tmp_path,
    monkeypatch,
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    captured_payloads: list[Mapping[str, Any]] = []

    async def _fake_completion(request: CompletionRequest) -> CompletionResponse:
        payload = request.model_dump()
        captured_payloads.append(payload)
        return CompletionResponse(
            model=request.model,
            choices=[{"message": {"content": "response"}}],
        )

    row_function = LiteLLMChatRowFunction()
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.anthropic",
                        "name": "claude-3-5-haiku-latest",
                        "target": "claude-3-5-haiku-latest",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "aisystem_secret_bindings": [
                    {
                        "id": "aisystem_secret_binding.anthropic",
                        "organization_id": "org.test",
                        "aisystem_id": "aisystem.anthropic",
                        "secret_id": "secret.1",
                        "is_default": True,
                        "status": "active",
                    }
                ],
                "secrets": [
                    {
                        "id": "secret.1",
                        "key": "anthropic",
                        "secret": {"type": "api_key", "api_key": "sk-ant-test"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    with patch(
        "agentcicd_fixtures.aisystem.acompletion",
        new=AsyncMock(side_effect=_fake_completion),
    ):
        await row_function.transform(
            aisystem_id="aisystem.anthropic",
            messages=[{"role": "user", "content": "Say hi"}],
            response_format={"type": "json_object"},
            timeout=timeout_config,
            retry=retry_config,
            secret_id="secret.1",
        )

    assert captured_payloads[0]["model"] == "anthropic/claude-3-5-haiku-latest"
    assert captured_payloads[0]["api_key"] == "sk-ant-test"


@pytest.mark.asyncio
async def test_litellm_chat_row_function_preserves_explicit_messages(
    tmp_path,
    monkeypatch,
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    captured_payloads: list[Mapping[str, Any]] = []

    async def _fake_completion(request: CompletionRequest) -> CompletionResponse:
        payload = request.model_dump()
        captured_payloads.append(payload)
        return CompletionResponse(
            model=request.model,
            choices=[{"message": {"content": "response"}}],
        )

    row_function = LiteLLMChatRowFunction()
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.anthropic",
                        "name": "claude-3-5-haiku-latest",
                        "target": "claude-3-5-haiku-latest",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "aisystem_secret_bindings": [
                    {
                        "id": "aisystem_secret_binding.anthropic",
                        "organization_id": "org.test",
                        "aisystem_id": "aisystem.anthropic",
                        "secret_id": "secret.1",
                        "is_default": True,
                        "status": "active",
                    }
                ],
                "secrets": [
                    {
                        "id": "secret.1",
                        "key": "anthropic",
                        "secret": {"type": "api_key", "api_key": "sk-ant-test"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    with patch(
        "agentcicd_fixtures.aisystem.acompletion",
        new=AsyncMock(side_effect=_fake_completion),
    ):
        await row_function.transform(
            aisystem_id="aisystem.anthropic",
            messages=[{"role": "user", "content": "Judge this answer"}],
            response_format={"type": "json_object"},
            timeout=timeout_config,
            retry=retry_config,
            secret_id="secret.1",
        )

    assert captured_payloads[0]["model"] == "anthropic/claude-3-5-haiku-latest"
    assert captured_payloads[0]["messages"] == [{"role": "user", "content": "Judge this answer"}]


@pytest.mark.asyncio
async def test_litellm_chat_row_function_requires_messages(
    tmp_path,
    monkeypatch,
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = LiteLLMChatRowFunction()
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.openai",
                        "name": "openai/gpt-4",
                        "target": "openai/gpt-4",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "aisystem_secret_bindings": [
                    {
                        "id": "aisystem_secret_binding.openai",
                        "organization_id": "org.test",
                        "aisystem_id": "aisystem.openai",
                        "secret_id": "secret.1",
                        "is_default": True,
                        "status": "active",
                    }
                ],
                "secrets": [
                    {
                        "id": "secret.1",
                        "key": "openai",
                        "secret": {"type": "api_key", "api_key": "sk-test"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    with patch("agentcicd_fixtures.aisystem.acompletion", new=AsyncMock()) as completion:
        result = await row_function.transform(
            aisystem_id="aisystem.openai",
            messages=None,
            timeout=timeout_config,
            retry=retry_config,
            secret_id="secret.1",
        )

    assert result is None
    completion.assert_not_called()


@pytest.mark.asyncio
async def test_litellm_chat_row_function_rejects_string_messages(
    tmp_path,
    monkeypatch,
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = LiteLLMChatRowFunction()
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.openai",
                        "name": "openai/gpt-4",
                        "target": "openai/gpt-4",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "aisystem_secret_bindings": [
                    {
                        "id": "aisystem_secret_binding.openai",
                        "organization_id": "org.test",
                        "aisystem_id": "aisystem.openai",
                        "secret_id": "secret.1",
                        "is_default": True,
                        "status": "active",
                    }
                ],
                "secrets": [
                    {
                        "id": "secret.1",
                        "key": "openai",
                        "secret": {"type": "api_key", "api_key": "sk-test"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    with patch("agentcicd_fixtures.aisystem.acompletion", new=AsyncMock()) as completion:
        result = await row_function.transform(
            aisystem_id="aisystem.openai",
            messages='[{"role":"user","content":"Judge this answer"}]',
            timeout=timeout_config,
            retry=retry_config,
            secret_id="secret.1",
        )

    assert result is None
    completion.assert_not_called()


def test_resolve_litellm_payload_from_aisystem_uses_default_secret() -> None:
    resolved = resolve_litellm_payload_from_aisystem(
        aisystem_id="aisystem.openai",
        expected_interface_type="llm.chat",
        options={
            "aisystems_by_id": {
                "aisystem.openai": {
                    "id": "aisystem.openai",
                    "name": "openai/gpt-4",
                    "target": "openai/gpt-4",
                    "interface": {"interface_type": "llm.chat"},
                }
            },
            "aisystem_secret_bindings": [
                {
                    "id": "aisystem_secret_binding.1",
                    "organization_id": "org.test",
                    "aisystem_id": "aisystem.openai",
                    "secret_id": "secret.1",
                    "is_default": True,
                    "status": "active",
                }
            ],
        },
    )
    assert resolved == {"model": "openai/gpt-4", "secret_id": "secret.1"}


def test_resolve_aisystem_accepts_generic_http_interface_for_http_get() -> None:
    resolved = resolve_aisystem(
        aisystem_id="aisystem.http",
        expected_interface_type="http.get",
        options={
            "aisystems_by_id": {
                "aisystem.http": {
                    "id": "aisystem.http",
                    "name": "http system",
                    "target": "https://api.example.com",
                    "interface": {"interface_type": "http"},
                    "interfaces": [{"id": "iface.http", "interface_type": "http"}],
                }
            }
        },
    )
    assert resolved["interface"]["interface_type"] == "http"
