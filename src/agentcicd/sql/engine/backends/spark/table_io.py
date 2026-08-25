from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from agentcicd.sql.engine.backends.spark.common import F, Window
from agentcicd.sql.engine.backends.spark.layout import SparkBackendPaths, is_uri_path, join_path
from agentcicd.sql.ir.options import StatementOptions


class SparkTableIOMixin:
    def _register_all_known_views(self) -> None:
        known_table_paths, known_table_schemas = self._snapshot_known_tables()
        for name, path in known_table_paths:
            dataframe = self._read_table_path(path, schema=known_table_schemas.get(name))
            dataframe.createOrReplaceTempView(name)
        for root in [self._paths.sources_root, self._paths.tables_root]:
            if self._is_uri_path(root):
                continue
            base = Path(root)
            if not base.exists():
                continue
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                if not self._is_materialized_table_path(child):
                    continue
                try:
                    dataframe = self._spark.read.format(self._table_format).load(str(child))
                except Exception:
                    continue
                dataframe.createOrReplaceTempView(child.name)

    def _register_stream_source_views(self, source_tables: list[str], *, batch_size: int | None) -> None:
        for table in source_tables:
            source_path = self._resolve_known_table_path(table)
            stream_path = source_path
            reader = self._spark.readStream.format(self._table_format)
            if batch_size is not None:
                stream_path = self._prepare_stream_batch_source(table, source_path, batch_size)
                reader = reader.option("maxFilesPerTrigger", 1)
            stream_schema = self._known_table_schema(table) or self._read_schema_sidecar(table)
            if stream_schema is None:
                stream_schema = self._read_table_path(stream_path).schema
            dataframe = reader.schema(stream_schema).load(stream_path)
            dataframe.createOrReplaceTempView(table)

    def _resolve_known_table_path(self, name: str) -> str:
        known_path, _schema = self._known_table_entry(name)
        if known_path:
            return known_path
        table_path = self._table_path(name)
        source_path = self._source_path(name)
        if self._is_uri_path(table_path):
            return table_path
        if self._is_uri_path(source_path):
            return source_path
        if self._is_materialized_table_path(Path(table_path)):
            return table_path
        if self._is_materialized_table_path(Path(source_path)):
            return source_path
        raise FileNotFoundError(f"Source table '{name}' not found at '{table_path}' or '{source_path}'")

    def _prepare_stream_batch_source(self, table: str, source_path: str, batch_size: int) -> str:
        if batch_size <= 0:
            raise ValueError("BATCH_SIZE must be a positive integer")
        dataframe = self._spark.read.format(self._table_format).load(source_path)
        total_rows = dataframe.count()
        staged_path = self._stream_batch_path(table, batch_size)
        if total_rows == 0:
            (
                dataframe.limit(0)
                .write.format(self._table_format)
                .mode("overwrite")
                .option("overwriteSchema", "true")
                .save(staged_path)
            )
            return staged_path

        batch_files = max(1, math.ceil(total_rows / batch_size))
        window = Window.orderBy(F.monotonically_increasing_id())
        batched_dataframe = dataframe.withColumn(
            "__agentcicd_stream_batch",
            F.floor((F.row_number().over(window) - F.lit(1)) / F.lit(batch_size)).cast("long"),
        )
        batched_dataframe = batched_dataframe.repartition(batch_files, "__agentcicd_stream_batch").drop("__agentcicd_stream_batch")
        (
            batched_dataframe.write.format(self._table_format)
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .save(staged_path)
        )
        return staged_path

    @staticmethod
    def _normalize_output_format(output_format: str) -> str:
        normalized = output_format.lower()
        if normalized in {"jsonl", "ndjson"}:
            return "json"
        return normalized

    def _read_known_table(self, name: str):
        known_path, known_schema = self._known_table_entry(name)
        if known_path:
            return self._read_table_path(known_path, schema=known_schema)
        table_path = self._table_path(name)
        source_path = self._source_path(name)
        if self._is_uri_path(table_path):
            return self._read_table_path(table_path)
        if self._is_uri_path(source_path):
            return self._read_table_path(source_path)
        if self._is_materialized_table_path(Path(table_path)):
            return self._read_table_path(table_path)
        if self._is_materialized_table_path(Path(source_path)):
            return self._read_table_path(source_path)
        return self._spark.table(name)

    def _refresh_table_view(self, name: str, *, schema: Any | None = None) -> None:
        if schema is not None:
            self._record_known_table_schema(name, schema)
        dataframe = self._read_table_path(self._table_path(name), schema=schema)
        dataframe.createOrReplaceTempView(name)

    def _read_table_path(self, path: str, *, schema: Any | None = None):
        reader = self._spark.read.format(self._table_format)
        if schema is not None:
            reader = reader.schema(schema)
        try:
            return reader.load(path)
        except Exception as exc:
            if not self._should_retry_recursive_parquet_read(exc):
                raise
            retry_reader = (
                self._spark.read.format(self._table_format)
                .option("recursiveFileLookup", "true")
                .option("pathGlobFilter", "*.parquet")
            )
            if schema is not None:
                retry_reader = retry_reader.schema(schema)
            return retry_reader.load(path)

    def _should_retry_recursive_parquet_read(self, exc: Exception) -> bool:
        if self._table_format != "parquet":
            return False
        message = str(exc)
        return "Unable to infer schema for Parquet" in message

    def _write_table(self, dataframe, path: str) -> None:
        dataframe.write.mode("overwrite").format(self._table_format).save(path)

    def _materialize_published_dataset(self, name: str) -> None:
        dataset_path = os.path.join(self._paths.working_dir, "published_datasets", name)
        dataframe = self._unwrap_cell_dataframe(self._read_known_table(name))
        self._write_table(dataframe, dataset_path)

    def _publication_layout(self, table_name: str) -> SparkBackendPaths:
        if not self._is_uri_path(self._paths.tables_root):
            return self._paths
        local_tables_root = os.path.join(self._paths.working_dir, "published_tables")
        local_table_path = os.path.join(local_tables_root, table_name)
        self._write_table(self._unwrap_cell_dataframe(self._read_known_table(table_name)), local_table_path)
        return SparkBackendPaths(
            working_dir=self._paths.working_dir,
            tables_root=local_tables_root,
            sources_root=self._paths.sources_root,
            outputs_root=self._paths.outputs_root,
            publish_root=self._paths.publish_root,
            checkpoints_root=self._paths.checkpoints_root,
            stream_batches_root=self._paths.stream_batches_root,
            http_cache_root=self._paths.http_cache_root,
            annotation_tasks_root=self._paths.annotation_tasks_root,
        )

    def _table_path(self, name: str) -> str:
        return join_path(self._paths.tables_root, name)

    def _source_path(self, name: str) -> str:
        return os.path.join(self._paths.sources_root, name)

    def _checkpoint_path(self, name: str) -> str:
        return join_path(self._paths.checkpoints_root, name)

    def _stream_batch_path(self, name: str, batch_size: int) -> str:
        return os.path.join(self._paths.stream_batches_root, name, str(batch_size))

    @staticmethod
    def _is_uri_path(path: str) -> bool:
        return is_uri_path(path)

    def _write_output_manifest(self, kind: str, name: str, payload: dict[str, Any]) -> None:
        manifest_path = Path(self._paths.outputs_root) / f"{kind}_{name}.json"
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _normalize_options(options: StatementOptions | Mapping[str, object]) -> StatementOptions:
        if isinstance(options, StatementOptions):
            return options
        return StatementOptions.from_mapping(options)

    @staticmethod
    def _is_materialized_table_path(path: Path) -> bool:
        if not path.is_dir():
            return False
        try:
            for item in path.rglob("*"):
                if item.name.startswith("_delta_log") or item.suffix == ".parquet":
                    return True
        except OSError:
            return False
        return False
