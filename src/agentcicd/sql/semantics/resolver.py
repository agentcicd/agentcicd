from __future__ import annotations

from dataclasses import replace
from typing import List

from agentcicd.sql.ir.expressions import AssignmentExpr, CallExpr, ExprIR, KeywordArgExpr, ReturnExpr, SqlAstExpr
from agentcicd.sql.ir.functions import FunctionDefinitionIR, SqlFunctionBodyIR
from agentcicd.sql.ir.statements import SqlFunctionDefStmt, StatementIR
from agentcicd.sql.ir.visitors import walk_ir
from agentcicd.sql.semantics.arg_binding import bind_function_arguments
from agentcicd.sql.semantics.registry import FunctionRegistry


def resolve_script(statements: List[StatementIR], registry: FunctionRegistry) -> List[StatementIR]:
    resolved: list[StatementIR] = []
    for statement in statements:
        if isinstance(statement, SqlFunctionDefStmt) and statement.definition is not None:
            resolved.append(
                replace(statement, definition=_resolve_function_definition(statement.definition, registry))
            )
            continue
        resolved.append(statement)
    return resolved


def _resolve_function_definition(
    definition: FunctionDefinitionIR,
    registry: FunctionRegistry,
) -> FunctionDefinitionIR:
    if definition.sql_body is None:
        return definition
    assignments = [
        AssignmentExpr(name=assignment.name, value=_resolve_expr(assignment.value, registry))
        for assignment in definition.sql_body.assignments
    ]
    return_expr = (
        ReturnExpr(value=_resolve_expr(definition.sql_body.return_expr.value, registry))
        if definition.sql_body.return_expr is not None
        else None
    )
    return replace(definition, sql_body=SqlFunctionBodyIR(assignments=assignments, return_expr=return_expr))


def _resolve_expr(expression: ExprIR, registry: FunctionRegistry) -> ExprIR:
    if isinstance(expression, CallExpr):
        definition = registry.resolve(expression.function_name)
        if definition is None:
            return expression
        bound_args = bind_function_arguments(expression, definition.parameters)
        return CallExpr(
            function_name=definition.canonical_name,
            args=[argument.value for argument in bound_args],
        )
    return expression
