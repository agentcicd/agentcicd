from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agentcicd.config import BackendName
from agentcicd.runtime.local_runner import run_project


pytestmark = [pytest.mark.e2e, pytest.mark.e2e_smoke, pytest.mark.spark]


def test_agentcicd_run_project_executes_folder_project_through_spark(tmp_path: Path) -> None:
    project = _write_project(tmp_path)

    result = run_project(project)

    assert result.backend is BackendName.SPARK
    assert result.run_dir.is_dir()
    assert (result.run_dir / "progress" / "progress.jsonl").is_file()
    assert (result.run_dir / "logs" / "engine_plan.json").is_file()
    assert (result.run_dir / "logs" / "engine_execution_report.json").is_file()
    assert (result.run_dir / "reports" / "report.md").is_file()
    assert (result.run_dir / "reports" / "report.html").is_file()

    metric_rows = _read_json(result.run_dir / "reports" / "metrics.json")
    assert len(metric_rows) == 1
    assert _cell_value(metric_rows[0]["metric"]) == "matched_fixture_rows"
    assert _cell_value(metric_rows[0]["value"]) == 2
    assert metric_rows[0]["tags"] == {"suite": "folder-e2e"}

    progress_text = (result.run_dir / "progress" / "progress.jsonl").read_text(encoding="utf-8")
    report_text = (result.run_dir / "reports" / "report.md").read_text(encoding="utf-8")
    log_text = (result.run_dir / "logs" / "engine_execution_report.json").read_text(encoding="utf-8")
    assert "sk-folder-runner-secret" not in progress_text
    assert "sk-folder-runner-secret" not in report_text
    assert "sk-folder-runner-secret" not in log_text


def _write_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "inputs.yaml").write_text(
        textwrap.dedent(
            """
            prefix: checked
            min_rows: 2
            api_secret: secret.openai
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "secrets.yaml").write_text(
        textwrap.dedent(
            """
            openai:
              type: api_key
              api_key: sk-folder-runner-secret
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "fixture_echo.py").write_text(
        textwrap.dedent(
            """
            from agentcicd import Str, function


            @function
            def echo(value: Str, prefix: Str) -> Str:
                return f"{prefix}:{value}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "recipe.sql").write_text(
        textwrap.dedent(
            """
            DECLARE INPUT prefix STRING;
            DECLARE INPUT min_rows INT;
            DECLARE INPUT api_secret SECRET;

            CREATE BATCH TABLE source_rows AS
            SELECT * FROM VALUES
              ('case-001', 'alpha'),
              ('case-002', 'beta')
            AS source_rows(case_id, message);

            CREATE BATCH TABLE echoed AS
            SELECT
              case_id,
              local.echo(value = message, prefix = prefix) AS echoed_text,
              api_secret AS secret_ref
            FROM source_rows;

            CREATE BATCH TABLE report_rows AS
            SELECT
              'matched_fixture_rows' AS metric,
              SUM(CASE WHEN err_or(echoed_text, '') LIKE concat(prefix, ':%') THEN 1 ELSE 0 END) AS value,
              map('suite', 'folder-e2e') AS tags
            FROM echoed
            HAVING COUNT(*) >= min_rows;

            PUBLISH report_rows TO REPORTS WITH (COMPONENT = METRIC);
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return project


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell_value(value: object) -> object:
    if isinstance(value, dict) and value.get("__agentcicd_cell") is True:
        return value["value"]
    return value
