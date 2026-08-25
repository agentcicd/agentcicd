from __future__ import annotations

import asyncio

import pytest

from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import FType, JsonType, StringType
from agentcicd.fixtures.functions.string import ExtractFromFenceRowFunction, ExtractFromFenceUdf


@pytest.fixture()
def timeout_config() -> TimeoutConfig:
    return TimeoutConfig()


@pytest.fixture()
def retry_config() -> RetryConfig:
    return RetryConfig()


def test_extract_from_fence_udf_metadata() -> None:
    udf = ExtractFromFenceUdf()
    assert udf.input_args() == ("content", "fence_type")
    assert len(udf.input_schema()) == 2
    assert isinstance(udf.input_schema()[0], StringType)
    assert isinstance(udf.input_schema()[1], StringType)
    assert isinstance(udf.output_schema(), JsonType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), ExtractFromFenceRowFunction)


def test_extract_from_fence_returns_all_fences(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = ExtractFromFenceRowFunction()
    content = (
        "Lead text\n"
        "```json\n"
        "{\"score\": 1}\n"
        "```\n"
        "middle\n"
        "```sql\n"
        "SELECT 1;\n"
        "```\n"
    )

    result = asyncio.run(
        row_function.transform(
            content=content,
            fence_type=None,
            timeout=timeout_config,
            retry=retry_config,
        )
    )

    assert result == [
        ["json", "{\"score\": 1}"],
        ["sql", "SELECT 1;"],
    ]


def test_extract_from_fence_filters_requested_type(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = ExtractFromFenceRowFunction()
    content = (
        "```json\n"
        "{\"score\": 1}\n"
        "```\n"
        "```python\n"
        "x = 'x'\n"
        "```\n"
    )

    result = asyncio.run(
        row_function.transform(
            content=content,
            fence_type="json",
            timeout=timeout_config,
            retry=retry_config,
        )
    )

    assert result == ['{"score": 1}']


def test_extract_from_fence_returns_empty_for_unfenced_content(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = ExtractFromFenceRowFunction()

    result = asyncio.run(
        row_function.transform(
            content='{"score": 1}',
            fence_type="json",
            timeout=timeout_config,
            retry=retry_config,
        )
    )

    assert result == []


def test_extract_from_fence_handles_escaped_newlines(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = ExtractFromFenceRowFunction()

    result = asyncio.run(
        row_function.transform(
            content='```json\\n{"score": 1}\\n```',
            fence_type="json",
            timeout=timeout_config,
            retry=retry_config,
        )
    )

    assert result == ['{"score": 1}']
