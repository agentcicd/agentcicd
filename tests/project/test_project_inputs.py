from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from agentcicd.config import BackendName
from agentcicd.errors import BackendNotSupportedError, InputCoercionError, ProjectLoadError
from agentcicd.project import load_project
from agentcicd.runtime import local_runner
from agentcicd.runtime.local_runner import _configure_local_spark_python, prepare_run, run_project, validate_project


def test_load_project_coerces_yaml_inputs_and_secrets(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="""
        DECLARE INPUT source DATASET;
        DECLARE INPUT judge SECRET;
        DECLARE INPUT limit_rows RATELIMIT DEFAULT 1;
        DECLARE INPUT flags VARIANT DEFAULT '{}';
        CREATE BATCH TABLE prepared SELECT 1 AS value;
        """,
        inputs_yaml="""
        source: inputs/cases.csv
        judge: secret.OPENAI_API_KEY
        limit_rows: 3
        flags:
          expected:
            - refund
            - escalation
        """,
        secrets_yaml="""
        OPENAI_API_KEY:
          type: api_key
          value: sk-test
          description: local judge key
        RAW_TOKEN: local-token
        """,
    )

    spec = load_project(project)

    assert spec.backend == BackendName.SPARK
    assert spec.inputs.input_values["source"] == (project / "inputs" / "cases.csv").resolve().as_posix()
    assert spec.inputs.input_values["judge"] == "secret.OPENAI_API_KEY"
    assert spec.inputs.input_values["limit_rows"] == "3"
    assert json.loads(spec.inputs.input_values["flags"]) == {"expected": ["refund", "escalation"]}
    assert [record.to_runtime_record() for record in spec.secrets] == [
        {
            "id": "secret.OPENAI_API_KEY",
            "organization_id": "local",
            "key": "OPENAI_API_KEY",
            "description": "local judge key",
            "secret_type": "api_key",
            "value": "sk-test",
            "secret": {"type": "api_key", "api_key": "sk-test"},
        },
        {
            "id": "secret.RAW_TOKEN",
            "organization_id": "local",
            "key": "RAW_TOKEN",
            "description": None,
            "secret_type": "raw",
            "value": "local-token",
            "secret": {"type": "raw", "value": "local-token"},
        },
    ]


def test_properties_files_remain_scalar_compatibility_path(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="""
        DECLARE INPUT source DATASET;
        DECLARE INPUT judge SECRET;
        CREATE BATCH TABLE prepared SELECT 1 AS value;
        """,
    )
    (project / "input.properties").write_text(
        "source=inputs/cases.csv\njudge=secret.LOCAL\n",
        encoding="utf-8",
    )
    (project / "secret.properties").write_text("LOCAL=value\n", encoding="utf-8")

    spec = load_project(project)

    assert spec.inputs.input_values["judge"] == "secret.LOCAL"
    assert spec.secrets[0].to_runtime_record()["value"] == "value"


def test_load_project_rejects_unknown_yaml_inputs(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="DECLARE INPUT source DATASET DEFAULT 'inputs/default.csv';",
        inputs_yaml="extra: value\n",
    )

    with pytest.raises(InputCoercionError, match="Unknown input key"):
        load_project(project)


def test_load_project_rejects_raw_secret_in_inputs_yaml(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="DECLARE INPUT judge SECRET;",
        inputs_yaml="judge: sk-raw-value\n",
    )

    with pytest.raises(InputCoercionError, match="starting with 'secret.'"):
        load_project(project)


def test_load_project_rejects_missing_required_input(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="DECLARE INPUT source DATASET;",
        inputs_yaml="{}\n",
    )

    with pytest.raises(InputCoercionError, match="Missing required input"):
        load_project(project)


def test_load_project_rejects_list_for_scalar_input(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="DECLARE INPUT name STRING;",
        inputs_yaml="name:\n  - a\n",
    )

    with pytest.raises(InputCoercionError, match="does not accept YAML list/object"):
        load_project(project)


def test_load_project_reads_backend_from_config(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="CREATE BATCH TABLE prepared SELECT 1 AS value;",
        agentcicd_toml="""
        [run]
        backend = "spark"
        max_parallel_stages = 2
        """,
    )

    spec = load_project(project)

    assert spec.config.run.backend == BackendName.SPARK
    assert spec.config.run.max_parallel_stages == 2


def test_validate_project_does_not_execute_spark(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="CREATE BATCH TABLE prepared SELECT 1 AS value;",
    )

    spec = validate_project(project)

    assert spec.paths.root == project
    assert not spec.paths.run_root.exists()


def test_prepare_run_reserves_the_local_run_directory_before_execution(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="CREATE BATCH TABLE prepared SELECT 1 AS value;",
    )

    prepared = prepare_run(project)

    assert prepared.run_dir.is_dir()
    assert prepared.run_dir.parent == project / ".agentcicd" / "runs"
    assert (prepared.run_dir / "progress").is_dir()


def test_configure_local_spark_python_uses_current_interpreter_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYSPARK_PYTHON", raising=False)

    _configure_local_spark_python()

    assert os.environ["PYSPARK_PYTHON"] == sys.executable


def test_configure_local_spark_python_preserves_explicit_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYSPARK_PYTHON", "/opt/custom/python")

    _configure_local_spark_python()

    assert os.environ["PYSPARK_PYTHON"] == "/opt/custom/python"


def test_restore_sigint_handler_restores_the_cli_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    restored: list[object] = []
    handler = local_runner.signal.default_int_handler

    monkeypatch.setattr(local_runner.signal, "signal", lambda _signal_number, received_handler: restored.append(received_handler))

    local_runner._restore_sigint_handler(handler)

    assert restored == [handler]


def test_run_project_rejects_duckdb_backend_in_v1(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="CREATE BATCH TABLE prepared SELECT 1 AS value;",
    )

    with pytest.raises(BackendNotSupportedError, match="duckdb"):
        run_project(project, backend=BackendName.DUCKDB)


def test_load_project_rejects_both_yaml_and_properties_inputs(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        recipe_sql="DECLARE INPUT source DATASET DEFAULT 'inputs/default.csv';",
        inputs_yaml="source: inputs/cases.csv\n",
    )
    (project / "input.properties").write_text("source=inputs/other.csv\n", encoding="utf-8")

    with pytest.raises(ProjectLoadError, match="Use either inputs.yaml or input.properties"):
        load_project(project)


def _write_project(
    tmp_path: Path,
    *,
    recipe_sql: str,
    inputs_yaml: str | None = None,
    secrets_yaml: str | None = None,
    agentcicd_toml: str | None = None,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text(textwrap.dedent(recipe_sql).strip() + "\n", encoding="utf-8")
    if inputs_yaml is not None:
        (project / "inputs.yaml").write_text(textwrap.dedent(inputs_yaml).strip() + "\n", encoding="utf-8")
    if secrets_yaml is not None:
        (project / "secrets.yaml").write_text(textwrap.dedent(secrets_yaml).strip() + "\n", encoding="utf-8")
    if agentcicd_toml is not None:
        (project / "agentcicd.toml").write_text(textwrap.dedent(agentcicd_toml).strip() + "\n", encoding="utf-8")
    return project
