from __future__ import annotations

from agentcicd.fixtures.core.types import FType, JsonType, StringType
from agentcicd.fixtures.functions.llm_chat import AISystemsLLMChatUdf, LiteLLMChatRowFunction
from agentcicd.fixtures.functions.llm_responses import AISystemsLLMResponsesUdf, LiteLLMResponsesRowFunction


def test_litellm_chat_udf_metadata() -> None:
    udf = AISystemsLLMChatUdf()

    input_schema = udf.input_schema()
    assert udf.input_args()[-2:] == ("limiter", "pool")
    assert len(input_schema) == len(udf.input_args()) - 2
    assert udf.signature()[-2].type_sql == "RATELIMIT"
    assert udf.signature()[-1].type_sql == "POOL"
    assert isinstance(input_schema[0], StringType)
    assert isinstance(input_schema[1], JsonType)
    assert isinstance(input_schema[7], JsonType)
    assert isinstance(udf.output_schema(), JsonType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), LiteLLMChatRowFunction)


def test_litellm_responses_udf_metadata() -> None:
    udf = AISystemsLLMResponsesUdf()

    input_schema = udf.input_schema()
    assert udf.input_args() == (
        "prompt",
        "aisystem_id",
        "input_value",
        "instructions",
        "request_timeout",
        "temperature",
        "top_p",
        "max_output_tokens",
        "stream",
        "tools",
        "tool_choice",
        "user",
        "metadata",
        "response_format",
        "base_url",
        "api_base",
        "api_key",
        "extra_headers",
        "secret_id",
        "limiter",
        "pool",
    )
    assert len(input_schema) == len(udf.input_args()) - 2
    assert udf.signature()[-2].type_sql == "RATELIMIT"
    assert udf.signature()[-1].type_sql == "POOL"
    assert isinstance(input_schema[0], StringType)
    assert isinstance(input_schema[1], StringType)
    assert isinstance(input_schema[2], JsonType)
    assert isinstance(udf.output_schema(), JsonType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), LiteLLMResponsesRowFunction)
