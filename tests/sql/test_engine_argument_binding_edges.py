from __future__ import annotations

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.statements import BatchTableStmt
from agentcicd.sql.lowering.segment_lowering import lower_statement_sql


def _registered_function(name: str, runtime_alias: str) -> dict:
    return {
        "name": name,
        "type": "py",
        "call_name": name,
        "runtime_alias": runtime_alias,
        "signature": {
            "parameters": [
                {"name": "text", "type_sql": "STRING", "has_default": False},
                {"name": "model", "type_sql": "STRING", "has_default": True, "default_value": "default-model"},
                {"name": "mode", "type_sql": "STRING", "has_default": True, "default_value": "fast"},
            ]
        },
    }


def _batch_stmt_and_registry(script: str, registered_functions: list[dict]):
    entrypoint = EngineEntrypoint(script, registered_functions=registered_functions)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))
    return batch_stmt, registry


def test_runtime_function_kwargs_are_case_insensitive_and_defaults_fill_remaining_args():
    script = """
    CREATE BATCH TABLE out
    SELECT embed(TEXT=value, MODEL='bge') AS embedding
    FROM prepared;
    """

    batch_stmt, registry = _batch_stmt_and_registry(script, [_registered_function("embed", "embed")])
    lowered_sql = lower_statement_sql(batch_stmt, registry)

    assert "embed(value, 'bge', 'fast')" in lowered_sql.lower()


def test_runtime_function_rejects_duplicate_keyword_arguments():
    script = """
    CREATE BATCH TABLE out
    SELECT embed(text=value, text='override') AS embedding
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Duplicate argument 'text'"):
        EngineEntrypoint(
            script,
            registered_functions=[_registered_function("embed", "embed")],
        ).lower_script()


def test_runtime_function_rejects_positional_argument_after_keyword_argument():
    script = """
    CREATE BATCH TABLE out
    SELECT embed(model='bge', value) AS embedding
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Positional argument cannot follow keyword binding"):
        EngineEntrypoint(
            script,
            registered_functions=[_registered_function("embed", "embed")],
        ).lower_script()

