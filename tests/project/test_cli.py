from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentcicd.cli import app


def test_validate_command(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text("CREATE BATCH TABLE prepared SELECT 1 AS value;\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["validate", str(project)])

    assert result.exit_code == 0
    assert "Validated" in result.output


def test_run_command_rejects_unsupported_duckdb_backend(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text("CREATE BATCH TABLE prepared SELECT 1 AS value;\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["run", str(project), "--backend", "duckdb"])

    assert result.exit_code != 0
    assert "duckdb" in result.output
