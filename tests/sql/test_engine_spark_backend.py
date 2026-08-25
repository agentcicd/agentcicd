import json
import threading
import time

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.reusable_stages import ReusableStageRegistry
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend, _s3a_endpoint, default_backend_paths
from agentcicd.sql.engine.stage_manifest import build_expected_stage_manifests, completed_manifest_from_expected
from agentcicd.sql.engine.stage_manifest import StageManifest


class _FakeWriter:
    def __init__(self, frame, sink):
        self._frame = frame
        self._sink = sink
        self._mode = None
        self._format = None

    def mode(self, value):
        self._mode = value
        return self

    def format(self, value):
        self._format = value
        return self

    def option(self, key, value):
        self._sink.append(("write_option", self._format, key, value))
        return self

    def save(self, path):
        self._sink.append(("save", self._frame.label, self._mode, self._format, path))


class _FakeFrame:
    def __init__(self, label, sink):
        self.label = label
        self.write = _FakeWriter(self, sink)
        self.schema = type("Schema", (), {"fields": []})()
        self._sink = sink

    def createOrReplaceTempView(self, name):
        self._sink.append(("temp_view", self.label, name))

    def select(self, *columns):
        self._sink.append(("select", self.label, len(columns)))
        return self

    def count(self):
        self._sink.append(("count", self.label))
        return 0

    def limit(self, count):
        self._sink.append(("limit", self.label, count))
        return self

    def coalesce(self, count):
        self._sink.append(("coalesce", self.label, count))
        return self

    @property
    def isStreaming(self):
        return False


class _FakeRead:
    def __init__(self, sink):
        self._sink = sink
        self._format = None
        self._options = {}

    def format(self, value):
        self._format = value
        return self

    def option(self, key, value):
        self._options[key] = value
        self._sink.append(("option", self._format, key, value))
        return self

    def schema(self, value):
        self._sink.append(("schema", self._format, value))
        return self

    def load(self, path):
        self._sink.append(("load", self._format, path))
        fail_paths = self._sink.fail_load_paths if hasattr(self._sink, "fail_load_paths") else set()
        if path in fail_paths and self._options.get("recursiveFileLookup") != "true":
            raise Exception("Unable to infer schema for Parquet at . It must be specified manually.")
        return _FakeFrame(f"frame:{path}", self._sink)

    def json(self, path):
        self._sink.append(("json", path))
        return _FakeFrame(f"json:{path}", self._sink)


class _FakeSpark:
    def __init__(self):
        class _Calls(list):
            fail_load_paths = set()

        self.calls = _Calls()
        self.read = _FakeRead(self.calls)
        self.udf = self

    def register(self, name, fn, return_type=None):
        self.calls.append(("udf_register", name, return_type.__class__.__name__ if return_type is not None else None))

    def sql(self, sql):
        self.calls.append(("sql", sql))
        return _FakeFrame(f"sql:{sql}", self.calls)

    def table(self, name):
        self.calls.append(("table", name))
        return _FakeFrame(f"table:{name}", self.calls)


class _RecordingPublicationStore:
    def __init__(self):
        self.calls = []

    def publish_report(self, layout, name, component, chart_type=None, report_options=None):
        self.calls.append(("report", layout, name, component, chart_type, report_options))

    def publish_dataset(self, layout, name, dataset_name):
        self.calls.append(("dataset", layout, name, dataset_name))

    def publish_annotation(self, layout, name, queue_name, *, alias=None, options=None):
        self.calls.append(("annotation", layout, name, queue_name, alias, options))


def test_spark_execution_backend_uses_spark_for_batch_table_and_save(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))

    backend.create_batch_table("out", "SELECT 1 AS x")
    backend.save_table("out", "/tmp/output-path", {"format": "delta"})

    assert ("sql", "SELECT 1 AS x") in spark.calls
    assert any(call[0] == "save" and call[4].endswith("/tables/out") for call in spark.calls)
    assert any(call[0] == "save" and call[4] == "/tmp/output-path" and call[3] == "delta" for call in spark.calls)
    manifest = json.loads((tmp_path / "outputs" / "save_table_out.json").read_text(encoding="utf-8"))
    assert manifest["path"] == "/tmp/output-path"


class _RaceWriter:
    def format(self, value):
        return self

    def option(self, key, value):
        return self

    def outputMode(self, value):
        return self

    def trigger(self, **kwargs):
        return self

    def start(self):
        return self

    def awaitTermination(self):
        return None


class _RaceFrame(_FakeFrame):
    def __init__(self, label, sink, *, is_streaming=False):
        super().__init__(label, sink)
        self._is_streaming = is_streaming
        self.writeStream = _RaceWriter()

    @property
    def isStreaming(self):
        return self._is_streaming

    def createOrReplaceTempView(self, name):
        self._sink.temp_views[name] = self._is_streaming
        self._sink.append(("temp_view", self.label, name, self._is_streaming))


