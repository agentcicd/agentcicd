from __future__ import annotations

from typing import Dict

from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import AssignmentExpr, ExprIR, ReturnExpr
from agentcicd.sql.ir.functions import FunctionDefinitionIR


def lower_sql_function_call(
    definition: FunctionDefinitionIR,
    bound_arguments: Dict[str, exp.Expression],
    lower_expr_fn,
) -> exp.Expression:
    if definition.sql_body is None or definition.sql_body.return_expr is None:
        raise ValueError(f"SQL function '{definition.canonical_name}' is missing a body")

    scope = dict(bound_arguments)
    for assignment in definition.sql_body.assignments:
        scope[assignment.name.lower()] = lower_expr_fn(assignment.value, scope)
    return lower_expr_fn(definition.sql_body.return_expr.value, scope)
