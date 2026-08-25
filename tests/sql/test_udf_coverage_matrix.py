from __future__ import annotations

import os
from pathlib import Path

import pytest
from pyspark.sql import Row

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend
from agentcicd.sql.testing.testing_udfs import MockLlmPythonUdf
from agentcicd.sql.udf_registry import clear_registered_udfs, register_udf


@pytest.fixture
def local_spark():
    pyspark = pytest.importorskip("pyspark.sql")
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-udf-coverage-matrix")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


def _registered_runtime_function(name: str, function_type: str, runtime_alias: str) -> dict:
    return {
        "name": name,
        "type": function_type,
        "call_name": name,
        "runtime_alias": runtime_alias,
        "signature": {
            "parameters": [
                {"name": "text", "type_sql": "STRING", "has_default": False},
                {"name": "model", "type_sql": "STRING", "has_default": True, "default_value": "bge"},
            ]
        },
    }


@pytest.mark.parametrize(
    ("function_name", "function_type", "runtime_alias"),
    [
        ("embed", "py", "embed"),
        ("embed_with_deps", "pydeps", "embed_with_deps"),
        ("container.exec", "container", "container_exec"),
        ("aisystems.http.get", "aisystems", "aisystems_http_get"),
    ],
)
def test_registered_runtime_function_types_execute_end_to_end(
    local_spark,
    tmp_path: Path,
    function_name: str,
    function_type: str,
    runtime_alias: str,
):
    source_path = tmp_path / f"{runtime_alias}_source"
    local_spark.createDataFrame([("alice",), ("bob",)], ["name"]).write.mode("overwrite").parquet(str(source_path))

    script = f"""
    LOAD raw FROM '{source_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE out
    SELECT {function_name}(text=name) AS result
    FROM raw
    ORDER BY name;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(
        script,
        registered_functions=[_registered_runtime_function(function_name, function_type, runtime_alias)],
    ).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
    assert [row["result"]["value"] for row in rows] == [
        f"{runtime_alias}(alice, bge)",
        f"{runtime_alias}(bob, bge)",
    ]


def test_pyudf_executes_in_ir_engine(local_spark, tmp_path: Path):
    clear_registered_udfs()
    register_udf(MockLlmPythonUdf)
    source_path = tmp_path / "pyudf_source"
    local_spark.createDataFrame([("alice",), ("bob",)], ["name"]).write.mode("overwrite").parquet(str(source_path))

    script = f"""
    LOAD raw FROM '{source_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE out
    SELECT mock.llm_call(text=name) AS result
    FROM raw
    ORDER BY name;
    """

    try:
        backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
        EngineEntrypoint(
            script,
            registered_functions=[
                {
                    "name": "mock.llm_call",
                    "type": "python",
                    "call_name": "mock.llm_call",
                    "runtime_alias": "py_mock_llm_call",
                }
            ],
        ).execute(backend, include_cells=True)

        rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
        assert [row["result"]["value"] for row in rows] == ["llm:alice", "llm:bob"]
    finally:
        clear_registered_udfs()
