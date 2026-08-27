import json

from agentcicd.sql.engine.plan import ExecutionPlanStep
from agentcicd.sql.engine import runner as engine_runner
from agentcicd.sql.engine.runner import (
    EngineRunConfig,
    _archive_working_dir_to_object_storage,
    _completed_batch_tables_from_env,
    _load_registered_functions,
    _write_execution_report,
    _write_plan_artifacts,
)
from agentcicd.sql.engine.runtime import ExecutionEvent, ExecutionReport
from agentcicd.sql.ir.functions import RegisteredFunctionParameterSpec, RegisteredFunctionSpec


def test_load_registered_functions_from_context_file(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    context_dir = run_dir / "fixture-definitions"
    context_dir.mkdir(parents=True)
    (context_dir / "context.enriched.json").write_text(
        json.dumps({"fixtures": [{"name": "embed", "type": "py"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_RUN_DIR", str(run_dir))

    items = _load_registered_functions(EngineRunConfig(working_dir=str(tmp_path)))

    assert len(items) == 1
    assert items[0].name == "embed"
    assert items[0].kind == "py"


def test_load_registered_functions_includes_raw_context_file(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    context_dir = run_dir / "fixture-definitions"
    context_dir.mkdir(parents=True)
    (context_dir / "context.raw.json").write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "fixture.1",
                        "name": "ankur.myfunc",
                        "type": "py",
                        "call_name": "ankur.myfunc",
                        "runtime_alias": "ankur_myfunc",
                        "base_url": "http://fixture-runtime",
                        "invoke_path": "/invoke/myfunc",
                        "signature": {"parameters": [{"name": "text", "type_sql": "ANY"}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_RUN_DIR", str(run_dir))

    items = _load_registered_functions(EngineRunConfig(working_dir=str(tmp_path)))

    assert len(items) == 1
    assert items[0].name == "ankur.myfunc"
    assert items[0].call_name == "ankur.myfunc"
    assert items[0].metadata["base_url"] == "http://fixture-runtime"


def test_load_registered_functions_merges_enriched_and_raw_context_files(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    context_dir = run_dir / "fixture-definitions"
    context_dir.mkdir(parents=True)
    (context_dir / "context.enriched.json").write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "fixture.1",
                        "name": "ankur.myfunc",
                        "type": "py",
                        "call_name": "ankur.myfunc",
                        "runtime_alias": "ankur_myfunc",
                        "source_text": "@function\ndef myfunc(text: str) -> int:\n    return 1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (context_dir / "context.raw.json").write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "fixture.1",
                        "name": "ankur.myfunc",
                        "type": "py",
                        "call_name": "ankur.myfunc",
                        "runtime_alias": "ankur_myfunc",
                        "source_text": "",
                        "base_url": "http://fixture-runtime",
                        "invoke_path": "/invoke/myfunc",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_RUN_DIR", str(run_dir))

    items = _load_registered_functions(EngineRunConfig(working_dir=str(tmp_path)))

    assert len(items) == 1
    assert items[0].source_text.startswith("@function")
    assert items[0].metadata["base_url"] == "http://fixture-runtime"


def test_completed_batch_tables_from_env_trims_and_deduplicates(monkeypatch):
    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "question_examples, citation_examples, question_examples, ")

    assert _completed_batch_tables_from_env() == {"question_examples", "citation_examples"}


def test_engine_run_config_defers_parallelism_to_runtime_env(tmp_path):
    config = EngineRunConfig(working_dir=str(tmp_path))

    assert config.max_parallel_stages is None


def test_write_execution_report_creates_json_log(tmp_path):
    report = ExecutionReport(
        events=[
            ExecutionEvent(step_kind="load_table", step_name="raw", status="started", payload={}),
            ExecutionEvent(step_kind="load_table", step_name="raw", status="completed", payload={"path": "/tmp/raw"}),
        ]
    )

    _write_execution_report(str(tmp_path), report)

    payload = json.loads((tmp_path / "logs" / "engine_execution_report.json").read_text(encoding="utf-8"))
    assert payload["events"][0]["step_kind"] == "load_table"
    assert payload["events"][1]["payload"]["path"] == "/tmp/raw"
    assert payload["error"] is None


def test_progress_reporter_jsonl_works_with_engine_report_shape(tmp_path):
    from agentcicd.sql.engine.progress_reporter import ProgressReporter

    progress_path = tmp_path / "progress" / "progress.jsonl"
    reporter = ProgressReporter(progress_path)

    reporter.emit("load_table", "raw", "started", None, None)
    reporter.emit("load_table", "raw", "completed", None, {"path": "/tmp/raw"})

    lines = [json.loads(line) for line in progress_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["step_type"] == "load_table"
    assert lines[0]["status"] == "running"
    assert lines[1]["status"] == "completed"


def test_write_plan_artifacts_persists_manifest_and_sql_files(tmp_path):
    plan = [
        ExecutionPlanStep(
            kind="create_batch_table",
            name="out",
            payload={"sql": "SELECT 1 AS x"},
            dependencies=["table:raw"],
        ),
        ExecutionPlanStep(
            kind="save_table",
            name="out",
            payload={"path": "/tmp/out", "options": {"format": "delta"}},
            dependencies=["table:out"],
        ),
    ]

    _write_plan_artifacts(str(tmp_path), plan)

    manifest = json.loads((tmp_path / "logs" / "engine_plan.json").read_text(encoding="utf-8"))
    assert manifest[0]["kind"] == "create_batch_table"
    assert manifest[0]["dependencies"] == ["table:raw"]
    sql_artifact = (tmp_path / "logs" / "transpiled" / "00_create_batch_table_out.sql").read_text(encoding="utf-8")
    assert "SELECT 1 AS x" in sql_artifact


def test_run_script_executes_default_injected_fixture_pool_plan(tmp_path, monkeypatch):
    created_sql: list[str] = []

    class _Spark:
        class _Conf:
            def set(self, *_args):
                return None

        conf = _Conf()

        def stop(self):
            return None

    class _Backend:
        def __init__(self, *_args, **_kwargs):
            return None

        def declare_variable(self, _name, _sql):
            return None

        def register_sql_function(self, _name, _definition):
            return None

        def register_runtime_function(self, _name, _definition):
            return None

        def create_batch_table(self, _name, sql, *, options=None):
            created_sql.append(sql)

    monkeypatch.setenv("AGENTCICD_COMPLETED_BATCH_TABLES", "prepared")
    monkeypatch.setattr(engine_runner, "build_spark_session", lambda **_kwargs: _Spark())
    monkeypatch.setattr(engine_runner, "SparkExecutionBackend", _Backend)
    fixture = RegisteredFunctionSpec(
        name="svc.score",
        kind="remote",
        runtime_alias="svc_score",
        signature=(
            RegisteredFunctionParameterSpec(name="text", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="pool", type_sql="POOL", has_default=True),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/score",
            "pool_kind": "service",
            "return_type_sql": "STRING",
        },
    )

    engine_runner.run_script_with_new_engine(
        "CREATE BATCH TABLE out SELECT svc.score(text = text) AS value FROM prepared;",
        EngineRunConfig(working_dir=str(tmp_path), registered_functions=[fixture]),
    )

    assert created_sql
    assert "service_pool.value" in created_sql[0]
    assert "AGENTCICD_WRAPPED_SVC_SCORE" in created_sql[0]


def test_archive_working_dir_includes_published_datasets(tmp_path, monkeypatch):
    class _Client:
        uploads: list[str] = []

        def __init__(self, *args, **kwargs):
            pass

        def bucket_exists(self, bucket):
            return True

        def make_bucket(self, bucket):
            pass

        def put_object(self, bucket, object_name, data, *, length, content_type):
            self.uploads.append(object_name)

        def fput_object(self, bucket, object_name, path):
            self.uploads.append(object_name)

    run_dir = tmp_path / "run"
    (run_dir / "published_datasets" / "scored").mkdir(parents=True)
    (run_dir / "published_datasets" / "scored" / "part-000.parquet").write_bytes(b"data")
    monkeypatch.setenv("AGENTCICD_ORGANIZATION_ID", "org.test")
    monkeypatch.setenv("AGENTCICD_RUN_ID", "run.test")
    monkeypatch.setattr("agentcicd.sql.engine.runner.Minio", _Client)

    _archive_working_dir_to_object_storage(str(run_dir))

    assert "runs/run.test/attempt_1/published_datasets/scored/part-000.parquet" in _Client.uploads
