from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional, Tuple

from pydantic import Field

from agentcicd.fixtures.core.function import Function, RowFunction
from agentcicd.fixtures.core.types import ArrayType, DType, FType, JsonEncodedPydanticType, AgentCICDModel, StringType
from agentcicd.fixtures.core.udf import Udf


class ToolSchemaMatchResponse(AgentCICDModel):
    """Deterministic tool-call/schema matching result."""

    score: float
    construction_failure_type: str
    malformed_fields: list[str] = Field(default_factory=list)
    expected_call: str
    actual_call: str
    reason: str


def _try_json(value: object) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if value is None:
        return []
    return [str(value)]


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _merge_json_objects(items: object) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in _as_str_list(items):
        parsed = _try_json(item)
        if isinstance(parsed, dict):
            merged.update(parsed)
    return merged


def _resolve_schema(schema_items: object) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    raw_schema = _merge_json_objects(schema_items)
    parameters = raw_schema.get("parameters") if isinstance(raw_schema.get("parameters"), dict) else raw_schema
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    raw_required = parameters.get("required")
    required = [str(item) for item in raw_required] if isinstance(raw_required, list) else []
    return raw_schema, required, properties


def _resolve_tool_call(tool_call: object) -> tuple[Optional[dict[str, Any]], str]:
    parsed = _try_json(tool_call)
    if not isinstance(parsed, dict):
        return None, "" if tool_call is None else str(tool_call)

    arguments = None
    for key in ("arguments", "args", "parameters", "input"):
        candidate = parsed.get(key)
        if isinstance(candidate, str):
            decoded = _try_json(candidate)
            if isinstance(decoded, dict):
                candidate = decoded
        if isinstance(candidate, dict):
            arguments = candidate
            break

    if arguments is None:
        # Treat a plain JSON object as the argument map when it is not wrapped in
        # a tool-call envelope.
        arguments = {
            key: value
            for key, value in parsed.items()
            if key not in {"name", "tool", "tool_name", "function", "id", "type"}
        }

    return arguments, _json_dumps(parsed)


def _parse_expected_properties(items: object) -> tuple[list[str], dict[str, Any], dict[str, str]]:
    required: list[str] = []
    equals: dict[str, Any] = {}
    contains: dict[str, str] = {}

    for item in _as_str_list(items):
        parsed = _try_json(item)
        if isinstance(parsed, dict):
            raw_required = parsed.get("required")
            if isinstance(raw_required, list):
                required.extend(str(value) for value in raw_required)

            raw_equals = parsed.get("equals")
            if isinstance(raw_equals, dict):
                equals.update(raw_equals)

            raw_contains = parsed.get("contains")
            if isinstance(raw_contains, dict):
                contains.update({str(key): str(value) for key, value in raw_contains.items()})

            simple_keys = {
                key: value
                for key, value in parsed.items()
                if key not in {"required", "equals", "contains", "forbidden", "notes"}
            }
            equals.update(simple_keys)
            continue

        text = item.strip()
        if not text:
            continue
        if "=" in text:
            key, value = text.split("=", 1)
            equals[key.strip()] = value.strip()
        else:
            required.append(text)

    return sorted(set(required)), equals, contains


def _values_equal(actual: object, expected: object) -> bool:
    if actual == expected:
        return True
    if actual is None or expected is None:
        return False
    return str(actual).strip() == str(expected).strip()


def _type_matches(value: object, schema: Mapping[str, Any]) -> bool:
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        return any(_type_matches(value, {"type": item}) for item in expected_type)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type in {"integer", "int"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type in {"number", "float"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return True


class ToolSchemaMatchRowFunction(RowFunction):
    """Check a tool call against JSON schema-like metadata and expected properties."""

    def transform(
        self,
        tool_schema: object,
        expected_call_properties: object,
        tool_call: object,
    ) -> ToolSchemaMatchResponse:
        raw_schema, schema_required, properties = _resolve_schema(tool_schema)
        arguments, actual_call = _resolve_tool_call(tool_call)

        if arguments is None:
            return ToolSchemaMatchResponse(
                score=0.0,
                construction_failure_type="invalid_call_shape",
                malformed_fields=["tool_call"],
                expected_call=_json_dumps(raw_schema or {"expected": _as_str_list(expected_call_properties)}),
                actual_call=actual_call,
                reason="The tool call is not valid JSON or a structured argument map.",
            )

        expected_required, expected_equals, expected_contains = _parse_expected_properties(expected_call_properties)
        required = sorted(set(schema_required + expected_required))

        malformed: list[str] = []
        failure_type = "none"

        for field in required:
            if field not in arguments or arguments.get(field) in (None, ""):
                malformed.append(field)
                failure_type = "missing_argument"

        for field, field_schema in properties.items():
            if field in arguments and isinstance(field_schema, dict) and not _type_matches(arguments[field], field_schema):
                malformed.append(str(field))
                if failure_type == "none":
                    failure_type = "wrong_argument_value"

            enum_values = field_schema.get("enum") if isinstance(field_schema, dict) else None
            if field in arguments and isinstance(enum_values, list) and arguments[field] not in enum_values:
                malformed.append(str(field))
                if failure_type == "none":
                    failure_type = "wrong_argument_value"

        for field, expected_value in expected_equals.items():
            if field not in arguments or not _values_equal(arguments.get(field), expected_value):
                malformed.append(str(field))
                if failure_type == "none":
                    failure_type = "wrong_argument_value"

        for field, expected_substring in expected_contains.items():
            actual_value = "" if field not in arguments or arguments.get(field) is None else str(arguments.get(field))
            if expected_substring not in actual_value:
                malformed.append(str(field))
                if failure_type == "none":
                    failure_type = "wrong_argument_value"

        malformed = sorted(set(malformed))
        score = 1.0 if not malformed else 0.0
        if not raw_schema and not expected_required and not expected_equals and not expected_contains:
            return ToolSchemaMatchResponse(
                score=0.0,
                construction_failure_type="insufficient_schema",
                malformed_fields=[],
                expected_call="{}",
                actual_call=actual_call,
                reason="No structured schema or expected call properties were provided for deterministic matching.",
            )

        return ToolSchemaMatchResponse(
            score=score,
            construction_failure_type=failure_type,
            malformed_fields=malformed,
            expected_call=_json_dumps(
                {
                    "required": required,
                    "properties": properties,
                    "equals": expected_equals,
                    "contains": expected_contains,
                }
            ),
            actual_call=actual_call,
            reason="The tool call matches the structured schema and expected properties." if score == 1.0 else "The tool call does not match the structured schema or expected properties.",
        )


class ToolSchemaMatchUdf(Udf, name="agent.tool.schema_match"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("tool_schema", "expected_call_properties", "tool_call")

    def output_schema(self) -> DType:
        return JsonEncodedPydanticType(ToolSchemaMatchResponse)

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return ToolSchemaMatchRowFunction()
