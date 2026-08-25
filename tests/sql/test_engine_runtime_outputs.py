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
        .appName("agentcicd-engine-runtime-outputs")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


def test_runtime_outputs_case_where_order_limit(local_spark, tmp_path: Path):
    source_path = tmp_path / "raw_prepared"
    local_spark.createDataFrame(
        [
            (1, "alice", 5, True),
            (2, None, 20, True),
            (3, "carol", 8, False),
        ],
        ["id", "name", "score", "active"],
    ).write.mode("overwrite").parquet(str(source_path))

    script = f"""
    LOAD raw FROM '{source_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE filtered
    SELECT
      CASE WHEN score > 10 THEN score ELSE 0 END AS score_norm,
      COALESCE(name, 'unknown') AS safe_name
    FROM raw
    WHERE active = true
    ORDER BY score DESC
    LIMIT 2;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "filtered")).collect()
    values = [(row["score_norm"]["value"], row["safe_name"]["value"]) for row in rows]

    assert values == [(20, "unknown"), (0, "alice")]
    assert rows[0]["score_norm"]["metadata"]["errors"] == []
    assert rows[0]["safe_name"]["metadata"]["errors"] == []


def test_runtime_outputs_select_star_through_cell_lowered_cte(local_spark, tmp_path: Path):
    source_path = tmp_path / "star_raw"
    local_spark.createDataFrame(
        [
            (1, "alice", 10),
            (2, "bob", 20),
        ],
        ["id", "name", "score"],
    ).write.mode("overwrite").parquet(str(source_path))

    script = f"""
    LOAD raw FROM '{source_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE out
    WITH prepared AS (
      SELECT *
      FROM raw
    )
    SELECT *
    FROM prepared
    ORDER BY id;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
    assert [(row["id"]["value"], row["name"]["value"], row["score"]["value"]) for row in rows] == [
        (1, "alice", 10),
        (2, "bob", 20),
    ]


def test_runtime_outputs_inline_values_star_feeds_next_cell_table(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE support_cases
    SELECT *
    FROM VALUES
      ('case-001', 'Where is order ORD-1001?'),
      ('case-002', 'What is your return policy?')
    AS support_cases(case_id, customer_message);

    CREATE BATCH TABLE out
    SELECT case_id, customer_message, concat(customer_message, case_id) AS response
    FROM support_cases;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
    assert sorted(row["case_id"]["value"] for row in rows) == ["case-001", "case-002"]
    assert all("lineage" not in row["response"]["metadata"].asDict() for row in rows)
