from __future__ import annotations

from sqlglot import parse_one
from sqlglot import expressions as exp

from agentcicd.sql.parsing.function_args import (
    is_keyword_argument_target,
    keyword_argument_name,
)


def test_keyword_argument_name_accepts_identifier_targets():
    expr = parse_one("SELECT helpfulness_judge(question=q) FROM prepared", read="spark")
    arg = next(node for node in expr.find_all(exp.EQ))

    assert keyword_argument_name(arg) == "question"


def test_is_keyword_argument_target_distinguishes_function_kwargs_from_equality():
    expr = parse_one(
        "SELECT helpfulness_judge(question=q), n = 10 AS matches FROM prepared",
        read="spark",
    )
    equals = list(expr.find_all(exp.EQ))
    kwarg_target = equals[0].this
    equality_left = equals[1].this

    assert is_keyword_argument_target(kwarg_target) is True
    assert is_keyword_argument_target(equality_left) is False


def test_keyword_argument_name_rejects_table_qualified_targets():
    expr = parse_one("SELECT helpfulness_judge(prepared.question=q) FROM prepared", read="spark")
    arg = next(node for node in expr.find_all(exp.EQ))

    try:
        keyword_argument_name(arg)
    except ValueError as exc:
        assert "table-qualified" in str(exc)
    else:
        raise AssertionError("Expected keyword_argument_name to reject table-qualified targets")