class _RaceRead(_FakeRead):
    def load(self, path):
        self._sink.append(("load", self._format, path))
        return _RaceFrame(f"batch:{path}", self._sink, is_streaming=False)


class _RaceReadStream(_FakeRead):
    def load(self, path):
        self._sink.append(("stream_load", self._format, path))
        return _RaceFrame(f"stream:{path}", self._sink, is_streaming=True)


class _RaceCalls(list):
    def __init__(self):
        super().__init__()
        self.temp_views = {}


class _RaceSpark:
    def __init__(self):
        self.calls = _RaceCalls()
        self.read = _RaceRead(self.calls)
        self.readStream = _RaceReadStream(self.calls)
        self.batch_sql_entered = threading.Event()

    def sql(self, sql):
        self.calls.append(("sql", sql))
        if sql == "SELECT * FROM source":
            self.batch_sql_entered.set()
            time.sleep(0.2)
            return _RaceFrame("batch-query", self.calls, is_streaming=self.calls.temp_views.get("source", False))
        return _RaceFrame("stream-query", self.calls, is_streaming=True)


def test_batch_sql_analysis_does_not_race_with_stream_source_view_registration(tmp_path, monkeypatch):
    spark = _RaceSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend._record_known_table("source", str(tmp_path / "tables" / "source"))

    monkeypatch.setattr(backend, "_write_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_write_schema_sidecar", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_write_stage_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_refresh_table_view", lambda *args, **kwargs: None)

    errors = []

    def run_batch():
        try:
            backend.create_batch_table("scored", "SELECT * FROM source")
        except Exception as exc:  # pragma: no cover - assertion reports below
            errors.append(exc)

    batch_thread = threading.Thread(target=run_batch)
    batch_thread.start()
    assert spark.batch_sql_entered.wait(timeout=2)

    backend.create_stream_table(
        "live",
        "SELECT * FROM source",
        source_tables=["source"],
    )
    batch_thread.join(timeout=2)

    assert errors == []
    batch_sql_index = spark.calls.index(("sql", "SELECT * FROM source"))
    stream_source_index = next(
        index
        for index, call in enumerate(spark.calls)
        if call[0] == "temp_view" and call[1].startswith("stream:") and call[2:] == ("source", True)
    )
    assert batch_sql_index < stream_source_index


def test_stream_table_batch_size_sets_max_files_per_trigger(tmp_path, monkeypatch):
    spark = _RaceSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend._record_known_table("source", str(tmp_path / "tables" / "source"))

    monkeypatch.setattr(backend, "_write_schema_sidecar", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_write_stage_manifest", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_refresh_table_view", lambda *args, **kwargs: None)

    backend.create_stream_table(
        "live",
        "SELECT * FROM source",
        source_tables=["source"],
        batch_size=2,
    )

    assert ("option", "parquet", "maxFilesPerTrigger", 1) in spark.calls


def test_spark_execution_backend_writes_column_semantics_to_stage_and_schema_manifests(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))

    backend.create_batch_table(
        "generated",
        "SELECT 1 AS assistant_answer",
        options={
            "description": "Generates assistant answers and raw judge payloads.",
            "column_semantics": {
                "assistant_answer": {"type": "markdown"},
                "judge_payload": {"type": "code", "language": "JSON"},
            }
        },
    )

    stage_manifest = json.loads((tmp_path / "outputs" / "stage_generated.json").read_text(encoding="utf-8"))
    schema_sidecar = json.loads((tmp_path / "outputs" / "schemas" / "generated.json").read_text(encoding="utf-8"))
    expected = {
        "columns": {
            "assistant_answer": {"type": "markdown"},
            "judge_payload": {"type": "code", "language": "json"},
        },
    }
    assert stage_manifest["description"] == "Generates assistant answers and raw judge payloads."
    assert schema_sidecar["description"] == "Generates assistant answers and raw judge payloads."
    assert stage_manifest["column_semantics"] == expected
    assert schema_sidecar["column_semantics"] == expected


def test_column_semantics_json_type_normalizes_to_json_code_renderer(tmp_path):
    from agentcicd.sql.ir.column_semantics import column_semantics_from_options

    semantics = column_semantics_from_options({
        "column_semantics": {
            "payload": {"type": "json"},
        },
    })

    assert semantics["columns"]["payload"] == {"type": "code", "language": "json"}


