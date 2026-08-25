from __future__ import annotations

from sqlglot import parse_one

from agentcicd.sql.parsing.sql_transpiler import transpile_query_expression_with_options


def test_transpiler_rewrites_case_predicates_with_boolean_ops_and_in():
    expr = parse_one(
        """
        SELECT
          CASE
            WHEN (n > 10 AND flag = true) OR kind IN ('a', 'b') THEN 1
            ELSE 0
          END AS score
        FROM prepared
        """,
        read="spark",
    )

    out = transpile_query_expression_with_options(expr).sql(dialect="spark")

    assert "n.value > 10" in out
    assert "flag.value = TRUE" in out or "flag = TRUE" in out
    assert "kind.value IN ('a', 'b')" in out


def test_transpiler_rewrites_between_and_qualified_join_predicates():
    expr = parse_one(
        """
        SELECT l.id, r.grp
        FROM left_raw l
        JOIN right_raw r
          ON l.id = r.id AND l.n BETWEEN 1 AND 10
        """,
        read="spark",
    )

    out = transpile_query_expression_with_options(expr).sql(dialect="spark")

    assert "l.id.value = r.id.value" in out
    assert "l.n.value BETWEEN 1 AND 10" in out
    assert "ASSERT_TRUE(" in out
