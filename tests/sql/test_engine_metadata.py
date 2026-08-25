import sqlglot

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.expressions import CallExpr, ColumnRefExpr, KeywordArgExpr, LiteralExpr, SqlAstExpr
from agentcicd.sql.ir.metadata import CellComponentsIR
from agentcicd.sql.ir.statements import BatchTableStmt
from agentcicd.sql.lowering.sql_lowering import lower_expr
from agentcicd.sql.lowering.metadata_lowering import build_cell_struct, lower_expr_to_cell
from agentcicd.sql.lowering.segment_lowering import lower_statement_cells_sql


def test_lower_expr_to_cell_for_column_and_literal():
    column_cell = lower_expr_to_cell(ColumnRefExpr(name="q"))
    literal_cell = lower_expr_to_cell(LiteralExpr(value="hi"))

    assert column_cell.value_sql.sql(dialect="spark") == "q"
    assert not hasattr(column_cell, "lineage_sql")
    assert literal_cell.value_sql.sql(dialect="spark") == "'hi'"
    assert not hasattr(literal_cell, "lineage_sql")


def test_lower_expr_to_cell_for_wrapped_column_uses_nested_metadata_fields():
    column_cell = lower_expr_to_cell(ColumnRefExpr(name="q"), assume_cell_columns=True)

    assert column_cell.value_sql.sql(dialect="spark") == "q.value"
    assert column_cell.error_sql.sql(dialect="spark") == "q.metadata.errors"
    assert column_cell.latency_sql.sql(dialect="spark") == "q.metadata.latency_ms"


def test_build_cell_struct_includes_fixture_trace_metadata_field():
    cell = lower_expr_to_cell(LiteralExpr(value="ok"))

    cell_sql = build_cell_struct(cell).sql(dialect="spark")

    assert "'fixture_trace'" in cell_sql
    assert "CAST(NULL AS STRUCT<schema_version: STRING" in cell_sql