def test_column_semantics_directory_and_image_types_are_additive(tmp_path):
    from agentcicd.sql.ir.column_semantics import column_semantics_from_options

    semantics = column_semantics_from_options({
        "column_semantics": {
            "artifacts": {"type": "directory"},
            "screenshot": {"type": "image", "display": "ref"},
        },
    })

    assert semantics["columns"]["artifacts"] == {"type": "directory", "display": "auto"}
    assert semantics["columns"]["screenshot"] == {"type": "image", "display": "ref"}


def test_stage_manifest_round_trips_description_and_column_semantics():
    manifest = StageManifest(
        stage_name="generated",
        stage_kind="batch",
        fingerprint="abc",
        description="Generates model outputs for scoring.",
        column_semantics={
            "columns": {"generated_code": {"type": "code", "language": "python"}},
        },
    )

    restored = StageManifest.from_dict(manifest.to_dict())

    assert restored.description == manifest.description
    assert restored.column_semantics == manifest.column_semantics


def test_s3a_endpoint_adds_scheme_for_hadoop_s3a():
    assert _s3a_endpoint("minio.agentcicd-dp.svc.cluster.local:9000", secure=False) == (
        "http://minio.agentcicd-dp.svc.cluster.local:9000"
    )
    assert _s3a_endpoint("minio.agentcicd-dp.svc.cluster.local:9000", secure=True) == (
        "https://minio.agentcicd-dp.svc.cluster.local:9000"
    )
    assert _s3a_endpoint("http://minio.agentcicd-dp.svc.cluster.local:9000", secure=False) == (
        "http://minio.agentcicd-dp.svc.cluster.local:9000"
    )


def test_reusable_stage_registry_tracks_registered_tables(monkeypatch):
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", " evaluated,metrics,evaluated ")

    registry = ReusableStageRegistry.from_env()

    assert registry.requested_tables == {"evaluated", "metrics"}
    assert not registry.should_skip_materialized_table("evaluated")
    registry.mark_registered("evaluated")
    assert registry.should_skip_materialized_table("Evaluated")


def test_spark_execution_backend_does_not_register_previous_table_without_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "evaluated, metrics, evaluated")
    spark = _FakeSpark()

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))

    assert dict(backend._snapshot_known_tables()[0]) == {}
    assert not any(call[0] == "load" for call in spark.calls)


def test_spark_execution_backend_registers_matching_completed_table_from_previous_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "evaluated")
    spark = _FakeSpark()
    plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE evaluated
        SELECT 1 AS x;
        """
    ).compile_plan(include_cells=True)
    expected = build_expected_stage_manifests(plan)["evaluated"]
    completed = completed_manifest_from_expected(
        expected,
        table_format="parquet",
        output_path="s3a://org-093ea7db6a5a/runs/run.test/attempt_1/tables/evaluated",
        output_schema_json={},
        value_schema_json={},
    )
    monkeypatch.setenv("AGENTCICD_PREVIOUS_STAGE_MANIFESTS_JSON", json.dumps({"evaluated": completed.to_dict()}))

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend.set_execution_plan_context(plan)

    assert dict(backend._snapshot_known_tables()[0]) == {
        "evaluated": "s3a://org-093ea7db6a5a/runs/run.test/attempt_1/tables/evaluated",
    }
    assert (
        "load",
        "parquet",
        "s3a://org-093ea7db6a5a/runs/run.test/attempt_1/tables/evaluated",
    ) in spark.calls
    assert ("temp_view", "frame:s3a://org-093ea7db6a5a/runs/run.test/attempt_1/tables/evaluated", "evaluated") in spark.calls
    assert backend.should_skip_materialized_stage(plan[0])


def test_spark_execution_backend_rejects_mismatched_previous_stage_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "evaluated")
    spark = _FakeSpark()
    current_plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE evaluated
        SELECT 1 AS x;
        """
    ).compile_plan(include_cells=True)
    stale_plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE evaluated
        SELECT 2 AS x;
        """
    ).compile_plan(include_cells=True)
    stale_expected = build_expected_stage_manifests(stale_plan)["evaluated"]
    stale_completed = completed_manifest_from_expected(
        stale_expected,
        table_format="parquet",
        output_path="s3a://org-093ea7db6a5a/runs/run.test/attempt_1/tables/evaluated",
        output_schema_json={},
        value_schema_json={},
    )
    monkeypatch.setenv("AGENTCICD_PREVIOUS_STAGE_MANIFESTS_JSON", json.dumps({"evaluated": stale_completed.to_dict()}))

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend.set_execution_plan_context(current_plan)

    assert dict(backend._snapshot_known_tables()[0]) == {}
    assert not any(call[0] == "load" for call in spark.calls)
    assert not backend.should_skip_materialized_stage(current_plan[0])


def test_spark_execution_backend_registers_copied_external_completed_table(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_2")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "fixture_results")
    schema_payload = {
        "wrapped_schema": {
            "type": "struct",
            "fields": [
                {
                    "name": "id",
                    "type": "integer",
                    "nullable": True,
                    "metadata": {},
                }
            ],
        }
    }

    class _Store:
        def get_json(self, uri):
            assert uri == (
                "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_2/"
                "outputs/schemas/fixture_results.json"
            )
            return schema_payload

    monkeypatch.setattr(
        "agentcicd.sql.engine.backends.spark.stage_artifacts.object_store_from_env",
        lambda: _Store(),
    )
    spark = _FakeSpark()
    plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE successful_rows
        SELECT id FROM fixture_results;
        """,
        external_tables={"fixture_results"},
    ).compile_plan(include_cells=True)

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend.set_execution_plan_context(plan)

    copied_path = "s3a://org-093ea7db6a5a/runs/run.test/attempt_2/tables/fixture_results"
    assert dict(backend._snapshot_known_tables()[0]) == {"fixture_results": copied_path}
    assert any(call[0] == "schema" and call[1] == "parquet" for call in spark.calls)
    assert ("load", "parquet", copied_path) in spark.calls
    assert ("temp_view", f"frame:{copied_path}", "fixture_results") in spark.calls
    assert backend.should_skip_materialized_stage(
        EngineEntrypoint(
            """
            CREATE BATCH TABLE fixture_results
            SELECT 1 AS id;
            """
        ).compile_plan(include_cells=True)[0]
    )


