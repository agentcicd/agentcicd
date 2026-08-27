from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.runner import EngineRunConfig, run_script_with_new_engine
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend


@pytest.fixture
def local_spark():
    pyspark = pytest.importorskip("pyspark.sql")
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-engine-integration")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


def test_load_save_publish_and_retrieve_integrate_end_to_end(local_spark, tmp_path: Path):
    raw_path = tmp_path / "raw_input"
    export_path = tmp_path / "exported_scores"
    local_spark.createDataFrame(
        [
            (1, "alice"),
            (2, "bob"),
        ],
        ["id", "name"],
    ).write.mode("overwrite").parquet(str(raw_path))

    annotation_root = tmp_path / "annotation_tasks" / "task-123"
    annotation_root.mkdir(parents=True)
    (annotation_root / "results.jsonl").write_text('{"id": 99, "label": "approved"}\n', encoding="utf-8")

    script = f"""
    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE scored
    SELECT id, name
    FROM raw;

    CREATE BATCH TABLE score_rows
    SELECT 'rows' AS metric, CAST(COUNT(*) AS DOUBLE) AS value
    FROM scored;

    SAVE scored TO '{export_path.as_posix()}'
    WITH FORMAT='parquet';

    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    PUBLISH scored TO DATASET 'customer-support';
    PUBLISH scored TO ANNOTATION QUEUE 'customer support review' AS scored_review
    WITH (
      INSTRUCTIONS = 'Review the row.',
      REVIEWERS_PER_TASK = 3,
      RESERVATION_MINUTES = 30,
      CONSENSUS = 'majority',
      TEMPLATE = '<View />'
    );

    RETRIEVE ANNOTATION RESULTS labeled FROM ANNOTATION REQUEST 'task-123';

    CREATE BATCH TABLE final_labels
    SELECT id, label
    FROM labeled;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    assert export_path.exists()
    exported_rows = local_spark.read.parquet(str(export_path)).collect()
    assert sorted((row["id"]["value"], row["name"]["value"]) for row in exported_rows) == [(1, "alice"), (2, "bob")]

    metrics_manifest = json.loads((tmp_path / "published" / "reports_metric_score_rows.json").read_text(encoding="utf-8"))
    dataset_manifest = json.loads((tmp_path / "published" / "dataset_scored.json").read_text(encoding="utf-8"))
    annotation_manifest = json.loads((tmp_path / "published" / "annotation_scored_review.json").read_text(encoding="utf-8"))
    published_dataset_rows = local_spark.read.parquet(str(tmp_path / "published_datasets" / "scored")).collect()
    assert len(metrics_manifest["rows"]) == 1
    assert metrics_manifest["rows"][0]["metric"]["value"] == "rows"
    assert metrics_manifest["rows"][0]["value"]["value"] == 2.0
    assert metrics_manifest["rows"][0]["tags"] == {}
    assert dataset_manifest["dataset_name"] == "customer-support"
    assert dataset_manifest["data_path"] == "published_datasets/scored"
    assert sorted((row["id"], row["name"]) for row in published_dataset_rows) == [(1, "alice"), (2, "bob")]
    assert annotation_manifest["table"] == "scored"
    assert annotation_manifest["queue_name"] == "customer support review"
    assert annotation_manifest["alias"] == "scored_review"
    assert annotation_manifest["options"]["consensus"] == "majority"

    labeled_rows = local_spark.read.parquet(str(tmp_path / "tables" / "final_labels")).collect()
    assert [(row["id"]["value"], row["label"]["value"]) for row in labeled_rows] == [(99, "approved")]


def test_runner_writes_plan_transpiled_sql_and_execution_report(tmp_path: Path, monkeypatch):
    pyspark = pytest.importorskip("pyspark.sql")

    raw_path = tmp_path / "raw_input"
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-engine-runner-artifacts")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        spark.createDataFrame([(1, "alice")], ["id", "name"]).write.mode("overwrite").parquet(str(raw_path))
    finally:
        spark.stop()

    run_dir = tmp_path / "run"
    context_dir = run_dir / "fixture-definitions"
    context_dir.mkdir(parents=True)
    (context_dir / "context.enriched.json").write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "name": "embed",
                        "type": "py",
                        "call_name": "embed",
                        "runtime_alias": "embed",
                        "signature": {
                            "parameters": [
                                {"name": "text", "type_sql": "STRING", "has_default": False},
                                {"name": "model", "type_sql": "STRING", "has_default": True, "default_value": "bge"},
                            ]
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_RUN_DIR", str(run_dir))

    script = f"""
    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE out
    SELECT embed(text=name) AS embedding
    FROM raw;
    """

    report = run_script_with_new_engine(
        script,
        EngineRunConfig(
            working_dir=str(run_dir),
            progress_file=str(run_dir / "progress" / "progress.jsonl"),
        ),
    )

    assert report.error is None
    plan_manifest = json.loads((run_dir / "logs" / "engine_plan.json").read_text(encoding="utf-8"))
    execution_report = json.loads((run_dir / "logs" / "engine_execution_report.json").read_text(encoding="utf-8"))
    transpiled_files = sorted((run_dir / "logs" / "transpiled").glob("*.sql"))

    plan_kinds = [entry["kind"] for entry in plan_manifest]
    assert "register_runtime_function" in plan_kinds
    assert plan_kinds.index("load_table") < plan_kinds.index("create_batch_table")
    assert execution_report["failed_step_kind"] is None
    assert transpiled_files
    assert any("EMBED" in path.read_text(encoding="utf-8") for path in transpiled_files)
