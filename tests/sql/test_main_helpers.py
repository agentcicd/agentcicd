import pytest
import typer

from agentcicd.sql.main import (
    _apply_macros,
    _load_script,
    _parse_macro_definitions,
)


def test_parse_macro_definitions_valid():
    macros = _parse_macro_definitions(["FOO=bar", "LIMIT=10"])
    assert macros == {"FOO": "bar", "LIMIT": "10"}


def test_parse_macro_definitions_missing_equals():
    with pytest.raises(typer.BadParameter, match="must be in KEY=VALUE"):
        _parse_macro_definitions(["FOO"])


def test_parse_macro_definitions_missing_key():
    with pytest.raises(typer.BadParameter, match="missing a key"):
        _parse_macro_definitions(["=value"])


def test_apply_macros_replaces_tokens():
    script = "SELECT * FROM $TABLE WHERE col = '$VALUE'"
    rendered = _apply_macros(script, {"TABLE": "my_table", "VALUE": "x"})
    assert rendered == "SELECT * FROM my_table WHERE col = 'x'"


def test_apply_macros_rewrites_standalone_limit_rows_placeholder():
    script = "SELECT *\nFROM source\n$LIMIT_ROWS;\n"
    rendered = _apply_macros(script, {"LIMIT_ROWS": "1"})
    assert rendered.strip() == "SELECT *\nFROM source\nLIMIT 1;"


def test_load_script_then_parser_accepts_standalone_limit_rows(tmp_path):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text(
        "CREATE BATCH TABLE out\nSELECT *\nFROM source\n$LIMIT_ROWS;\n",
        encoding="utf-8",
    )

    rendered = _load_script(sql_file, ["LIMIT_ROWS=1"])

    from agentcicd.sql.parsing.parser import AgentCICDScriptParser

    blocks = AgentCICDScriptParser(rendered).parse()
    assert len(blocks) == 1
