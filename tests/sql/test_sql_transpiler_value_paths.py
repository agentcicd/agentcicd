from sqlglot import parse_one

from agentcicd.sql.parsing.sql_transpiler import transpile_query_expression_with_options


def test_explicit_value_path_uses_root_cell_for_error_metadata():
    expr = parse_one(
        """
        SELECT
            CASE
                WHEN evaluation_criteria.value.actions IS NULL THEN CAST(array() AS ARRAY<STRUCT<action_id:STRING>>)
                ELSE transform(
                    evaluation_criteria.value.actions,
                    a -> named_struct('action_id', cast(a.action_id AS STRING))
                )
            END AS actions
        FROM t
        """,
        read="spark",
    )

    out = transpile_query_expression_with_options(expr).sql(dialect="spark")

    assert "evaluation_criteria.metadata.error" in out
    assert "evaluation_criteria.value.actions.metadata.error" not in out
    assert "TRANSFORM(evaluation_criteria.value.actions" in out


def test_explicit_value_path_not_double_unwrapped():
    expr = parse_one("SELECT CAST(id.value AS STRING) AS x FROM t", read="spark")

    out = transpile_query_expression_with_options(expr).sql(dialect="spark")

    assert "id.metadata.error" in out
    assert "id.value.metadata.error" not in out
    assert "CAST(id.value AS STRING)" in out
    assert "id.value.value" not in out
