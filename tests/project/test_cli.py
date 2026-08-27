from __future__ import annotations

import json
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


def test_transpile_command_prints_lowered_sql(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text(
        """
        DECLARE INPUT name STRING DEFAULT 'Ada';

        CREATE BATCH TABLE prepared
        SELECT name AS value;
        """,
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["transpile", str(project)])

    assert result.exit_code == 0
    assert "-- Step 0: declare_variable name" in result.output
    assert "create_batch_table prepared" in result.output
    assert "SELECT name AS value" in result.output


def test_transpile_command_writes_sql_files_and_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output_dir = tmp_path / "transpiled"
    project.mkdir()
    (project / "recipe.sql").write_text("CREATE BATCH TABLE prepared SELECT 1 AS value;\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["transpile", str(project), "--output-dir", str(output_dir)])

    assert result.exit_code == 0
    manifest = json.loads((output_dir / "engine_plan.json").read_text(encoding="utf-8"))
    sql_files = sorted(output_dir.glob("*.sql"))
    table_step = next(item for item in manifest if item["kind"] == "create_batch_table")
    assert table_step["name"] == "prepared"
    assert table_step["sql_file"] in {path.name for path in sql_files}
    assert any(" AS value" in path.read_text(encoding="utf-8") for path in sql_files)


def test_run_command_rejects_unsupported_duckdb_backend(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text("CREATE BATCH TABLE prepared SELECT 1 AS value;\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["run", str(project), "--backend", "duckdb"])

    assert result.exit_code != 0
    assert "duckdb" in result.output