def test_lower_expr_to_cell_for_remote_function_merges_metadata():
    entrypoint = EngineEntrypoint(
        "",
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
    registry = entrypoint.registry()

    cell = lower_expr_to_cell(
        CallExpr(
            function_name="embed",
            args=[
                KeywordArgExpr(name="text", value=ColumnRefExpr(name="value")),
                KeywordArgExpr(name="model", value=LiteralExpr(value="bge")),
            ],
        ),
        registry=registry,
    )

    assert "AGENTCICD_WRAPPED_EMBED" in cell.value_sql.sql(dialect="spark")
    assert cell.representation == "cell"
    assert cell.cell_sql is not None
    assert "NAMED_STRUCT" in cell.cell_sql.sql(dialect="spark")
    assert not hasattr(cell, "lineage_sql")


def test_lower_expr_to_cell_supports_is_err_for_derived_expression():
    expression = sqlglot.parse_one("SELECT is_err(score + penalty)", read="spark").expressions[0]

    cell = lower_expr_to_cell(SqlAstExpr(expression=expression), assume_cell_columns=True)

    value_sql = cell.value_sql.sql(dialect="spark")
    assert value_sql.startswith("SIZE(")
    assert "score.metadata.errors" in value_sql
    assert "penalty.metadata.errors" in value_sql
    assert "COALESCE" in value_sql
    assert value_sql.endswith("> 0")
    assert cell.error_sql.sql(dialect="spark") == (
        "CAST(ARRAY() AS ARRAY<STRUCT<code: STRING, message: STRING, source: STRING, path: STRING, "
        "recoverable: BOOLEAN, cause_code: STRING, cause_message: STRING, details: MAP<STRING, STRING>>>)"
    )


def test_lower_expr_to_cell_supports_err_or_for_derived_expression():
    expression = sqlglot.parse_one("SELECT err_or(score + penalty, 0)", read="spark").expressions[0]

    cell = lower_expr_to_cell(SqlAstExpr(expression=expression), assume_cell_columns=True)

    value_sql = cell.value_sql.sql(dialect="spark")
    assert "CASE WHEN SIZE(" in value_sql
    assert "score.metadata.errors" in value_sql
    assert "penalty.metadata.errors" in value_sql
    assert "COALESCE" in value_sql
    assert "THEN 0" in value_sql
    assert "ELSE score.value + penalty.value END" in value_sql
    assert cell.error_sql.sql(dialect="spark") == (
        "CAST(ARRAY() AS ARRAY<STRUCT<code: STRING, message: STRING, source: STRING, path: STRING, "
        "recoverable: BOOLEAN, cause_code: STRING, cause_message: STRING, details: MAP<STRING, STRING>>>)"
    )


def test_lower_expr_to_cell_supports_latency_for_wrapped_column():
    expression = sqlglot.parse_one("SELECT latency(embedding)", read="spark").expressions[0]

    cell = lower_expr_to_cell(SqlAstExpr(expression=expression), assume_cell_columns=True)

    assert cell.value_sql.sql(dialect="spark") == "embedding.metadata.latency_ms"
    assert cell.error_sql.sql(dialect="spark") == (
        "CAST(ARRAY() AS ARRAY<STRUCT<code: STRING, message: STRING, source: STRING, path: STRING, "
        "recoverable: BOOLEAN, cause_code: STRING, cause_message: STRING, details: MAP<STRING, STRING>>>)"
    )


def test_lower_expr_to_cell_latency_returns_null_when_not_applicable():
    expression = sqlglot.parse_one("SELECT latency(score + penalty)", read="spark").expressions[0]

    cell = lower_expr_to_cell(SqlAstExpr(expression=expression), assume_cell_columns=True)

    assert cell.value_sql.sql(dialect="spark") == "NULL"


def test_lower_statement_cells_sql_wraps_top_level_select_projection():
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

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "NAMED_STRUCT" in lowered_sql
    assert "'value'" in lowered_sql
    assert "'metadata'" in lowered_sql
    assert "'errors'" in lowered_sql
    assert "'lineage'" not in lowered_sql
    assert "'__agentcicd_cell'" in lowered_sql
    assert "AGENTCICD_WRAPPED_EMBED" in lowered_sql


def test_lower_statement_cells_sql_wraps_star_from_inline_values():
    script = """
    CREATE BATCH TABLE support_cases
    SELECT *
    FROM VALUES
      ('case-001', 'Where is order ORD-1001?')
    AS support_cases(case_id, customer_message);
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "SELECT *" not in lowered_sql
    assert "case_id.value" not in lowered_sql
    assert "customer_message.value" not in lowered_sql
    assert "NAMED_STRUCT('cell_id', NULL, 'value', CASE WHEN SIZE(" in lowered_sql
    assert "'lineage'" not in lowered_sql
    assert "table:support_cases" not in lowered_sql


def test_lower_statement_cells_sql_wraps_derived_generator_before_outer_references():
    script = """
    DECLARE INPUT num_cases INT DEFAULT 5;

    CREATE BATCH TABLE generated_cases
    SELECT CAST(i AS STRING) AS case_id
    FROM (
      SELECT explode(sequence(0, num_cases - 1)) AS i
    );
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "EXPLODE" in lowered_sql
    assert "NAMED_STRUCT('value', EXPLODE" not in lowered_sql
    assert "FROM (SELECT NAMED_STRUCT('cell_id', NULL, 'value'" in lowered_sql
    assert "FROM (SELECT *, EXPLODE" in lowered_sql
    assert "num_cases.value" in lowered_sql
    assert "num_cases.metadata.errors" in lowered_sql
    assert "TRY_CAST(i.value AS STRING)" in lowered_sql or "CAST(i.value AS STRING)" in lowered_sql
    assert "Can't extract" not in lowered_sql


def test_lower_statement_cells_sql_reads_struct_fields_from_derived_generator_cell_values():
    script = """
    CREATE BATCH TABLE expression_cases
    SELECT
      generated_case.case_id AS case_id,
      generated_case.expression_text AS expression_text
    FROM (
      SELECT explode(cases) AS generated_case
      FROM generated_case_array
    ) exploded_cases;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "generated_case.value.case_id" in lowered_sql
    assert "generated_case.value.expression_text" in lowered_sql
    assert "generated_case.case_id AS case_id" not in lowered_sql
    assert "generated_case.expression_text AS expression_text" not in lowered_sql


def test_lower_statement_cells_sql_reads_generator_struct_fields_inside_sql_function_body():
    script = """
    CREATE FUNCTION unit_prompt(prompt_text STRING)
    RETURNS ARRAY<VARIANT>
    RETURN [
      {'role': 'user', 'content': prompt_text}
    ];

    CREATE BATCH TABLE expression_cases
    SELECT
      unit_prompt(generated_case.prompt_text) AS messages
    FROM (
      SELECT explode(cases) AS generated_case
      FROM generated_case_array
    ) exploded_cases;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "generated_case.value.prompt_text" in lowered_sql
    assert "generated_case.metadata.errors" in lowered_sql
    assert "generated_case.prompt_text.value" not in lowered_sql
    assert "generated_case.prompt_text.metadata.errors" not in lowered_sql


def test_lower_statement_cells_sql_orders_aggregate_by_select_alias():
    script = """
    CREATE BATCH TABLE scale_error_distribution
    SELECT
      scale_error_bucket,
      count(*) AS case_count
    FROM scored_answers
    GROUP BY scale_error_bucket
    ORDER BY case_count DESC;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "case_count.metadata.errors" not in lowered_sql
    assert "case_count.value" in lowered_sql
    assert "COUNT(*)" in lowered_sql.upper()
    assert "ORDER BY" in lowered_sql.upper()


def test_lower_statement_cells_sql_orders_aggregate_group_key_by_alias_value():
    script = """
    CREATE BATCH TABLE segment_summary
    SELECT
      segment,
      COUNT(*) AS row_count
    FROM current_scores
    GROUP BY segment
    HAVING COUNT(*) > 0
    ORDER BY segment;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "ORDER BY segment.value" in lowered_sql
    assert "Wrapped-mode ORDER BY consumed an errored cell" not in lowered_sql
    assert "Wrapped-mode HAVING consumed an errored cell" not in lowered_sql


def test_lower_statement_cells_sql_window_functions_do_not_collect_errors_as_aggregates():
    script = """
    CREATE BATCH TABLE ranked_cases
    SELECT
      case_id,
      segment,
      ROW_NUMBER() OVER (PARTITION BY segment ORDER BY score DESC, routing_weight DESC) AS segment_rank,
      LAG(score) OVER (PARTITION BY segment ORDER BY score DESC, routing_weight DESC) AS previous_score
    FROM judged
    ORDER BY segment, segment_rank;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "PARTITION BY segment.value" in lowered_sql
    assert "ORDER BY score.value DESC, routing_weight.value DESC" in lowered_sql
    assert "COLLECT_LIST(score.metadata.errors" not in lowered_sql
    assert "COLLECT_LIST(routing_weight.metadata.errors" not in lowered_sql


def test_lower_statement_cells_sql_wraps_derived_join_sources():
    script = """
    CREATE BATCH TABLE joined
    SELECT left_rows.id, right_rows.bonus
    FROM (
      SELECT 1 AS id
    ) AS left_rows
    JOIN (
      SELECT 1 AS id, 2 AS bonus
    ) AS right_rows
    ON left_rows.id = right_rows.id;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "NAMED_STRUCT('cell_id', NULL, 'value', CASE WHEN SIZE(" in lowered_sql
    assert "left_rows.id.value = right_rows.id.value" in lowered_sql
    assert "left_rows.id AS id" in lowered_sql
    assert "right_rows.bonus AS bonus" in lowered_sql


def test_lower_statement_cells_sql_does_not_emit_stage_lineage():
    script = """
    CREATE BATCH TABLE agent_responses
    SELECT case_id, concat(customer_message, case_id) AS agent_answer
    FROM support_cases;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "customer_message.metadata.errors" in lowered_sql
    assert "case_id.metadata.errors" in lowered_sql
    assert "metadata.lineage" not in lowered_sql
    assert "table:agent_responses" not in lowered_sql


def test_lower_statement_cells_sql_uses_value_fields_for_arithmetic_and_filter():
    script = """
    CREATE BATCH TABLE out
    SELECT price + tax AS total
    FROM prepared
    WHERE price > 0;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "price.value + tax.value" in lowered_sql
    assert "price.value > 0" in lowered_sql
    assert "price.metadata.errors" in lowered_sql
    assert "tax.metadata.errors" in lowered_sql
    assert "price.metadata.lineage" not in lowered_sql


def test_lower_statement_cells_sql_uses_value_fields_for_join_and_aggregate():
    script = """
    CREATE BATCH TABLE out
    SELECT AVG(left.score) AS avg_score
    FROM left
    JOIN right ON left.id = right.id
    GROUP BY right.segment;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "AVG(left.score.value)" in lowered_sql
    assert "left.id.value = right.id.value" in lowered_sql
    assert "right.segment.value" in lowered_sql


def test_lower_statement_cells_sql_wraps_ctes_on_union_queries_before_join_conditions():
    script = """
    CREATE BATCH TABLE out
    WITH derived AS (
      SELECT
        right_rows.id,
        right_rows.segment,
        CAST(right_rows.score AS DOUBLE) AS score
      FROM right_rows
    ),
    combined AS (
      SELECT
        left_rows.id,
        coalesce(derived.score, left_rows.score) AS score
      FROM left_rows
      LEFT JOIN derived
        ON left_rows.id = derived.id
       AND left_rows.segment = derived.segment
    )
    SELECT id, score
    FROM left_rows
    UNION ALL
    SELECT id, score
    FROM combined;
    """

    entrypoint = EngineEntrypoint(script, external_tables=["left_rows", "right_rows"])
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "left_rows.id.value = derived.id.value" in lowered_sql
    assert "left_rows.segment.value = derived.segment.value" in lowered_sql
    assert "COALESCE(derived.score.value, left_rows.score.value)" in lowered_sql


def test_lower_expr_sizes_variant_array_access():
    expression = sqlglot.parse_one("SELECT array_size(simulation_result['history'])", read="spark").expressions[0]

    lowered = lower_expr(SqlAstExpr(expression), variant_columns={"simulation_result"}).sql(dialect="spark")

    assert lowered == "SIZE(FROM_JSON(TO_JSON(TRY_VARIANT_GET(simulation_result, '$.history')), 'array<variant>'))"


def test_lower_statement_cells_sql_sizes_variant_array_access_from_cell_value():
    script = """
    CREATE BATCH TABLE extracted_trajectories
    SELECT array_size(simulation_result['history']) AS num_turns
    FROM all_simulations;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry, variant_columns={"simulation_result"})

    assert (
        "SIZE(FROM_JSON(TO_JSON(TRY_VARIANT_GET(simulation_result.value, '$.history')), 'array<variant>'))"
        in lowered_sql
    )
    assert "NAMED_STRUCT('cell_id', NULL, 'value', CASE WHEN SIZE(" in lowered_sql
    assert "simulation_result.metadata.errors" in lowered_sql
    assert "simulation_result.metadata.lineage" not in lowered_sql