def test_spark_execution_backend_registers_full_plan_completed_table_from_object_store_manifest(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_2")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "source_data")
    monkeypatch.delenv("AGENTCICD_PREVIOUS_STAGE_MANIFESTS_JSON", raising=False)
    plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE source_data
        SELECT 1 AS id;

        CREATE BATCH TABLE fixture_results
        SELECT id FROM source_data;
        """
    ).compile_plan(include_cells=True)
    expected = build_expected_stage_manifests(plan)["source_data"]
    completed = completed_manifest_from_expected(
        expected,
        table_format="parquet",
        output_path="s3a://org-093ea7db6a5a/runs/run.test/attempt_1/tables/source_data",
        output_schema_json={},
        value_schema_json={},
    )
    schema_payload = {
        "wrapped_schema": {
            "type": "struct",
            "fields": [{"name": "id", "type": "integer", "nullable": True, "metadata": {}}],
        }
    }

    class _Store:
        def get_json(self, uri):
            if uri.endswith("/outputs/stage_source_data.json"):
                return completed.to_dict()
            if uri.endswith("/outputs/schemas/source_data.json"):
                return schema_payload
            raise AssertionError(uri)

    monkeypatch.setattr(
        "agentcicd.sql.engine.backends.spark.stage_artifacts.object_store_from_env",
        lambda: _Store(),
    )
    spark = _FakeSpark()

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend.set_execution_plan_context(plan)

    copied_path = "s3a://org-093ea7db6a5a/runs/run.test/attempt_2/tables/source_data"
    assert dict(backend._snapshot_known_tables()[0]) == {"source_data": copied_path}
    assert ("load", "parquet", copied_path) in spark.calls
    assert backend.should_skip_materialized_stage(plan[0])
    assert not backend.should_skip_materialized_stage(plan[1])


def test_spark_execution_backend_reads_copied_schema_sidecar_from_object_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_1")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_2")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "fixture_results")
    schema_payload = {
        "wrapped_schema": {
            "type": "struct",
            "fields": [
                {
                    "name": "fixture_response",
                    "type": {
                        "type": "struct",
                        "fields": [
                            {"name": "value", "type": "string", "nullable": True, "metadata": {}},
                        ],
                    },
                    "nullable": True,
                    "metadata": {},
                }
            ],
        }
    }

    class _Store:
        def get_json(self, uri):
            assert uri == (
                "agentcicd-object://org.093ea7db6a5a/runs/run.test/attempt_2/"
                "outputs/schemas/fixture_results.json"
            )
            return schema_payload

    monkeypatch.setattr(
        "agentcicd.sql.engine.backends.spark.stage_artifacts.object_store_from_env",
        lambda: _Store(),
    )
    spark = _FakeSpark()
    plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE successful_rows
        SELECT fixture_response['value'] AS value FROM fixture_results;
        """,
        external_tables={"fixture_results"},
    ).compile_plan(include_cells=True)

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend.set_execution_plan_context(plan)

    assert any(call[0] == "schema" and call[1] == "parquet" for call in spark.calls)


