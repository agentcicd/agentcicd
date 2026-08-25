from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.statements import BatchTableStmt
from agentcicd.sql.lowering.segment_lowering import lower_statement_sql


def test_new_engine_lowering_uses_runtime_alias_for_non_sql_registered_function():
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
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_sql(batch_stmt, registry)

    assert "embed" in lowered_sql.lower()


def test_execution_plan_registers_function_called_by_distinct_call_name():
    script = """
    CREATE BATCH TABLE simulated_conversations
    SELECT support_multi_turn_simulator.run_support_simulation(
      intent = user_message,
      target_agent = target_agent,
      user_model = user_model
    ) AS simulation_result
    FROM prepared_cases;
    """

    entrypoint = EngineEntrypoint(
        script,
        external_tables=["prepared_cases"],
        registered_functions=[
            {
                "id": "fixture.263ad0b895d3edc2",
                "name": "support_multi_turn_simulator_run_support_simulation",
                "type": "py",
                "call_name": "support_multi_turn_simulator.run_support_simulation",
                "runtime_alias": "support_multi_turn_simulator_run_support_simulation",
                "signature": {
                    "parameters": [
                        {"name": "intent", "type_sql": "ANY"},
                        {"name": "target_agent", "type_sql": "ANY"},
                        {"name": "user_model", "type_sql": "ANY"},
                    ]
                },
                "base_url": "http://fixture-runtime",
                "invoke_path": "/invoke/run_support_simulation",
            }
        ],
    )

    plan = entrypoint.compile_plan(include_cells=True)

    assert [
        (step.kind, step.name)
        for step in plan
        if step.kind == "register_runtime_function"
    ] == [
        (
            "register_runtime_function",
            "support_multi_turn_simulator_run_support_simulation",
        )
    ]
