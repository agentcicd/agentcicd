import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint


def test_compile_plan_rejects_direct_recursive_sql_function():
    script = """
    CREATE FUNCTION helpers.loop(value STRING)
    RETURNS STRING
    RETURN helpers.loop(value=value);

    LOAD prepared FROM 's3://bucket/prepared';

    CREATE BATCH TABLE out
    SELECT helpers.loop(value=q) AS answer
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Cyclic plan dependencies detected"):
        EngineEntrypoint(script).compile_plan(include_cells=True)


def test_compile_plan_rejects_indirect_recursive_sql_functions():
    script = """
    CREATE FUNCTION helpers.first(value STRING)
    RETURNS STRING
    RETURN helpers.second(value=value);

    CREATE FUNCTION helpers.second(value STRING)
    RETURNS STRING
    RETURN helpers.first(value=value);

    LOAD prepared FROM 's3://bucket/prepared';

    CREATE BATCH TABLE out
    SELECT helpers.first(value=q) AS answer
    FROM prepared;
    """

    with pytest.raises(ValueError, match="Cyclic plan dependencies detected"):
        EngineEntrypoint(script).compile_plan(include_cells=True)