def test_spark_execution_backend_loads_sources_and_publishes_manifest(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(
        spark,
        working_dir=str(tmp_path),
        source_loader=_RecordingSourceLoader(),
    )

    backend.load_table("raw", "/tmp/raw.parquet", {"format": "parquet"})
    backend.publish_dataset("raw", "dataset-name")

    assert ("load", "parquet", "/tmp/raw.parquet") in spark.calls
    assert any(call[0] == "save" and call[4].endswith("/sources/raw") for call in spark.calls)
    manifest = json.loads((tmp_path / "published" / "dataset_raw.json").read_text(encoding="utf-8"))
    assert manifest["dataset_name"] == "dataset-name"


def test_spark_execution_backend_infers_and_normalizes_load_formats(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(
        spark,
        working_dir=str(tmp_path),
        source_loader=_RecordingSourceLoader(),
    )

    backend.load_table("events", "https://example.com/events.jsonl", {})
    backend.load_table("rows", "/tmp/rows.csv", {})

    assert ("source_loader", "https://example.com/events.jsonl", {}) in spark.calls
    assert ("source_loader", "/tmp/rows.csv", {}) in spark.calls


def test_spark_execution_backend_normalizes_jsonl_save_format(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))

    backend.create_batch_table("out", "SELECT 1 AS x")
    backend.save_table("out", "/tmp/events.jsonl", {"format": "jsonl"})

    assert any(call[0] == "save" and call[4] == "/tmp/events.jsonl" and call[3] == "json" for call in spark.calls)
    manifest = json.loads((tmp_path / "outputs" / "save_table_out.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "jsonl"
    assert manifest["writer_format"] == "json"


def test_spark_execution_backend_tracks_source_paths_with_remote_table_root(tmp_path):
    spark = _FakeSpark()
    paths = default_backend_paths(
        str(tmp_path),
        tables_root="s3a://bucket/runs/run-1/attempts/1/tables",
        checkpoints_root="s3a://bucket/runs/run-1/attempts/1/checkpoints",
    )
    backend = SparkExecutionBackend(
        spark,
        working_dir=str(tmp_path),
        paths=paths,
        source_loader=_RecordingSourceLoader(),
    )

    backend.load_table("raw", "/tmp/raw.parquet", {"format": "parquet"})
    backend.save_table("raw", "/tmp/output-path", {"format": "parquet"})

    assert ("load", "parquet", f"{tmp_path}/sources/raw") in spark.calls
    assert not any(call[0] == "load" and call[2].endswith("/tables/raw") for call in spark.calls)


def test_spark_execution_backend_resolves_remote_batch_table_path_for_stream_sources(tmp_path):
    spark = _FakeSpark()
    paths = default_backend_paths(
        str(tmp_path),
        tables_root="s3a://bucket/runs/run-1/attempts/1/tables",
        checkpoints_root="s3a://bucket/runs/run-1/attempts/1/checkpoints",
    )
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path), paths=paths)

    assert backend._resolve_known_table_path("completed") == "s3a://bucket/runs/run-1/attempts/1/tables/completed"


def test_spark_execution_backend_refreshes_stream_view_with_known_schema(tmp_path):
    spark = _FakeSpark()
    paths = default_backend_paths(
        str(tmp_path),
        tables_root="s3a://bucket/runs/run-1/attempt_1/tables",
        checkpoints_root="s3a://bucket/runs/run-1/attempt_1/checkpoints",
    )
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path), paths=paths)
    schema = object()

    backend._refresh_table_view("simulated_v12", schema=schema)

    assert ("schema", "parquet", schema) in spark.calls
    assert ("load", "parquet", "s3a://bucket/runs/run-1/attempt_1/tables/simulated_v12") in spark.calls
    assert ("temp_view", "frame:s3a://bucket/runs/run-1/attempt_1/tables/simulated_v12", "simulated_v12") in spark.calls


def test_spark_execution_backend_registers_known_stream_view_with_cached_schema(tmp_path):
    spark = _FakeSpark()
    paths = default_backend_paths(
        str(tmp_path),
        tables_root="s3a://bucket/runs/run-1/attempt_1/tables",
        checkpoints_root="s3a://bucket/runs/run-1/attempt_1/checkpoints",
    )
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path), paths=paths)
    schema = object()
    backend._record_known_table(
        "simulated_v12",
        "s3a://bucket/runs/run-1/attempt_1/tables/simulated_v12",
        schema=schema,
    )

    backend._register_all_known_views()

    assert ("schema", "parquet", schema) in spark.calls
    assert ("load", "parquet", "s3a://bucket/runs/run-1/attempt_1/tables/simulated_v12") in spark.calls
    assert ("temp_view", "frame:s3a://bucket/runs/run-1/attempt_1/tables/simulated_v12", "simulated_v12") in spark.calls


