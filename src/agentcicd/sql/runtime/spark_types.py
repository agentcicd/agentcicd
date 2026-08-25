from __future__ import annotations

import json
from typing import Any

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DataType,
    DoubleType,
    IntegerType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
    VariantType,
)

from agentcicd.sql.engine.spark_udf import _json_schema_to_spark_type
from agentcicd.sql.ir.functions import FunctionDefinitionIR


def _metadata_return_type(metadata: dict[str, object]) -> DataType:
    output_schema = metadata.get("output_schema")
    if isinstance(output_schema, dict):
        return _json_schema_to_spark_type(output_schema)
    output_type = str(metadata.get("output_type") or "").strip().lower()
    if output_type in {"json", "variant"}:
        return VariantType()
    return StringType()

def _definition_return_type(definition: FunctionDefinitionIR) -> DataType:
    metadata = getattr(definition, "metadata", {}) or {}
    return_type = _metadata_return_type(metadata)
    if not isinstance(return_type, StringType) or metadata.get("output_schema") is not None or metadata.get("output_type") is not None:
        return return_type
    return _type_sql_to_spark(getattr(definition, "return_type_sql", None))

def _type_sql_to_spark(type_sql: object) -> DataType:
    raw_type = str(type_sql or "").strip().upper()
    if raw_type in {"BOOLEAN", "BOOL"}:
        return BooleanType()
    if raw_type == "BIGINT":
        return LongType()
    if raw_type in {"INT", "INTEGER", "SMALLINT", "TINYINT"}:
        return IntegerType()
    if raw_type in {"DOUBLE", "FLOAT", "REAL", "NUMBER", "NUMERIC", "DECIMAL"}:
        return DoubleType()
    if raw_type in {"VARIANT", "JSON"}:
        return VariantType()
    if raw_type:
        try:
            return DataType.fromDDL(raw_type)
        except Exception:
            pass
    return StringType()

def _cell_return_type(value_type: DataType) -> StructType:
    return StructType(
        [
            StructField("cell_id", StringType(), True),
            StructField("value", value_type, True),
            StructField(
                "metadata",
                StructType(
                    [
                        StructField("errors", _errors_return_type(), False),
                        StructField("latency_ms", LongType(), True),
                        StructField("fixture_trace", _fixture_trace_return_type(), True),
                    ]
                ),
                False,
            ),
            StructField("__agentcicd_cell", BooleanType(), False),
        ]
    )

def _errors_return_type() -> ArrayType:
    return ArrayType(
        StructType(
            [
                StructField("code", StringType(), False),
                StructField("message", StringType(), False),
                StructField("source", StringType(), True),
                StructField("path", StringType(), True),
                StructField("recoverable", BooleanType(), False),
                StructField("cause_code", StringType(), True),
                StructField("cause_message", StringType(), True),
                StructField("details", MapType(StringType(), StringType(), True), False),
            ]
        ),
        False,
    )

def _fixture_trace_return_type() -> StructType:
    return StructType(
        [
            StructField("schema_version", StringType(), True),
            StructField("call_id", StringType(), True),
            StructField("parent_call_id", StringType(), True),
            StructField("trace_id", StringType(), True),
            StructField("span_id", StringType(), True),
            StructField("parent_span_id", StringType(), True),
            StructField("function_name", StringType(), True),
            StructField("runtime_alias", StringType(), True),
            StructField("backend", StringType(), True),
            StructField("fixture_id", StringType(), True),
            StructField("image_id", StringType(), True),
            StructField("execution_runtime", StringType(), True),
            StructField("status", StringType(), True),
            StructField("duration_ms", LongType(), True),
            StructField("cache_hit", BooleanType(), True),
            StructField("limiter_key", StringType(), True),
            StructField("max_in_flight", LongType(), True),
            StructField("pool_name", StringType(), True),
            StructField("pool_kind", StringType(), True),
            StructField("pool_node_id", StringType(), True),
            StructField("http_status", LongType(), True),
            StructField("error_code", StringType(), True),
            StructField("error_message", StringType(), True),
            StructField("error_type", StringType(), True),
            StructField("artifact_count", LongType(), True),
            StructField("summary", StringType(), True),
            StructField("top_error", StringType(), True),
            StructField("span_count", LongType(), True),
            StructField("error_count", LongType(), True),
            StructField("trace_summary_path", StringType(), True),
            StructField("trace_spans_path", StringType(), True),
        ]
    )

def _coerce_remote_result(value: object, return_type: DataType) -> object:
    if value is None:
        return None
    value = _unwrap_cell_payload(value)
    if isinstance(return_type, VariantType):
        return _variant_result(value)
    if isinstance(return_type, StringType):
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    if isinstance(return_type, StructType):
        if not isinstance(value, dict):
            return value
        return {
            field.name: _coerce_remote_result(value.get(field.name), field.dataType)
            for field in return_type.fields
        }
    if isinstance(return_type, MapType):
        if not isinstance(value, dict):
            return value
        return {
            str(key): _coerce_remote_result(item, return_type.valueType)
            for key, item in value.items()
        }
    if isinstance(return_type, ArrayType):
        if not isinstance(value, list):
            return value
        return [_coerce_remote_result(item, return_type.elementType) for item in value]
    return value

def _unwrap_cell_payload(value: object) -> object:
    if isinstance(value, dict):
        if value.get("__agentcicd_cell") is True and "value" in value:
            return _unwrap_cell_payload(value.get("value"))
        return {key: _unwrap_cell_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_cell_payload(item) for item in value]
    return value

def _variant_result(value: object) -> object:
    if value is None:
        return None
    from pyspark.sql.types import VariantVal

    if isinstance(value, str):
        try:
            json.loads(value)
            raw_json = value
        except json.JSONDecodeError:
            raw_json = json.dumps(value, ensure_ascii=False)
    else:
        raw_json = json.dumps(value, ensure_ascii=False)
    return VariantVal.parseJson(raw_json)
