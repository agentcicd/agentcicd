import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.statements import BatchTableStmt
from agentcicd.sql.lowering.segment_lowering import lower_statement_sql


FAMILY_CASES = [
    ("embed", "embed"),
    ("embed_with_deps", "embed_with_deps"),
    ("aisystems.http.get", "aisystems_http_get"),
    ("container.exec", "container_exec"),
    ("http.post", "http_post"),
]


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
            ]
        },
    }


def _statement_and_registry(script: str, registered_functions: list[dict]):
    entrypoint = EngineEntrypoint(script, registered_functions=registered_functions)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))
    return batch_stmt, registry


@pytest.mark.parametrize(("function_name", "runtime_alias"), FAMILY_CASES)
def test_runtime_family_lowers_with_projection_filter_aggregate(function_name, runtime_alias):
    script = f"""
    CREATE BATCH TABLE out
    SELECT {function_name}(text=value, model='bge') AS embedding
    FROM prepared
    WHERE {function_name}(text=value, model='bge') IS NOT NULL
    GROUP BY value
    HAVING COUNT({function_name}(text=value, model='bge')) > 0;
    """

    batch_stmt, registry = _statement_and_registry(script, [_registered_function(function_name, runtime_alias)])
    lowered_sql = lower_statement_sql(batch_stmt, registry)

    assert runtime_alias.lower() in lowered_sql.lower()
    assert f"COUNT({runtime_alias}(value, 'bge'))".lower() in lowered_sql.lower()


@pytest.mark.parametrize(("function_name", "runtime_alias"), FAMILY_CASES)
def test_runtime_family_uses_default_arg_and_positional_binding(function_name, runtime_alias):
    script = f"""
    CREATE BATCH TABLE out
    SELECT {function_name}(value) AS embedding
    FROM prepared;
    """

    batch_stmt, registry = _statement_and_registry(script, [_registered_function(function_name, runtime_alias)])
    lowered_sql = lower_statement_sql(batch_stmt, registry)

    assert f"{runtime_alias}(value, 'default-model')".lower() in lowered_sql.lower()


@pytest.mark.parametrize(("function_name", "runtime_alias"), FAMILY_CASES)
def test_runtime_family_rejects_invalid_argument_count(function_name, runtime_alias):
    script = f"""
    CREATE BATCH TABLE out
    SELECT {function_name}(value, 'm1', 'extra') AS embedding
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Too many positional arguments"):
        entrypoint = EngineEntrypoint(script, registered_functions=[_registered_function(function_name, runtime_alias)])
        entrypoint.lower_script()


@pytest.mark.parametrize(("function_name", "runtime_alias"), FAMILY_CASES)
def test_runtime_family_rejects_invalid_kwarg(function_name, runtime_alias):
    script = f"""
    CREATE BATCH TABLE out
    SELECT {function_name}(badarg=value) AS embedding
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Invalid keyword argument"):
        entrypoint = EngineEntrypoint(script, registered_functions=[_registered_function(function_name, runtime_alias)])
        entrypoint.lower_script()


@pytest.mark.parametrize(("function_name", "runtime_alias"), FAMILY_CASES)
def test_runtime_family_rejects_missing_required_arg(function_name, runtime_alias):
    script = f"""
    CREATE BATCH TABLE out
    SELECT {function_name}(model='bge') AS embedding
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Missing required argument 'text'"):
        entrypoint = EngineEntrypoint(script, registered_functions=[_registered_function(function_name, runtime_alias)])
        entrypoint.lower_script()
