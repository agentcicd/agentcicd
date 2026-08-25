from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import FType, JsonType, IntType, StringType
from agentcicd.fixtures.functions.simple_agent import SimpleAgentChatRowFunction, SimpleAgentChatUdf


@pytest.fixture()
def timeout_config() -> TimeoutConfig:
    return TimeoutConfig()


@pytest.fixture()
def retry_config() -> RetryConfig:
    return RetryConfig()


def test_simple_agent_udf_metadata() -> None:
    udf = SimpleAgentChatUdf()
    input_schema = udf.input_schema()
    assert isinstance(input_schema[0], StringType)
    assert isinstance(input_schema[1], StringType)
    assert isinstance(input_schema[2], StringType)
    assert isinstance(input_schema[3], StringType)
    assert isinstance(input_schema[4], IntType)
    assert isinstance(input_schema[5], JsonType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), SimpleAgentChatRowFunction)


@pytest.mark.asyncio
async def test_simple_agent_chat_generates_otel_and_executes_tool(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "lookup_order",
                                    "arguments": '{"order_id":"#W1"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
        {
            "choices": [{"message": {"content": "Your order is shipped.", "tool_calls": []}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        },
    ]

    async def _fake_completion(request):  # noqa: ANN001
        payload = responses.pop(0)
        class _Resp:
            def model_dump(self_nonlocal):  # noqa: ANN001
                return payload
        return _Resp()

    row_function = SimpleAgentChatRowFunction()
    tools = [
        {
            "name": "lookup_order",
            "description": "Lookup order details",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            "handler": {"type": "static", "output": {"status": "shipped"}},
        }
    ]
    with patch(
        "agentcicd_fixtures.functions.simple_agent.create_aiohttp_session",
        return_value=object(),
    ), patch(
        "agentcicd_fixtures.functions.simple_agent.acompletion",
        new=AsyncMock(side_effect=_fake_completion),
    ):
        raw = await row_function.transform(
            json.dumps({"messages": [{"role": "user", "content": "Where is my order?"}]}),
            json.dumps(tools),
            json.dumps({"mode": "scripted", "turns": []}),
            "Where is my order?",
            3,
            {"model": "gpt-4.1-mini"},
            timeout_config,
            retry_config,
        )
    parsed = json.loads(raw or "{}")
    assert "resourceSpans" in parsed
    assert parsed["summary"]["termination_reason"] in {"user_script_done", "max_turns"}
    tool_messages = [m for m in parsed["trajectory"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["status"] == "shipped"
