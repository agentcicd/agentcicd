from __future__ import annotations

from pathlib import Path

import pytest
from pyspark.sql import Row

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend


@pytest.fixture
def local_spark():
    pyspark = pytest.importorskip("pyspark.sql")
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-engine-stream-runtime")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


def test_stream_table_executes_through_spark_backend_with_cell_outputs(local_spark, tmp_path: Path):
    source_path = tmp_path / "stream_input"
    local_spark.createDataFrame(
        [
            ("alice", 3),
            ("bob", 8),
        ],
        ["name", "score"],
    ).write.mode("overwrite").parquet(str(source_path))

    script = f"""
    LOAD raw FROM '{source_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE STREAM TABLE live_scores
    OPTIONS (BATCH_SIZE=1)
    SELECT name, score * 2 AS doubled_score
    FROM raw
    WHERE score > 4;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "live_scores")).collect()
    assert [(row["name"]["value"], row["doubled_score"]["value"]) for row in rows] == [("bob", 16)]
    assert rows[0]["doubled_score"]["metadata"]["errors"] == []
    assert "lineage" not in rows[0]["doubled_score"]["metadata"].asDict()
    assert (tmp_path / "checkpoints" / "live_scores").exists()
    assert (tmp_path / "stream_batches" / "raw" / "1").exists()


def test_stream_table_checkpoint_reuse_only_processes_new_files(local_spark, tmp_path: Path):
    input_path = tmp_path / "stream_restart_input"
    local_spark.createDataFrame(
        [
            ("alice", 3),
            ("bob", 8),
        ],
        ["name", "score"],
    ).write.mode("overwrite").parquet(str(input_path))

    load_script = f"""
    LOAD raw FROM '{input_path.as_posix()}'
    WITH FORMAT='parquet';
    """

    stream_script = """
    CREATE STREAM TABLE live_scores
    SELECT name, score
    FROM raw
    WHERE score >= 3;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(load_script).execute(backend, include_cells=True)
    EngineEntrypoint(stream_script, external_tables={"raw"}).execute(backend, include_cells=True)

    initial_rows = local_spark.read.parquet(str(tmp_path / "tables" / "live_scores")).collect()
    assert [(row["name"]["value"], row["score"]["value"]) for row in initial_rows] == [
        ("alice", 3),
        ("bob", 8),
    ]

    local_spark.createDataFrame(
        [
            Row(
                name=Row(
                    cell_id=None,
                    value="carol",
                    metadata=Row(errors=[], latency_ms=None),
                    __agentcicd_cell=True,
                ),
                score=Row(
                    cell_id=None,
                    value=12,
                    metadata=Row(errors=[], latency_ms=None),
                    __agentcicd_cell=True,
                ),
            ),
        ],
        "name STRUCT<cell_id:STRING,value:STRING,metadata:STRUCT<errors:ARRAY<STRUCT<code:STRING,message:STRING,source:STRING,path:STRING,recoverable:BOOLEAN,cause_code:STRING,cause_message:STRING,details:MAP<STRING,STRING>>>,latency_ms:BIGINT>,__agentcicd_cell:BOOLEAN>, score STRUCT<cell_id:STRING,value:BIGINT,metadata:STRUCT<errors:ARRAY<STRUCT<code:STRING,message:STRING,source:STRING,path:STRING,recoverable:BOOLEAN,cause_code:STRING,cause_message:STRING,details:MAP<STRING,STRING>>>,latency_ms:BIGINT>,__agentcicd_cell:BOOLEAN>",
    ).write.mode("append").parquet(str(tmp_path / "sources" / "raw"))

    EngineEntrypoint(stream_script, external_tables={"raw"}).execute(backend, include_cells=True)

    final_rows = local_spark.read.parquet(str(tmp_path / "tables" / "live_scores")).collect()
    assert [(row["name"]["value"], row["score"]["value"]) for row in final_rows] == [
        ("alice", 3),
        ("bob", 8),
        ("carol", 12),
    ]
