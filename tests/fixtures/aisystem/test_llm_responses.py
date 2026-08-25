from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from agentcicd_fixtures_aisystem.auth_config import ApiKeyAuth
from agentcicd_fixtures_aisystem.llm_responses import (
    ResponsesRequest,
    ResponsesResponse,
    _build_responses_kwargs,
    _extract_output_text,
    _responses_input_to_messages,
    aresponse,
)


pytestmark = pytest.mark.essential


def test_build_responses_kwargs_prefers_request_base_url_and_merges_auth_headers() -> None:
    request = ResponsesRequest(
        model="openai/gpt-4.1-mini",
        input="Score this support answer.",
        base_url="https://gateway.example.test/v1",
        api_base="https://ignored.example.test/v1",
        extra_headers={"X-Trace-Id": "trace-123", "X-API-Key": "old"},
    )
    auth = ApiKeyAuth(api_key="fresh-key", additional_params={"timeout": 45})

    kwargs = _build_responses_kwargs(request, auth)

    assert kwargs["api_base"] == "https://gateway.example.test/v1"
    assert kwargs["stream"] is False
    assert kwargs["timeout"] == 45
    assert kwargs["extra_headers"] == {"X-Trace-Id": "trace-123", "X-API-Key": "fresh-key"}
    assert "base_url" not in kwargs


def test_extract_output_text_collects_nested_response_content_blocks() -> None:
    response = ResponsesResponse(
        output=[
            {"content": [{"type": "output_text", "text": "First paragraph."}]},
            {"text": "Second paragraph."},
            {"content": [{"type": "image"}]},
        ]
    )

    extracted = _extract_output_text(response)

    assert extracted == "First paragraph.\nSecond paragraph."


@pytest.mark.parametrize(
    ("input_value", "expected_messages"),
    [
        ("hello", [{"role": "user", "content": "hello"}]),
        ({"role": "system", "content": "Be precise."}, [{"role": "system", "content": "Be precise."}]),
        (
            [
                {"role": "user", "content": "Score this"},
                {"role": "assistant", "content": "Ready"},
                "ignored",
            ],
            [
                {"role": "user", "content": "Score this"},
                {"role": "assistant", "content": "Ready"},
            ],
        ),
    ],
)
def test_responses_input_to_messages_supports_fallback_completion_payloads(
    input_value: object,
    expected_messages: list[dict[str, str]],
) -> None:
    assert _responses_input_to_messages(input_value) == expected_messages


def test_aresponse_uses_litellm_responses_api_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentcicd_fixtures_aisystem.llm_responses as module

    captured: dict[str, object] = {}

    async def fake_aresponses(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "id": "resp.123",
            "status": "completed",
            "output": [{"content": [{"text": "The answer is grounded."}]}],
            "usage": {"input_tokens": 12, "output_tokens": 6},
        }

    monkeypatch.setattr(module.litellm, "aresponses", fake_aresponses, raising=False)

    response = asyncio.run(
        aresponse(
            ResponsesRequest(model="openai/gpt-4.1-mini", input="Evaluate response."),
            auth_config=ApiKeyAuth(api_key="key-123", url="https://gateway.example.test/v1"),
        )
    )

    assert captured["api_base"] == "https://gateway.example.test/v1"
    assert captured["stream"] is False
    assert response.id == "resp.123"
    assert response.output_text == "The answer is grounded."


def test_aresponse_falls_back_to_completion_api_without_litellm_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentcicd_fixtures_aisystem.llm_responses as module

    monkeypatch.delattr(module.litellm, "aresponses", raising=False)
    completion = Mock(
        id="chatcmpl.123",
        created=123456,
        model="openai/gpt-4.1-mini",
        choices=[{"message": {"content": "Fallback answer"}}],
        usage={"total_tokens": 10},
    )
    fake_acompletion = AsyncMock(return_value=completion)
    monkeypatch.setattr(module, "acompletion", fake_acompletion)

    response = asyncio.run(
        aresponse(
            ResponsesRequest(
                model="openai/gpt-4.1-mini",
                input=[{"role": "user", "content": "Evaluate response."}],
                max_output_tokens=200,
            )
        )
    )

    completion_request = fake_acompletion.call_args.args[0]
    assert completion_request.messages == [{"role": "user", "content": "Evaluate response."}]
    assert completion_request.max_tokens == 200
    assert response.id == "chatcmpl.123"
    assert response.output_text == "Fallback answer"