def test_spark_execution_backend_registers_known_views_from_snapshot(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend._record_known_table("first", "/tmp/first.parquet", schema="schema-first")
    backend._record_known_table("existing", "/tmp/existing.parquet", schema="schema-existing")
    original_read_table_path = backend._read_table_path
    mutated = False

    def mutating_read_table_path(path, *, schema=None):
        nonlocal mutated
        if not mutated:
            mutated = True
            backend._record_known_table("second", "/tmp/second.parquet", schema="schema-second")
        return original_read_table_path(path, schema=schema)

    backend._read_table_path = mutating_read_table_path

    backend._register_all_known_views()

    assert ("schema", "parquet", "schema-first") in spark.calls
    assert ("schema", "parquet", "schema-existing") in spark.calls
    assert ("temp_view", "frame:/tmp/first.parquet", "first") in spark.calls
    assert ("temp_view", "frame:/tmp/existing.parquet", "existing") in spark.calls
    assert ("temp_view", "frame:/tmp/second.parquet", "second") not in spark.calls


def test_spark_execution_backend_registers_previous_completed_table_with_recursive_parquet_fallback(
    tmp_path,
    monkeypatch,
):
    spark = _FakeSpark()
    failed_path = "s3a://org-123/runs/run-1/attempt_1/tables/simulated_v12"
    spark.calls.fail_load_paths = {failed_path}
    monkeypatch.setenv("AGENTCICD_PREVIOUS_RUN_OBJECT_URI", "agentcicd-object://org.123/runs/run-1/attempt_1")
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "simulated_v12")
    plan = EngineEntrypoint(
        """
        CREATE BATCH TABLE simulated_v12
        SELECT 1 AS x;
        """
    ).compile_plan(include_cells=True)
    expected = build_expected_stage_manifests(plan)["simulated_v12"]
    completed = completed_manifest_from_expected(
        expected,
        table_format="parquet",
        output_path=failed_path,
        output_schema_json={},
        value_schema_json={},
    )
    monkeypatch.setenv("AGENTCICD_PREVIOUS_STAGE_MANIFESTS_JSON", json.dumps({"simulated_v12": completed.to_dict()}))

    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    backend.set_execution_plan_context(plan)

    assert ("load", "parquet", failed_path) in spark.calls
    assert ("option", "parquet", "recursiveFileLookup", "true") in spark.calls
    assert ("option", "parquet", "pathGlobFilter", "*.parquet") in spark.calls
    assert ("temp_view", f"frame:{failed_path}", "simulated_v12") in spark.calls


def test_spark_execution_backend_snapshots_remote_tables_for_publication(tmp_path):
    spark = _FakeSpark()
    publication_store = _RecordingPublicationStore()
    paths = default_backend_paths(
        str(tmp_path),
        tables_root="s3a://bucket/runs/run-1/attempt_1/tables",
        checkpoints_root="s3a://bucket/runs/run-1/attempt_1/checkpoints",
    )
    backend = SparkExecutionBackend(
        spark,
        working_dir=str(tmp_path),
        paths=paths,
        publication_store=publication_store,
    )

    backend.create_batch_table("metrics", "SELECT 'accuracy' AS metric, 1.0 AS value")
    backend.publish_report("metrics", "metric")

    assert any(
        call[0] == "save"
        and call[4] == "s3a://bucket/runs/run-1/attempt_1/tables/metrics"
        for call in spark.calls
    )
    assert any(
        call[0] == "save"
        and call[4] == str(tmp_path / "published_tables" / "metrics")
        for call in spark.calls
    )
    assert publication_store.calls[0][1].tables_root == str(tmp_path / "published_tables")


def test_compile_plan_marks_load_steps_for_cell_wrapping():
    script = """
    LOAD raw FROM '/tmp/raw.jsonl' WITH FORMAT='jsonl';

    CREATE BATCH TABLE out
    SELECT id, name
    FROM raw;
    """

    plan = EngineEntrypoint(script).compile_plan(include_cells=True)

    assert plan[0].kind == "load_table"
    assert plan[0].payload["wrap_cells"] is True


def test_spark_execution_backend_wraps_loaded_sources_when_requested(tmp_path, monkeypatch):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(
        spark,
        working_dir=str(tmp_path),
        source_loader=_RecordingSourceLoader(),
    )

    wrapped = _FakeFrame("wrapped:raw", spark.calls)

    def _fake_wrap(frame, *, source_name):
        spark.calls.append(("wrap_loaded_dataframe", frame.label, source_name))
        return wrapped

    monkeypatch.setattr(backend, "_wrap_loaded_dataframe", _fake_wrap)

    backend.load_table("raw", "/tmp/raw.parquet", {"format": "parquet"}, wrap_cells=True)

    assert ("wrap_loaded_dataframe", "frame:/tmp/raw.parquet", "raw") in spark.calls
    assert any(call[0] == "save" and call[1] == "wrapped:raw" and call[4].endswith("/sources/raw") for call in spark.calls)
    manifest = json.loads((tmp_path / "outputs" / "load_table_raw.json").read_text(encoding="utf-8"))
    assert manifest["wrap_cells"] is True


