from __future__ import annotations

from typing import Any

from sqlglot import expressions as exp

from agentcicd.sql.engine.backends.spark.common import F, Window
from agentcicd.sql.engine.cell_metadata import ERROR_ARRAY_SQL_TYPE, FIXTURE_TRACE_SQL_TYPE
from agentcicd.sql.engine.stable_hashing import stable_hash
from agentcicd.sql.ir.options import StatementOptions


class SparkCellMixin:
    @staticmethod
    def _should_wrap_load_cells(options: StatementOptions, *, default: bool) -> bool:
        if not default:
            return False
        wrap_cells = options.get("wrap_cells")
        if wrap_cells is not None:
            if not SparkCellMixin._is_truthy_option(wrap_cells):
                raise ValueError("Wrapped mode always wraps loaded columns; remove WRAP_CELLS=false.")
            return True
        wrap = options.get("wrap")
        if wrap is not None:
            if not SparkCellMixin._is_truthy_option(wrap):
                raise ValueError("Wrapped mode always wraps loaded columns; remove WRAP=false.")
            return True
        return True

    @staticmethod
    def _is_truthy_option(value: Any) -> bool:
        normalized = str(value).strip().lower()
        return normalized in {"1", "true", "yes", "on", "cell", "cells", "wrapped", "metadata"}

    def _unwrap_cell_dataframe(self, dataframe):
        columns = []
        for field in dataframe.schema.fields:
            column_name = field.name
            column = F.col(column_name)
            if self._is_cell_struct_type(field.dataType):
                column = self._cell_value_col(field.dataType, column)
            columns.append(column.alias(column_name))
        return dataframe.select(*columns)

    def _wrap_loaded_dataframe(self, dataframe, *, source_name: str):
        stage_id = self._stage_id(source_name, "load_table", None)
        dataframe = self._with_deterministic_row_ordinal(dataframe, preserve_input_order=False)
        row_id = self._hash_columns(
            F.lit(stage_id),
            F.lit("source"),
            F.lit(source_name),
            F.col("__agentcicd_ingest_ordinal").cast("string"),
        )
        dataframe = dataframe.withColumn("__agentcicd_row_id", row_id)
        wrapped_columns = []
        for field in dataframe.schema.fields:
            column_name = field.name
            if column_name == "__agentcicd_ingest_ordinal":
                continue
            if column_name == "__agentcicd_row_id":
                wrapped_columns.append(F.col(column_name))
                continue
            if self._is_cell_struct_type(field.dataType):
                if not self._is_current_cell_struct_type(field.dataType):
                    raise ValueError(
                        f"Wrapped input column '{column_name}' uses an unsupported cell schema; "
                        "migrate it to the current wrapped cell schema before loading"
                    )
                wrapped_columns.append(
                    self._cell_struct(
                        column_name,
                        value_col=self._cell_value_col(field.dataType, F.col(column_name)),
                        errors_col=self._cell_errors_col(field.dataType, F.col(column_name)),
                        latency_col=self._cell_latency_col(field.dataType, F.col(column_name)),
                        fixture_trace_col=self._cell_fixture_trace_col(field.dataType, F.col(column_name)),
                        stage_id=stage_id,
                    ).alias(column_name)
                )
                continue
            wrapped_columns.append(
                self._cell_struct(
                    column_name,
                    value_col=F.col(column_name),
                    errors_col=F.array().cast(ERROR_ARRAY_SQL_TYPE),
                    stage_id=stage_id,
                ).alias(column_name)
            )
        return dataframe.select(*wrapped_columns)

    def _normalize_materialized_dataframe(self, dataframe, *, stage_name: str, stage_kind: str, sql: str | None):
        cell_columns = [
            field.name
            for field in getattr(dataframe.schema, "fields", [])
            if self._is_cell_struct_type(getattr(field, "dataType", None))
        ]
        if not cell_columns:
            return dataframe
        stage_id = self._stage_id(stage_name, stage_kind, sql)
        dataframe = self._with_deterministic_row_ordinal(dataframe, preserve_input_order=True)
        dataframe = dataframe.withColumn(
            "__agentcicd_row_id",
            self._hash_columns(
                F.lit(stage_id),
                F.lit(stage_kind),
                F.col("__agentcicd_ingest_ordinal").cast("string"),
            ),
        )
        projections = [F.col("__agentcicd_row_id")]
        for field in dataframe.schema.fields:
            column_name = field.name
            if column_name in {"__agentcicd_ingest_ordinal", "__agentcicd_row_id"}:
                continue
            if column_name in cell_columns:
                projections.append(
                    self._cell_struct(
                        column_name,
                        value_col=self._cell_value_col(field.dataType, F.col(column_name)),
                        errors_col=self._cell_errors_col(field.dataType, F.col(column_name)),
                        latency_col=self._cell_latency_col(field.dataType, F.col(column_name)),
                        fixture_trace_col=self._cell_fixture_trace_col(field.dataType, F.col(column_name)),
                        stage_id=stage_id,
                    ).alias(column_name)
                )
            else:
                projections.append(F.col(column_name))
        return dataframe.select(*projections)

    def _cell_struct(
        self,
        column_name: str,
        *,
        value_col: Any,
        errors_col: Any,
        stage_id: str,
        latency_col: Any | None = None,
        fixture_trace_col: Any | None = None,
    ):
        cell_id = self._hash_columns(F.lit(stage_id), F.col("__agentcicd_row_id"), F.lit(column_name))
        return F.struct(
            cell_id.alias("cell_id"),
            value_col.alias("value"),
            F.struct(
                F.coalesce(errors_col, F.array().cast(ERROR_ARRAY_SQL_TYPE)).alias("errors"),
                (latency_col if latency_col is not None else F.lit(None).cast("bigint")).alias("latency_ms"),
                fixture_trace_col.alias("fixture_trace")
                if fixture_trace_col is not None
                else F.lit(None).cast(FIXTURE_TRACE_SQL_TYPE).alias("fixture_trace"),
            ).alias("metadata"),
            F.lit(True).alias("__agentcicd_cell"),
        )

    @classmethod
    def _cell_latency_col(cls, data_type: Any, column: Any):
        if cls._cell_metadata_has_latency(data_type):
            return column["metadata"]["latency_ms"].cast("bigint")
        return F.lit(None).cast("bigint")

    @classmethod
    def _cell_fixture_trace_col(cls, data_type: Any, column: Any):
        if cls._cell_metadata_has_fixture_trace(data_type):
            return column["metadata"]["fixture_trace"]
        return F.lit(None).cast(FIXTURE_TRACE_SQL_TYPE)

    @classmethod
    def _cell_value_col(cls, data_type: Any, column: Any):
        value_col = column["value"]
        value_type = cls._cell_value_type(data_type)
        if cls._is_cell_struct_type(value_type):
            return cls._cell_value_col(value_type, value_col)
        return value_col

    @classmethod
    def _cell_errors_col(cls, data_type: Any, column: Any):
        errors_col = F.coalesce(column["metadata"]["errors"], F.array().cast(ERROR_ARRAY_SQL_TYPE))
        value_type = cls._cell_value_type(data_type)
        value_col = column["value"]
        if cls._is_cell_struct_type(value_type):
            return F.concat(errors_col, cls._cell_errors_col(value_type, value_col))
        return errors_col

    @staticmethod
    def _cell_value_type(data_type: Any) -> Any | None:
        try:
            return data_type["value"].dataType
        except Exception:
            return None

    def _with_deterministic_row_ordinal(self, dataframe, *, preserve_input_order: bool):
        if preserve_input_order:
            sort_columns = [F.monotonically_increasing_id()]
        else:
            sort_columns = [
                F.col(field.name).cast("string").asc_nulls_first()
                for field in dataframe.schema.fields
                if field.name != "__agentcicd_row_id"
            ]
        if not sort_columns:
            sort_columns = [F.lit(1)]
        return dataframe.withColumn(
            "__agentcicd_ingest_ordinal",
            F.row_number().over(Window.orderBy(*sort_columns)) - F.lit(1),
        )

    @staticmethod
    def _hash_columns(*columns):
        return F.substring(F.sha2(F.concat_ws("|", *columns), 256), 1, 32)

    def _stage_id(self, name: str, stage_kind: str, sql: str | None) -> str:
        expected = self._expected_stage_manifests.get(name.lower())
        if expected is not None and expected.fingerprint:
            return expected.fingerprint[:32]
        return stable_hash({"stage_name": name.lower(), "stage_kind": stage_kind, "sql": sql or ""})

    @staticmethod
    def _is_cell_struct_type(data_type: Any) -> bool:
        field_names = None
        if hasattr(data_type, "fieldNames"):
            try:
                field_names = set(data_type.fieldNames())
            except Exception:
                field_names = None
        if field_names is None:
            expr = getattr(data_type, "expr", None)
            if isinstance(expr, exp.DataType):
                field_names = {column.name for column in expr.expressions if getattr(column, "name", None)}
        return bool(
            field_names
            and {"value", "metadata", "__agentcicd_cell"}.issubset(field_names)
        )

    @staticmethod
    def _is_current_cell_struct_type(data_type: Any) -> bool:
        field_names = None
        if hasattr(data_type, "fieldNames"):
            try:
                field_names = set(data_type.fieldNames())
            except Exception:
                field_names = None
        return bool(field_names and {"cell_id", "value", "metadata", "__agentcicd_cell"}.issubset(field_names))

    @staticmethod
    def _cell_metadata_has_latency(data_type: Any) -> bool:
        try:
            metadata_field = data_type["metadata"]
            metadata_type = metadata_field.dataType
            return "latency_ms" in set(metadata_type.fieldNames())
        except Exception:
            return False

    @staticmethod
    def _cell_metadata_has_fixture_trace(data_type: Any) -> bool:
        try:
            metadata_field = data_type["metadata"]
            metadata_type = metadata_field.dataType
            return "fixture_trace" in set(metadata_type.fieldNames())
        except Exception:
            return False
