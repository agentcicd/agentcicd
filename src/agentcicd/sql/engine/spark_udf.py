from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Optional

from pyspark.sql import SparkSession
from pyspark.sql.functions import pandas_udf
from pyspark.sql.functions import udf as spark_udf
from pyspark.sql.types import (
    ArrayType,
    BooleanType as SparkBooleanType,
    DataType,
    DoubleType,
    IntegerType,
    LongType,
    MapType,
    NullType,
    StringType as SparkStringType,
    StructField,
    StructType,
    VariantType,
)

from agentcicd.sql.runtime.udf_compat.function import AsyncRowFunction, Function
from agentcicd.sql.runtime.udf_compat.runtime_control import runtime_limiter
from agentcicd.sql.runtime.udf_compat.types import DType, FType
from agentcicd.sql.runtime.udf_compat.types import StringType
from agentcicd.sql.runtime.udf_compat.udf import Udf
from agentcicd.sql.udf_registry import registered_udf_name

logger = logging.getLogger(__name__)


def _normalize_nulls(value: Any) -> Any:
    try:
        from pyspark.sql.types import VariantVal

        if isinstance(value, VariantVal):
            return _normalize_nulls(value.toPython())
    except Exception:
        pass
    if isinstance(value, dict):
        return {key: _normalize_nulls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_nulls(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_nulls(item) for item in value)

    import pandas as pd

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _single_part_alias(udf_name: str) -> str:
    return udf_name.replace(".", "_")


def _unwrap_nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        non_null = [
            item
            for item in any_of
            if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(non_null) == 1:
            return dict(non_null[0])
    return schema


def _json_schema_to_spark_type(schema: dict[str, Any]) -> DataType:
    normalized = _unwrap_nullable_schema(dict(schema or {}))
    schema_type = str(normalized.get("type") or "").strip().lower()
    if schema_type in {"json", "variant"}:
        return VariantType()
    if schema_type == "string":
        return SparkStringType()
    if schema_type == "integer" and str(normalized.get("format") or "").strip().lower() == "int64":
        return LongType()
    if schema_type == "integer":
        return IntegerType()
    if schema_type == "number":
        return DoubleType()
    if schema_type == "boolean":
        return SparkBooleanType()
    if schema_type == "null":
        return NullType()
    if schema_type == "array":
        return ArrayType(_json_schema_to_spark_type(dict(normalized.get("items") or {"type": "string"})))
    if schema_type == "object":
        properties = normalized.get("properties")
        if isinstance(properties, dict) and properties:
            required = {
                str(item)
                for item in normalized.get("required", [])
                if isinstance(item, str) and item.strip()
            }
            fields = [
                StructField(
                    str(name),
                    _json_schema_to_spark_type(dict(value or {})),
                    nullable=str(name) not in required,
                )
                for name, value in properties.items()
            ]
            return StructType(fields)
        additional = normalized.get("additionalProperties")
        if isinstance(additional, dict):
            return MapType(SparkStringType(), _json_schema_to_spark_type(additional), valueContainsNull=True)
    return SparkStringType()


def _dtype_to_spark(dtype: DType) -> DataType:
    spark_type_class = dtype.spark_interface()
    return spark_type_class()  # type: ignore[operator]


def _validate_arg_count(udf_name: str, columns: tuple[Any, ...], input_schema: tuple[DType, ...]) -> None:
    expected_count = len(input_schema)
    if len(columns) != expected_count:
        raise ValueError(f"{udf_name} expects {expected_count} arguments but received {len(columns)}")


def _column_to_pylist(column: Any) -> list[Any]:
    import pandas as pd
    import pyarrow as pa

    try:
        from pyspark.sql.types import VariantVal

        if isinstance(column, VariantVal):
            return [_normalize_nulls(column)]
    except Exception:
        pass
    if isinstance(column, pd.DataFrame):
        return [_normalize_nulls(row) for row in column.to_dict(orient="records")]
    if isinstance(column, pd.Series):
        return [_normalize_nulls(value) for value in column.tolist()]
    if isinstance(column, (pa.Array, pa.ChunkedArray)):
        return [_normalize_nulls(value) for value in column.to_pylist()]
    if isinstance(column, (str, bytes, int, float, bool, Decimal)) or column is None:
        return [_normalize_nulls(column)]
    if isinstance(column, dict):
        return [_normalize_nulls(column)]
    if isinstance(column, (list, tuple)):
        return [_normalize_nulls(value) for value in column]
    return [_normalize_nulls(value) for value in list(column)]


def _to_pyarrow_columns(columns: tuple[Any, ...]) -> list[Any]:
    import pyarrow as pa

    return [pa.array(_column_to_pylist(column)) for column in columns]


def _has_vector_input(columns: tuple[Any, ...]) -> bool:
    import pandas as pd
    import pyarrow as pa

    return any(isinstance(column, (pd.DataFrame, pd.Series, pa.Array, pa.ChunkedArray)) for column in columns)


def _collect_arrow_results(result_iterator: Any) -> list[Any]:
    results = []
    for result_array in result_iterator:
        results.extend(result_array.to_pylist())
    return results


def _coerce_string_results(results: list[Any]) -> list[Optional[str]]:
    coerced: list[Optional[str]] = []
    for value in results:
        if value is None:
            coerced.append(None)
        elif isinstance(value, str):
            coerced.append("null" if value.strip().lower() == "none" else value)
        elif isinstance(value, bytes):
            coerced.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, (dict, list)):
            coerced.append(json.dumps(_normalize_nulls(value), ensure_ascii=False))
        else:
            coerced.append(str(value))
    return coerced


def _coerce_result_for_output_schema(value: Any, output_schema: DType) -> Any:
    if value is None:
        return None
    if isinstance(output_schema, StringType):
        return _coerce_string_results([value])[0]
    return output_schema.from_internal(value)


def _coerce_results_for_output_schema(results: list[Any], output_schema: DType) -> list[Any]:
    if isinstance(output_schema, StringType):
        return _coerce_string_results(results)
    return [_coerce_result_for_output_schema(value, output_schema) for value in results]


def _register_with_alias(
    spark: SparkSession,
    udf_name: str,
    udf_func: Any,
    udf_kind: str,
) -> None:
    spark.udf.register(udf_name, udf_func)
    logger.info("Registered %s UDF '%s'", udf_kind, udf_name)

    alias = _single_part_alias(udf_name)
    if alias == udf_name:
        return

    spark.udf.register(alias, udf_func)
    logger.info("Registered %s UDF alias '%s' -> '%s'", udf_kind, alias, udf_name)


def register_spark_udf(
    spark: SparkSession,
    udf_cls: type[Udf],
    udf_name: Optional[str] = None,
    control_arg_indexes: set[int] | None = None,
) -> None:
    resolved_udf_name = udf_name or registered_udf_name(udf_cls)
    if resolved_udf_name is None:
        raise ValueError(f"UDF class {udf_cls.__name__} is not registered.")

    udf_instance = udf_cls()
    input_schema = udf_instance.input_schema()
    output_schema = udf_instance.output_schema()
    function_factory = udf_instance.function()
    function_instance = function_factory()
    function_type = udf_instance.ftype()
    limiter_key = "default"
    control_indexes = set(control_arg_indexes or set())
    control_indexes.update(_control_indexes_from_udf_signature(udf_instance))
    setattr(function_instance, "_agentcicd_rate_limit_key", limiter_key)

    if function_type == FType.BATCH_FUNCTION:
        _register_spark_batch_udf(
            spark,
            resolved_udf_name,
            function_instance,
            input_schema,
            output_schema,
            limiter_key,
            control_indexes,
        )
        return
    if function_type == FType.ROW_EXPLODE_FUNCTION:
        _register_spark_row_explode_udf(
            spark,
            resolved_udf_name,
            function_instance,
            input_schema,
            output_schema,
            limiter_key,
            control_indexes,
        )
        return
    if function_type == FType.AGGREGATE_FUNCTION:
        _register_spark_aggregate_udf(
            spark,
            resolved_udf_name,
            function_instance,
            input_schema,
            output_schema,
            limiter_key,
            control_indexes,
        )
        return

    logger.warning("Unknown function type %s for UDF '%s'; skipping", function_type, resolved_udf_name)


def _register_spark_batch_udf(
    spark: SparkSession,
    udf_name: str,
    function_instance: Function,
    input_schema: tuple[DType, ...],
    output_schema: DType,
    limiter_key: str,
    control_arg_indexes: set[int],
) -> None:
    return_type = _dtype_to_spark(output_schema)

    def _batch_udf(*columns: Any) -> Any:
        data_columns, rate_limit_values = _split_control_columns(columns, control_arg_indexes)
        _validate_arg_count(udf_name, data_columns, input_schema)
        resolved_limiter_key, max_in_flight = _limiter_from_control_values(rate_limit_values, fallback_key=limiter_key)
        _set_async_runtime_limiter(function_instance, resolved_limiter_key, max_in_flight)
        with _runtime_limit_for_function(function_instance, resolved_limiter_key, max_in_flight):
            result_iterator = function_instance(*_to_pyarrow_columns(data_columns))
            results = _collect_arrow_results(result_iterator)
        if _has_vector_input(columns):
            import pandas as pd

            return pd.Series(_coerce_results_for_output_schema(results, output_schema))
        if len(results) != 1:
            raise ValueError(f"{udf_name} expected one result for a scalar row call but received {len(results)}")
        return _coerce_result_for_output_schema(results[0], output_schema)

    _register_with_alias(spark, udf_name, spark_udf(_batch_udf, return_type, useArrow=True), "batch")


def _register_spark_row_explode_udf(
    spark: SparkSession,
    udf_name: str,
    function_instance: Function,
    input_schema: tuple[DType, ...],
    output_schema: DType,
    limiter_key: str,
    control_arg_indexes: set[int],
) -> None:
    return_type = _dtype_to_spark(output_schema)

    def _row_explode_udf(*columns: Any) -> Any:
        data_columns, rate_limit_values = _split_control_columns(columns, control_arg_indexes)
        _validate_arg_count(udf_name, data_columns, input_schema)
        resolved_limiter_key, max_in_flight = _limiter_from_control_values(rate_limit_values, fallback_key=limiter_key)
        _set_async_runtime_limiter(function_instance, resolved_limiter_key, max_in_flight)
        with _runtime_limit_for_function(function_instance, resolved_limiter_key, max_in_flight):
            result_iterator = function_instance(*_to_pyarrow_columns(data_columns))
            results = _collect_arrow_results(result_iterator)
        if _has_vector_input(columns):
            import pandas as pd

            return pd.Series(_coerce_results_for_output_schema(results, output_schema))
        return _coerce_results_for_output_schema(results, output_schema)

    _register_with_alias(spark, udf_name, spark_udf(_row_explode_udf, return_type, useArrow=True), "row explode")


def _register_spark_aggregate_udf(
    spark: SparkSession,
    udf_name: str,
    function_instance: Function,
    input_schema: tuple[DType, ...],
    output_schema: DType,
    limiter_key: str,
    control_arg_indexes: set[int],
) -> None:
    import pandas as pd

    return_type = _dtype_to_spark(output_schema)

    @pandas_udf(return_type)  # type: ignore[call-overload]
    def _aggregate_udf(*columns: pd.Series) -> Any:
        data_columns, rate_limit_values = _split_control_columns(columns, control_arg_indexes)
        _validate_arg_count(udf_name, data_columns, input_schema)
        resolved_limiter_key, max_in_flight = _limiter_from_control_values(rate_limit_values, fallback_key=limiter_key)
        _set_async_runtime_limiter(function_instance, resolved_limiter_key, max_in_flight)
        with _runtime_limit_for_function(function_instance, resolved_limiter_key, max_in_flight):
            return _coerce_result_for_output_schema(function_instance(*data_columns), output_schema)

    _register_with_alias(spark, udf_name, _aggregate_udf, "aggregate")


def _runtime_limit_for_function(function_instance: Function, limiter_key: str, max_in_flight: int | None):
    if isinstance(function_instance, AsyncRowFunction):
        return _null_context()
    return runtime_limiter(max_in_flight, key=limiter_key).acquire_blocking(permits=1)


def _set_async_runtime_limiter(function_instance: Function, limiter_key: str, max_in_flight: int | None) -> None:
    if not isinstance(function_instance, AsyncRowFunction):
        return
    setattr(function_instance, "_agentcicd_rate_limit_key", limiter_key)
    setattr(function_instance, "_agentcicd_rate_limit_max_in_flight", max_in_flight)


@contextmanager
def _null_context():
    yield


def _split_control_columns(columns: tuple[Any, ...], control_arg_indexes: set[int]) -> tuple[tuple[Any, ...], list[object]]:
    if not control_arg_indexes:
        return columns, []
    data_columns = tuple(column for index, column in enumerate(columns) if index not in control_arg_indexes)
    control_values = [
        _first_control_value(column)
        for index, column in enumerate(columns)
        if index in control_arg_indexes
    ]
    return data_columns, control_values


def _control_indexes_from_udf_signature(udf_instance: Udf) -> set[int]:
    try:
        parameters = tuple(udf_instance.signature())
    except Exception:
        return set()
    return {
        index
        for index, parameter in enumerate(parameters)
        if str(getattr(parameter, "type_sql", "") or "").strip().upper() in {"RATELIMIT", "POOL"}
    }


def _first_control_value(column: Any) -> object:
    values = _column_to_pylist(column)
    return values[0] if values else None


def _limiter_from_control_values(values: list[object], *, fallback_key: str) -> tuple[str, int | None]:
    for value in values:
        payload = _rate_limit_payload(value)
        if payload is None:
            continue
        key = str(payload.get("key") or fallback_key).strip() or fallback_key
        raw_max = payload.get("max_in_flight")
        max_in_flight = int(raw_max) if raw_max is not None else None
        return key, max_in_flight
    return fallback_key, None


def _rate_limit_payload(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    as_dict = getattr(value, "asDict", None)
    if callable(as_dict):
        return dict(as_dict(recursive=True))
    if hasattr(value, "key") or hasattr(value, "max_in_flight"):
        return {
            "key": getattr(value, "key", None),
            "max_in_flight": getattr(value, "max_in_flight", None),
        }
    return None