def test_lower_statement_cells_sql_preserves_unaliased_cte_variant_access():
    script = """
    CREATE BATCH TABLE prepared
    WITH parsed AS (
      SELECT parse_json(payload_json) AS payload
      FROM raw_rows
    )
    SELECT CAST(payload['category'] AS STRING) AS category
    FROM parsed;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "TRY_VARIANT_GET(payload.value, '$.category')" in lowered_sql
    assert "payload.value['category']" not in lowered_sql


def test_lower_statement_cells_sql_sizes_map_variant_array_access_from_cell_value():
    script = """
    CREATE BATCH TABLE extracted_trajectories
    SELECT array_size(simulation_result['history']) AS num_turns
    FROM all_simulations;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert (
        "SIZE(FROM_JSON(TO_JSON(simulation_result.value['history']), 'array<variant>'))"
        in lowered_sql
    )


def test_lower_statement_cells_sql_string_concat_casts_cell_values():
    script = """
    CREATE BATCH TABLE policy_adherence_judged
    SELECT
      'Policy: ' || support_policy_text || ' | Steps: ' || required_policy_steps || ' | Intent: ' || customer_intent AS prompt
    FROM extracted_trajectories;
    """

    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))

    lowered_sql = lower_statement_cells_sql(batch_stmt, registry)

    assert "'Policy: ' || TRY_CAST(support_policy_text.value AS STRING)" in lowered_sql
    assert "' | Steps: ' || TRY_CAST(required_policy_steps.value AS STRING)" in lowered_sql
    assert "' | Intent: ' || TRY_CAST(customer_intent.value AS STRING)" in lowered_sql
    assert "AGENTCICD_CAST_ERROR" in lowered_sql
    assert "support_policy_text.metadata.errors" in lowered_sql
    assert "required_policy_steps.metadata.errors" in lowered_sql
    assert "customer_intent.metadata.errors" in lowered_sql
