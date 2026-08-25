from __future__ import annotations

import threading
from typing import Any, Mapping, Optional

from agentcicd.sql.engine.annotation_store import LocalAnnotationStore
from agentcicd.sql.engine.backends.spark.cells import SparkCellMixin
from agentcicd.sql.engine.backends.spark.debug_streams import _normalize_debug_options, SparkDebugStreamsMixin
from agentcicd.sql.engine.backends.spark.layout import SparkBackendPaths, default_backend_paths, join_path
from agentcicd.sql.engine.backends.spark.reuse import SparkReuseMixin
from agentcicd.sql.engine.backends.spark.session import build_spark_session, s3a_endpoint
from agentcicd.sql.engine.backends.spark.stage_artifacts import (
    _schema_json_value,
    _value_schema_json_value,
    SparkStageArtifactsMixin,
)
from agentcicd.sql.engine.backends.spark.table_io import SparkTableIOMixin
from agentcicd.sql.engine.backends.spark.table_registry import SparkTableRegistry, SparkTableRegistryMixin
from agentcicd.sql.engine.interfaces import (
    AnnotationStore,
    PublicationStore,
    RuntimeFunctionInvoker,
    SourceLoader,
    ensure_layout_roots,
)
from agentcicd.sql.engine.publication_store import LocalManifestPublicationStore
from agentcicd.sql.engine.runtime import ExecutionBackend
from agentcicd.sql.engine.objectstore_functions import (
    register_objectstore_functions,
    rewrite_objectstore_function_calls,
    sql_uses_objectstore_functions,
)
from agentcicd.sql.runtime.invokers import CompositeRuntimeFunctionInvoker
from agentcicd.sql.engine.reusable_stages import ReusableStageRegistry
from agentcicd.sql.engine.source_loader import SparkSourceLoader
from agentcicd.sql.engine.stage_manifest import StageManifest, build_expected_stage_manifests, description_from_options
from agentcicd.sql.ir.column_semantics import column_semantics_from_options
from agentcicd.sql.ir.options import StatementOptions

_s3a_endpoint = s3a_endpoint
_join_path = join_path


