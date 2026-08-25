from __future__ import annotations

from agentcicd.sql.engine.backends.spark.session import default_codegen_conf


def test_default_codegen_conf_avoids_large_generated_methods(monkeypatch) -> None:
    monkeypatch.delenv("AGENTCICD_SPARK_SQL_CODEGEN_WHOLE_STAGE", raising=False)
    monkeypatch.delenv("AGENTCICD_SPARK_SQL_CODEGEN_MAX_FIELDS", raising=False)
    monkeypatch.delenv("AGENTCICD_SPARK_SQL_CODEGEN_METHOD_SPLIT_THRESHOLD", raising=False)

    assert default_codegen_conf() == {
        "spark.sql.codegen.wholeStage": "false",
        "spark.sql.codegen.maxFields": "50",
        "spark.sql.codegen.methodSplitThreshold": "1024",
    }


def test_default_codegen_conf_allows_env_overrides_and_empty_opt_out(monkeypatch) -> None:
    monkeypatch.setenv("AGENTCICD_SPARK_SQL_CODEGEN_WHOLE_STAGE", "true")
    monkeypatch.setenv("AGENTCICD_SPARK_SQL_CODEGEN_MAX_FIELDS", "")
    monkeypatch.setenv("AGENTCICD_SPARK_SQL_CODEGEN_METHOD_SPLIT_THRESHOLD", "2048")

    assert default_codegen_conf() == {
        "spark.sql.codegen.wholeStage": "true",
        "spark.sql.codegen.methodSplitThreshold": "2048",
    }