def test_spark_execution_backend_rejects_wrap_false_option_in_wrapped_mode(tmp_path, monkeypatch):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(
        spark,
        working_dir=str(tmp_path),
        source_loader=_RecordingSourceLoader(),
    )

    def _fail_wrap(frame, *, source_name):
        raise AssertionError("wrap should not be called")

    monkeypatch.setattr(backend, "_wrap_loaded_dataframe", _fail_wrap)

    with pytest.raises(ValueError, match="always wraps"):
        backend.load_table("raw", "/tmp/raw.parquet", {"format": "parquet", "wrap": "false"}, wrap_cells=True)


def test_spark_execution_backend_rejects_legacy_top_level_cell_shape():
    types = pytest.importorskip("pyspark.sql.types")

    legacy_type = types.StructType(
        [
            types.StructField("value", types.StringType(), True),
            types.StructField("error", types.StringType(), True),
            types.StructField("lineage", types.StringType(), True),
        ]
    )
    current_type = types.StructType(
        [
            types.StructField("value", types.StringType(), True),
            types.StructField(
                "metadata",
                types.StructType(
                    [
                        types.StructField("errors", types.ArrayType(types.StringType()), True),
                        types.StructField("latency_ms", types.LongType(), True),
                    ]
                ),
                True,
            ),
            types.StructField("__agentcicd_cell", types.BooleanType(), True),
        ]
    )

    assert SparkExecutionBackend._is_cell_struct_type(legacy_type) is False
    assert SparkExecutionBackend._is_cell_struct_type(current_type) is True


def test_spark_execution_backend_rejects_unsupported_wrapped_input_cells(tmp_path):
    pyspark = pytest.importorskip("pyspark.sql")

    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-engine-strict-cell-schema")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        dataframe = spark.createDataFrame(
            [
                pyspark.Row(
                    score=pyspark.Row(
                        value=10,
                        metadata=pyspark.Row(errors=[]),
                        __agentcicd_cell=True,
                    )
                )
            ],
            "score STRUCT<value:BIGINT,metadata:STRUCT<errors:ARRAY<STRUCT<code:STRING,message:STRING,source:STRING,path:STRING,recoverable:BOOLEAN,cause_code:STRING,cause_message:STRING,details:MAP<STRING,STRING>>>>,__agentcicd_cell:BOOLEAN>",
        )
        backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))

        with pytest.raises(ValueError, match="unsupported cell schema"):
            backend._wrap_loaded_dataframe(dataframe, source_name="raw")
    finally:
        spark.stop()


@pytest.mark.spark
@pytest.mark.spark_smoke
def test_debug_row_stream_preserves_null_error_cell_value(tmp_path, monkeypatch):
    pyspark = pytest.importorskip("pyspark.sql")

    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-debug-expanded-null-value")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        schema = (
            "fixture_response STRUCT<cell_id:STRING,value:STRING,"
            "metadata:STRUCT<errors:ARRAY<STRUCT<code:STRING,message:STRING,source:STRING,path:STRING,"
            "recoverable:BOOLEAN,cause_code:STRING,cause_message:STRING,details:MAP<STRING,STRING>>>,"
            "latency_ms:BIGINT>,__agentcicd_cell:BOOLEAN>"
        )
        dataframe = spark.createDataFrame(
            [
                pyspark.Row(
                    fixture_response=pyspark.Row(
                        cell_id="cell.error",
                        value=None,
                        metadata=pyspark.Row(
                            errors=[
                                pyspark.Row(
                                    code="AGENTCICD_RUNTIME_HTTP_ERROR",
                                    message="intentional flaky test error",
                                    source="custom_custom_run",
                                    path=None,
                                    recoverable=True,
                                    cause_code="HTTPError",
                                    cause_message="HTTP Error 400: Bad Request",
                                    details={},
                                )
                            ],
                            latency_ms=123,
                        ),
                        __agentcicd_cell=True,
                    )
                )
            ],
            schema,
        )
        backend = SparkExecutionBackend(
            spark,
            working_dir=str(tmp_path),
            debug={"store_intermediate_tables": True},
        )
        mirrored: dict[str, bytes] = {}

        class _Store:
            def put_bytes(self, ref, payload, content_type=None):
                mirrored[str(ref)] = bytes(payload)
                assert content_type == "application/x-ndjson"

        monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
        monkeypatch.setattr(
            "agentcicd.sql.engine.backends.spark.debug_streams.object_store_from_env",
            lambda: _Store(),
        )

        backend._write_debug_row_streams("parallel_fixture_b", dataframe, row_count=1)

        row_part = next((tmp_path / "debug" / "tables" / "parallel_fixture_b" / "rows").glob("*.jsonl"))
        row = json.loads(row_part.read_text(encoding="utf-8").strip())
        assert "fixture_response" in row
        assert row["fixture_response"]["value"] is None
        assert row["fixture_response"]["metadata"]["errors"][0]["message"] == "intentional flaky test error"
        assert row["fixture_response"]["metadata"]["latency_ms"] == 123
        assert row["fixture_response"]["__agentcicd_cell"] is True
        mirrored_rows = [
            key
            for key in mirrored
            if key.startswith("agentcicd-object://org.test/runs/run.test/attempt_1/debug/tables/parallel_fixture_b/rows/")
        ]
        assert mirrored_rows
        assert b'"message":"intentional flaky test error"' in mirrored[mirrored_rows[0]]
    finally:
        spark.stop()


