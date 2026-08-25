from __future__ import annotations

from pathlib import Path

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend


@pytest.fixture
def local_spark():
    pyspark = pytest.importorskip("pyspark.sql")
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-engine-interface-edges")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


def test_retrieve_annotation_supports_json_results(local_spark, tmp_path: Path):
    annotation_root = tmp_path / "annotation_tasks" / "task-json"
    annotation_root.mkdir(parents=True)
    (annotation_root / "results.json").write_text(
        '[{"id": 1, "label": "approved"}, {"id": 2, "label": "review"}]',
        encoding="utf-8",
    )

    script = """
    RETRIEVE ANNOTATION RESULTS labeled FROM ANNOTATION REQUEST 'task-json';

    CREATE BATCH TABLE out
    SELECT id, label
    FROM labeled;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
    assert sorted((row["id"]["value"], row["label"]["value"]) for row in rows) == [(1, "approved"), (2, "review")]


def test_save_jsonl_normalization_writes_readable_json_records(local_spark, tmp_path: Path):
    raw_path = tmp_path / "raw_input"
    export_path = tmp_path / "saved_jsonl"
    local_spark.createDataFrame([(1, "alice")], ["id", "name"]).write.mode("overwrite").parquet(str(raw_path))

    script = f"""
    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE scored
    SELECT id, name
    FROM raw;

    SAVE scored TO '{export_path.as_posix()}'
    WITH FORMAT='jsonl';
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.json(str(export_path)).collect()
    assert [(row["id"]["value"], row["name"]["value"]) for row in rows] == [(1, "alice")]
