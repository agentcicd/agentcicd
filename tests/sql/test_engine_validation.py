from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.validator import validate_lowered_sql


def test_validate_lowered_sql_sqlglot_fallback_accepts_spark_sql():
    result = validate_lowered_sql("SELECT q FROM prepared")

    assert result.ok is True
    assert result.engine == "sqlglot_fallback"


def test_engine_entrypoint_lower_and_validate_script():
    script = """
    CREATE BATCH TABLE out
    SELECT embed(text=value, model='bge') AS embedding
    FROM prepared;
    """

    entrypoint = EngineEntrypoint(
        script,
        registered_functions=[
            {
                "name": "embed",
                "type": "py",
                "call_name": "embed",
                "runtime_alias": "embed",
                "signature": {
                    "parameters": [
                        {"name": "text", "type_sql": "STRING", "has_default": False},
                        {"name": "model", "type_sql": "STRING", "has_default": True},
                    ]
                },
            }
        ],
    )

    lowered = entrypoint.lower_script(include_cells=True)
    validations = entrypoint.validate_lowered_script(include_cells=True)

    assert len(lowered) == 1
    assert "NAMED_STRUCT" in lowered[0]
    assert len(validations) == 1
    assert validations[0].ok is True
