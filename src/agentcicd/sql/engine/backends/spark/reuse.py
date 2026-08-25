from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from agentcicd.sql.engine.backends.spark.layout import join_path
from agentcicd.sql.engine.reusable_stages import ReusableStageRegistry
from agentcicd.sql.engine.stage_manifest import manifest_matches_expected


class SparkReuseMixin:
    def register_reusable_materialized_stages(self) -> None:
        if self._previous_registration_attempted:
            return
        self._previous_registration_attempted = True
        if not self._reusable_stages.requested_tables:
            self._reusable_stages = ReusableStageRegistry.from_env()
        previous_run_object_uri = os.getenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "").strip()
        current_run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
        completed_tables = sorted(self._reusable_stages.requested_tables)
        if not previous_run_object_uri or not completed_tables:
            return
        previous_tables_root = join_path(self._object_uri_to_s3a(previous_run_object_uri), "tables")
        current_tables_root = (
            join_path(self._object_uri_to_s3a(current_run_object_uri), "tables")
            if current_run_object_uri
            else previous_tables_root
        )
        for table_name in completed_tables:
            expected = self._expected_stage_manifests.get(table_name.lower())
            if expected is not None:
                previous_manifest = self._read_previous_stage_manifest(table_name, previous_run_object_uri)
                if previous_manifest is None or not manifest_matches_expected(previous_manifest, expected):
                    continue
            table_path = join_path(current_tables_root, table_name)
            schema = self._read_previous_schema_sidecar(table_name, previous_run_object_uri)
            dataframe = self._read_table_path(table_path, schema=schema)
            dataframe.createOrReplaceTempView(table_name)
            self._record_known_table(table_name, table_path, schema=schema)
            self._reusable_stages.mark_registered(table_name)
            self._mirror_reused_local_artifacts(
                table_name,
                previous_run_object_uri=previous_run_object_uri,
                table_path=table_path,
            )

    def should_skip_materialized_stage(self, step: Any) -> bool:
        if getattr(step, "kind", None) not in {"create_batch_table", "create_stream_table"}:
            return False
        skipped = self._reusable_stages.should_skip_materialized_table(str(getattr(step, "name", "")))
        if skipped:
            self._completion_metadata[(str(getattr(step, "kind", "")), str(getattr(step, "name", "")))] = {
                "reuse_state": "reused",
            }
        return skipped

    def should_skip_step(self, step: Any) -> bool:
        return self.should_skip_materialized_stage(step)

    def step_completion_metadata(self, step: Any) -> dict[str, Any]:
        key = (str(getattr(step, "kind", "")), str(getattr(step, "name", "")))
        return dict(self._completion_metadata.get(key) or {})

    def _mirror_reused_local_artifacts(self, table_name: str, *, previous_run_object_uri: str, table_path: str) -> None:
        if self._is_uri_path(previous_run_object_uri) or self._is_uri_path(table_path):
            return
        source_table = Path(table_path)
        target_table = Path(self._paths.tables_root) / table_name
        if source_table.exists() and not target_table.exists():
            target_table.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_table, target_table)
            spark_metadata = target_table / "_spark_metadata"
            if spark_metadata.exists():
                shutil.rmtree(spark_metadata)
            self._record_known_table(table_name, str(target_table))

        previous_outputs = Path(previous_run_object_uri) / "outputs"
        current_outputs = Path(self._paths.outputs_root)
        current_outputs.mkdir(parents=True, exist_ok=True)
        for filename in [f"stage_{table_name}.json", f"stage_error_summary_{table_name}.json"]:
            source = previous_outputs / filename
            target = current_outputs / filename
            if source.exists() and not target.exists():
                shutil.copyfile(source, target)

        previous_schema = previous_outputs / "schemas" / f"{table_name}.json"
        current_schema = current_outputs / "schemas" / f"{table_name}.json"
        if previous_schema.exists() and not current_schema.exists():
            current_schema.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(previous_schema, current_schema)

    @staticmethod
    def _object_uri_to_s3a(uri: str) -> str:
        if not uri.startswith("agentcicd-object://"):
            return uri
        bucket, separator, object_name = uri.removeprefix("agentcicd-object://").partition("/")
        if not bucket or not separator or not object_name:
            return uri
        return f"s3a://{bucket.strip().lower().replace('.', '-')}/{object_name}"