class SparkExecutionBackend(
    SparkReuseMixin,
    SparkTableRegistryMixin,
    SparkTableIOMixin,
    SparkDebugStreamsMixin,
    SparkStageArtifactsMixin,
    SparkCellMixin,
    ExecutionBackend,
):
    def __init__(
        self,
        spark_session,
        *,
        working_dir: str,
        table_format: str = "parquet",
        paths: Optional[SparkBackendPaths] = None,
        source_loader: SourceLoader | None = None,
        publication_store: PublicationStore | None = None,
        annotation_store: AnnotationStore | None = None,
        runtime_function_invoker: RuntimeFunctionInvoker | None = None,
        debug: bool | Mapping[str, Any] | None = None,
    ) -> None:
        self._spark = spark_session
        self._table_format = table_format.strip().lower()
        if self._table_format not in {"parquet", "delta"}:
            raise ValueError("table_format must be either 'parquet' or 'delta'")
        self._paths = paths or default_backend_paths(working_dir)
        self._source_loader = source_loader or SparkSourceLoader(self._paths)
        self._publication_store = publication_store or LocalManifestPublicationStore()
        self._annotation_store = annotation_store or LocalAnnotationStore()
        self._runtime_function_invoker = runtime_function_invoker or CompositeRuntimeFunctionInvoker()
        self._debug_options = _normalize_debug_options(debug)
        self._registered_sql_functions: dict[str, Any] = {}
        self._registered_runtime_functions: set[str] = set()
        self._table_registry = SparkTableRegistry()
        self._reusable_stages = ReusableStageRegistry.from_env()
        self._expected_stage_manifests: dict[str, StageManifest] = {}
        self._completion_metadata: dict[tuple[str, str], dict[str, Any]] = {}
        self._previous_registration_attempted = False
        self._spark_view_lock = threading.RLock()
        ensure_layout_roots(self._paths)

    def set_execution_plan_context(self, plan: list[Any]) -> None:
        self._expected_stage_manifests = build_expected_stage_manifests(plan)
        self.register_reusable_materialized_stages()

    def declare_variable(self, name: str, sql: str) -> None:
        self._spark.sql(sql)

    def register_sql_function(self, name: str, definition: Any) -> None:
        self._registered_sql_functions[name.lower()] = definition

    def register_runtime_function(self, name: str, definition: Any) -> None:
        runtime_alias = str(getattr(definition, "runtime_alias", "") or name.replace(".", "_")).strip()
        alias_key = runtime_alias.lower()
        if alias_key in self._registered_runtime_functions:
            return
        self._runtime_function_invoker.register(self._spark, definition)
        self._registered_runtime_functions.add(alias_key)

    def create_batch_table(
        self,
        name: str,
        sql: str,
        *,
        options: StatementOptions | Mapping[str, object] | None = None,
    ) -> None:
        options = self._normalize_options(options or {})
        with self._spark_view_lock:
            self._register_all_known_views()
            execution_sql = self._prepare_objectstore_function_sql(sql)
            dataframe = self._spark.sql(execution_sql)
        if getattr(dataframe, "isStreaming", False):
            raise RuntimeError("Batch table execution produced a streaming DataFrame")
        dataframe = self._normalize_materialized_dataframe(dataframe, stage_name=name, stage_kind="batch", sql=sql)
        table_path = self._table_path(name)
        self._write_table(dataframe, table_path)
        self._record_known_table(name, table_path, schema=dataframe.schema)
        column_semantics = column_semantics_from_options(options)
        description = description_from_options(options)
        self._write_schema_sidecar(
            name,
            dataframe.schema,
            table_path=table_path,
            kind="batch",
            description=description,
            column_semantics=column_semantics,
        )
        self._write_stage_manifest(
            name,
            "batch",
            sql=sql,
            dataframe=dataframe,
            table_path=table_path,
            description=description,
            column_semantics=column_semantics,
        )
        self._refresh_table_view(name, schema=dataframe.schema)

    def create_stream_table(
        self,
        name: str,
        sql: str,
        *,
        source_tables: list[str] | None = None,
        batch_size: int | None = None,
        options: StatementOptions | Mapping[str, object] | None = None,
    ) -> None:
        options = self._normalize_options(options or {})
        source_tables = list(source_tables or [])
        with self._spark_view_lock:
            self._register_all_known_views()
            self._register_stream_source_views(source_tables, batch_size=batch_size)
            execution_sql = self._prepare_objectstore_function_sql(sql)
            dataframe = self._spark.sql(execution_sql)
        if not getattr(dataframe, "isStreaming", False):
            raise RuntimeError("Stream table execution requires a streaming DataFrame")
        table_path = self._table_path(name)
        checkpoint_path = self._checkpoint_path(name)
        debug_observer = self._start_stream_debug_row_observer(name, table_path, dataframe.schema)
        query = (
            dataframe.writeStream.format(self._table_format)
            .option("path", table_path)
            .option("checkpointLocation", checkpoint_path)
            .outputMode("append")
            .trigger(availableNow=True)
            .start()
        )
        debug_observer.start()
        try:
            query.awaitTermination()
        finally:
            debug_observer.stop_and_flush()
        self._record_known_table(name, table_path, schema=dataframe.schema)
        column_semantics = column_semantics_from_options(options)
        description = description_from_options(options)
        self._write_schema_sidecar(
            name,
            dataframe.schema,
            table_path=table_path,
            kind="stream",
            description=description,
            column_semantics=column_semantics,
        )
        self._write_stage_manifest(
            name,
            "stream",
            sql=sql,
            dataframe=dataframe,
            table_path=table_path,
            checkpoint_path=checkpoint_path,
            description=description,
            column_semantics=column_semantics,
        )
        self._refresh_table_view(name, schema=dataframe.schema)

    def load_table(
        self,
        name: str,
        path: str,
        options: StatementOptions | Mapping[str, object],
        *,
        wrap_cells: bool = False,
        limit: int | None = None,
    ) -> None:
        options = self._normalize_options(options)
        dataframe = self._source_loader.load_dataframe(self._spark, path, options)
        if limit is not None:
            if limit <= 0:
                raise ValueError("LOAD LIMIT must be a positive integer")
            dataframe = dataframe.limit(limit)
        wrap_cells = self._should_wrap_load_cells(options, default=wrap_cells)
        if wrap_cells:
            dataframe = self._wrap_loaded_dataframe(dataframe, source_name=name)
        target_path = self._source_path(name)
        self._write_table(dataframe, target_path)
        self._record_known_table(name, target_path, schema=dataframe.schema)
        dataframe.createOrReplaceTempView(name)
        self._write_schema_sidecar(name, dataframe.schema, table_path=target_path, kind="load_table")
        self._write_stage_manifest(name, "load_table", sql=None, dataframe=dataframe, table_path=target_path)
        self._write_output_manifest(
            kind="load_table",
            name=name,
            payload={"path": path, "options": options.to_dict(), "wrap_cells": wrap_cells, "limit": limit},
        )

    def save_table(self, name: str, path: str, options: StatementOptions | Mapping[str, object]) -> None:
        options = self._normalize_options(options)
        dataframe = self._read_known_table(name)
        output_format = str(options.get("format") or self._table_format).lower()
        writer_format = self._normalize_output_format(output_format)
        dataframe.write.mode("overwrite").format(writer_format).save(path)
        self._write_output_manifest(
            kind="save_table",
            name=name,
            payload={"path": path, "format": output_format, "writer_format": writer_format},
        )

    def publish_report(
        self,
        name: str,
        component: str,
        chart_type: str | None = None,
        report_options: dict[str, str] | None = None,
    ) -> None:
        self._publication_store.publish_report(self._publication_layout(name), name, component, chart_type, report_options)

    def publish_dataset(self, name: str, dataset_name: str | None) -> None:
        self._materialize_published_dataset(name)
        self._publication_store.publish_dataset(self._paths, name, dataset_name)

    def _prepare_objectstore_function_sql(self, sql: str) -> str:
        if not sql_uses_objectstore_functions(sql):
            return sql
        register_objectstore_functions(self._spark)
        return rewrite_objectstore_function_calls(sql)

    def publish_annotation(
        self,
        name: str,
        queue_name: str,
        *,
        alias: str | None = None,
        options: StatementOptions | Mapping[str, object] | None = None,
    ) -> None:
        normalized_options = self._normalize_options(options or {})
        self._publication_store.publish_annotation(
            self._publication_layout(name),
            name,
            queue_name,
            alias=alias,
            options=normalized_options,
        )

    def retrieve_annotation(self, name: str, source_ref: str, *, wrap_cells: bool = False) -> None:
        dataframe = self._annotation_store.load_annotation_dataframe(self._spark, self._paths, source_ref)
        if wrap_cells:
            dataframe = self._wrap_loaded_dataframe(dataframe, source_name=name)
        target_path = self._table_path(name)
        self._write_table(dataframe, target_path)
        self._record_known_table(name, target_path, schema=dataframe.schema)
        self._write_schema_sidecar(name, dataframe.schema, table_path=target_path, kind="retrieve_annotation")
        self._write_stage_manifest(name, "retrieve_annotation", sql=None, dataframe=dataframe, table_path=target_path)
        self._refresh_table_view(name, schema=dataframe.schema)