@pytest.mark.spark
@pytest.mark.spark_smoke
def test_debug_row_stream_writes_wrapped_cells(tmp_path):
    pyspark = pytest.importorskip("pyspark.sql")

    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-debug-row-stream-values")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        schema = (
            "score STRUCT<cell_id:STRING,value:DOUBLE,"
            "metadata:STRUCT<errors:ARRAY<STRUCT<code:STRING,message:STRING,source:STRING,path:STRING,"
            "recoverable:BOOLEAN,cause_code:STRING,cause_message:STRING,details:MAP<STRING,STRING>>>,"
            "latency_ms:BIGINT>,__agentcicd_cell:BOOLEAN>"
        )
        dataframe = spark.createDataFrame(
            [
                pyspark.Row(
                    score=pyspark.Row(
                        cell_id="cell.score",
                        value=0.82,
                        metadata=pyspark.Row(errors=[], latency_ms=123),
                        __agentcicd_cell=True,
                    )
                )
            ],
            schema,
        )
        backend = SparkExecutionBackend(
            spark,
            working_dir=str(tmp_path),
            debug={"store_intermediate_tables": True},
        )

        backend._write_debug_row_streams("metrics", dataframe, row_count=1)

        row_part = next((tmp_path / "debug" / "tables" / "metrics" / "rows").glob("*.jsonl"))
        row = json.loads(row_part.read_text(encoding="utf-8").strip())
        assert row["score"]["value"] == 0.82
        assert row["score"]["metadata"]["errors"] == []
        assert row["score"]["metadata"]["latency_ms"] == 123
        assert row["score"]["__agentcicd_cell"] is True

        stored = dataframe.collect()[0]["score"]
        assert stored["metadata"]["errors"] == []
        assert stored["metadata"]["latency_ms"] == 123
    finally:
        spark.stop()


def test_debug_load_table_row_stream_is_capped_by_default(tmp_path):
    pyspark = pytest.importorskip("pyspark.sql")

    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-debug-load-cap")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        dataframe = spark.createDataFrame([(1, "a"), (2, "b"), (3, "c")], ["id", "value"])
        backend = SparkExecutionBackend(
            spark,
            working_dir=str(tmp_path),
            debug={"store_intermediate_tables": True, "load_table_row_stream_limit": 2},
        )

        artifacts = backend._write_debug_row_streams("raw", dataframe, row_count=3, stage_kind="load_table")

        row_stream = artifacts["row_stream"]
        assert row_stream["total_rows"] == 2
        assert row_stream["source_total_rows"] == 3
        assert row_stream["row_limit"] == 2

        compact_rows = []
        for path in sorted((tmp_path / "debug" / "tables" / "raw" / "rows").glob("*.jsonl")):
            compact_rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        assert len(compact_rows) == 2
        assert {row["id"] for row in compact_rows}.issubset({1, 2, 3})
    finally:
        spark.stop()


def test_spark_execution_backend_registers_runtime_function_aliases(tmp_path):
    spark = _FakeSpark()
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))

    class _Definition:
        runtime_alias = "embed"

    backend.register_runtime_function("embed", _Definition())
    backend.register_runtime_function("embed", _Definition())

    assert [call for call in spark.calls if call[0] == "udf_register"] == [
        ("udf_register", "embed", "StringType"),
        ("udf_register", "agentcicd_wrapped_embed", "StructType"),
    ]


class _RecordingSourceLoader:
    def load_dataframe(self, spark_session, path, options):
        spark_session.calls.append(("source_loader", path, dict(options)))
        source_format = dict(options).get("format") or "parquet"
        return spark_session.read.format(source_format).load(path)
