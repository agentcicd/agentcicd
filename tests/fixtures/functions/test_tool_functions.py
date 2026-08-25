from __future__ import annotations

import json

from agentcicd.fixtures.core.types import FType, JsonEncodedPydanticType
from agentcicd.fixtures.functions.tool import (
    ToolSchemaMatchResponse,
    ToolSchemaMatchRowFunction,
    ToolSchemaMatchUdf,
)


def test_tool_schema_match_udf_metadata() -> None:
    udf = ToolSchemaMatchUdf()

    assert udf.input_args() == ("tool_schema", "expected_call_properties", "tool_call")
    assert len(udf.input_schema()) == 3
    assert isinstance(udf.output_schema(), JsonEncodedPydanticType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), ToolSchemaMatchRowFunction)


def test_tool_schema_match_passes_matching_json_call() -> None:
    schema = json.dumps(
        {
            "name": "lookup_order",
            "parameters": {
                "type": "object",
                "required": ["order_id"],
                "properties": {
                    "order_id": {"type": "string"},
                    "include_refunds": {"type": "boolean"},
                },
            },
        }
    )
    expected = json.dumps({"equals": {"order_id": "ord_123", "include_refunds": True}})
    tool_call = json.dumps(
        {
            "name": "lookup_order",
            "arguments": {"order_id": "ord_123", "include_refunds": True},
        }
    )

    result = ToolSchemaMatchRowFunction().transform([schema], [expected], tool_call)

    assert isinstance(result, ToolSchemaMatchResponse)
    assert result.score == 1.0
    assert result.construction_failure_type == "none"
    assert result.malformed_fields == []


def test_tool_schema_match_fails_missing_required_argument() -> None:
    schema = json.dumps(
        {
            "parameters": {
                "required": ["order_id"],
                "properties": {"order_id": {"type": "string"}},
            }
        }
    )
    tool_call = json.dumps({"name": "lookup_order", "arguments": {}})

    result = ToolSchemaMatchRowFunction().transform([schema], [], tool_call)

    assert result.score == 0.0
    assert result.construction_failure_type == "missing_argument"
    assert result.malformed_fields == ["order_id"]


def test_tool_schema_match_fails_wrong_expected_argument_value() -> None:
    expected = json.dumps({"equals": {"order_id": "ord_123"}})
    tool_call = json.dumps({"name": "lookup_order", "arguments": {"order_id": "ord_456"}})

    result = ToolSchemaMatchRowFunction().transform([], [expected], tool_call)

    assert result.score == 0.0
    assert result.construction_failure_type == "wrong_argument_value"
    assert result.malformed_fields == ["order_id"]


def test_tool_schema_match_reports_invalid_call_shape() -> None:
    result = ToolSchemaMatchRowFunction().transform([], ["order_id"], "lookup_order ord_123")

    assert result.score == 0.0
    assert result.construction_failure_type == "invalid_call_shape"
    assert result.malformed_fields == ["tool_call"]
