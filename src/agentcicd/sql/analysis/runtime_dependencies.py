from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

import sqlglot
from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import CallExpr, ExprIR, KeywordArgExpr, LiteralExpr, SqlAstExpr
from agentcicd.sql.ir.statements import BatchTableStmt, DeclareInputStmt, QueryStmt, SqlFunctionDefStmt, StatementIR, StreamTableStmt
from agentcicd.sql.ir.visitors import walk_ir
from agentcicd.sql.surface.top_level_parser import TopLevelParser

_ENTITY_ID_PATTERN = re.compile(r"^(secret|fixture|image|aisystem)\.[A-Za-z0-9]+$")

_FIXTURE_FUNCTION_REQUIREMENTS: dict[str, int | None] = {}

_SECRET_KEYS = {"secret_id", "secret_ids"}
_AISYSTEM_KEYS = {"aisystem", "aisystem_id", "aisystem_ids"}
_FIXTURE_KEYS = {"fixture_id", "fixture_ids"}
_IMAGE_KEYS = {"image_id", "image_ids"}


@dataclass
class RuntimeSqlDependencies:
    secret_ids: set[str] = field(default_factory=set)
    aisystem_ids: set[str] = field(default_factory=set)
    fixture_ids: set[str] = field(default_factory=set)
    image_ids: set[str] = field(default_factory=set)


def extract_runtime_dependencies_from_sql(
    sql_text: str,
    macros: dict[str, str] | None = None,
) -> RuntimeSqlDependencies:
    dependencies = RuntimeSqlDependencies()
    if not sql_text.strip():
        return dependencies

    try:
        statements = TopLevelParser(sql_text).parse()
    except Exception:
        return dependencies

    for statement in statements:
        _collect_statement_dependencies(statement, macros=macros or {}, dependencies=dependencies)

    return dependencies


def _collect_statement_dependencies(
    statement: StatementIR,
    *,
    macros: dict[str, str],
    dependencies: RuntimeSqlDependencies,
) -> None:
    if isinstance(statement, SqlFunctionDefStmt) and statement.definition and statement.definition.sql_body:
        for assignment in statement.definition.sql_body.assignments:
            _collect_expression_dependencies(assignment.value, macros=macros, dependencies=dependencies)
        if statement.definition.sql_body.return_expr is not None:
            _collect_expression_dependencies(statement.definition.sql_body.return_expr.value, macros=macros, dependencies=dependencies)
        return

    if isinstance(statement, DeclareInputStmt):
        if statement.input_type.upper() == "AISYSTEM" and statement.default_sql:
            dependencies.aisystem_ids.update(
                value
                for value in _extract_string_values_from_sql_ast_string(statement.default_sql, macros=macros)
                if _ENTITY_ID_PATTERN.match(value) and value.startswith("aisystem.")
            )
        if statement.input_type.upper() == "SECRET" and statement.default_sql:
            dependencies.secret_ids.update(
                value
                for value in _extract_string_values_from_sql_ast_string(statement.default_sql, macros=macros)
                if _ENTITY_ID_PATTERN.match(value) and value.startswith("secret.")
            )
        return

    if isinstance(statement, (BatchTableStmt, StreamTableStmt, QueryStmt)) and statement.query is not None:
        _collect_expression_dependencies(statement.query, macros=macros, dependencies=dependencies)


def _extract_string_values_from_sql_ast_string(sql_text: str, *, macros: dict[str, str]) -> list[str]:
    try:
        expression = sqlglot.parse_one(sql_text, read="spark")
    except Exception:
        return []
    if expression is None:
        return []
    return _extract_string_values_from_sql_ast(expression, macros=macros)


def _collect_expression_dependencies(
    expression: ExprIR,
    *,
    macros: dict[str, str],
    dependencies: RuntimeSqlDependencies,
) -> None:
    function_calls = list(_iter_function_calls(expression))

    for call_name, call_args in function_calls:
        lowered = call_name.strip().lower()
        fixture_arg_index = _FIXTURE_FUNCTION_REQUIREMENTS.get(lowered)
        if fixture_arg_index is not None:
            if fixture_arg_index is None:
                continue
            if fixture_arg_index < len(call_args):
                for entity_id in _extract_entity_ids_from_ir(call_args[fixture_arg_index], macros=macros):
                    if entity_id.startswith("image."):
                        dependencies.image_ids.add(entity_id)
                    elif entity_id.startswith("fixture."):
                        dependencies.fixture_ids.add(entity_id)

        _extract_from_named_arguments(call_args, macros=macros, dependencies=dependencies)
        for arg in call_args:
            _extract_nested_map_dependencies(arg, macros=macros, dependencies=dependencies)


def _iter_function_calls(expression: ExprIR) -> Iterable[tuple[str, list[ExprIR]]]:
    yielded_sqlast_nodes: set[int] = set()

    def visit(node: object) -> None:
        if isinstance(node, CallExpr):
            calls.append((node.function_name, list(node.args)))
        elif isinstance(node, SqlAstExpr):
            for inner in node.expression.walk():
                if isinstance(inner, exp.Dot):
                    namespaced = _extract_namespaced_call(inner)
                    if namespaced is not None:
                        name, call_expression = namespaced
                        yielded_sqlast_nodes.add(id(call_expression))
                        calls.append((name, _sql_args_to_ir(list(call_expression.expressions or []))))
                        continue
                if isinstance(inner, exp.Func):
                    if id(inner) in yielded_sqlast_nodes:
                        continue
                    call_name = _function_call_name(inner)
                    if call_name:
                        calls.append((call_name, _sql_args_to_ir(list(inner.expressions or []))))

    calls: list[tuple[str, list[ExprIR]]] = []
    walk_ir(expression, visit)
    return calls


def _sql_args_to_ir(arguments: list[exp.Expression]) -> list[ExprIR]:
    converted: list[ExprIR] = []
    for argument in arguments:
        if isinstance(argument, exp.EQ):
            key = _named_argument_key_sql(argument.this)
            if key:
                converted.append(
                    KeywordArgExpr(
                        name=key,
                        value=_sql_expression_to_ir(argument.expression),
                    )
                )
                continue
        converted.append(_sql_expression_to_ir(argument))
    return converted


def _sql_expression_to_ir(expression: exp.Expression | None) -> ExprIR:
    if expression is None:
        return LiteralExpr(value=None)
    if isinstance(expression, exp.Literal):
        if expression.args.get("is_string"):
            return LiteralExpr(value=str(expression.this))
        text = str(expression.this)
        try:
            if "." in text:
                return LiteralExpr(value=float(text))
            return LiteralExpr(value=int(text))
        except ValueError:
            return LiteralExpr(value=text)
    return SqlAstExpr(expression=expression)


def _extract_from_named_arguments(
    call_args: list[ExprIR],
    *,
    macros: dict[str, str],
    dependencies: RuntimeSqlDependencies,
) -> None:
    for arg in call_args:
        if not isinstance(arg, KeywordArgExpr):
            continue
        key = arg.name.strip().lower()
        ids = _extract_entity_ids_from_ir(arg.value, macros=macros)
        if not ids:
            continue
        _assign_ids_by_key(key, ids, dependencies)


def _extract_nested_map_dependencies(
    expression: ExprIR,
    *,
    macros: dict[str, str],
    dependencies: RuntimeSqlDependencies,
) -> None:
    def visit(node: object) -> None:
        if not isinstance(node, SqlAstExpr):
            return
        for candidate in node.expression.walk():
            if isinstance(candidate, exp.VarMap):
                _extract_from_map_expression(candidate, macros=macros, dependencies=dependencies)
            elif isinstance(candidate, exp.Anonymous) and candidate.name.strip().lower() == "map":
                _extract_from_map_expression(candidate, macros=macros, dependencies=dependencies)

    walk_ir(expression, visit)


def _extract_from_map_expression(
    map_expr: exp.Expression,
    *,
    macros: dict[str, str],
    dependencies: RuntimeSqlDependencies,
) -> None:
    if isinstance(map_expr, exp.VarMap):
        raw_keys = map_expr.args.get("keys")
        raw_values = map_expr.args.get("values")
        keys = list(raw_keys.expressions or []) if isinstance(raw_keys, exp.Expression) else []
        values = list(raw_values.expressions or []) if isinstance(raw_values, exp.Expression) else []
        pairs = zip(keys, values)
    else:
        entries = list(map_expr.expressions or [])
        pairs = (
            (entries[index], entries[index + 1])
            for index in range(0, len(entries), 2)
            if index + 1 < len(entries)
        )

    for key_expr, value_expr in pairs:
        key = _literal_string_value_sql(key_expr)
        if not key:
            continue
        ids = _extract_entity_ids_from_ir(_sql_expression_to_ir(value_expr), macros=macros)
        if not ids:
            continue
        _assign_ids_by_key(key.strip().lower(), ids, dependencies)


def _assign_ids_by_key(key: str, ids: set[str], dependencies: RuntimeSqlDependencies) -> None:
    if key in _SECRET_KEYS:
        dependencies.secret_ids.update(entity_id for entity_id in ids if entity_id.startswith("secret."))
    elif key in _AISYSTEM_KEYS:
        dependencies.aisystem_ids.update(entity_id for entity_id in ids if entity_id.startswith("aisystem."))
    elif key in _FIXTURE_KEYS:
        dependencies.fixture_ids.update(entity_id for entity_id in ids if entity_id.startswith("fixture."))
    elif key in _IMAGE_KEYS:
        dependencies.image_ids.update(entity_id for entity_id in ids if entity_id.startswith("image."))


def _extract_entity_ids_from_ir(expression: ExprIR, *, macros: dict[str, str]) -> set[str]:
    values = _extract_string_values_from_ir(expression, macros=macros)
    return {
        value
        for value in values
        if isinstance(value, str) and _ENTITY_ID_PATTERN.match(value)
    }


def _extract_string_values_from_ir(expression: ExprIR, *, macros: dict[str, str]) -> list[str]:
    if isinstance(expression, LiteralExpr):
        value = expression.value
        if isinstance(value, str):
            if value.startswith("$"):
                resolved = macros.get(value[1:])
                return [resolved] if isinstance(resolved, str) and resolved else []
            return [value]
        return []

    values: list[str] = []
    if isinstance(expression, SqlAstExpr):
        values.extend(_extract_string_values_from_sql_ast(expression.expression, macros=macros))
    else:
        def visit(node: object) -> None:
            if node is expression:
                return
            if isinstance(node, LiteralExpr) and isinstance(node.value, str):
                if node.value.startswith("$"):
                    resolved = macros.get(node.value[1:])
                    if isinstance(resolved, str) and resolved:
                        values.append(resolved)
                else:
                    values.append(node.value)
            elif isinstance(node, SqlAstExpr):
                values.extend(_extract_string_values_from_sql_ast(node.expression, macros=macros))

        walk_ir(expression, visit)
    return values


def _extract_string_values_from_sql_ast(expression: exp.Expression, *, macros: dict[str, str]) -> list[str]:
    values: list[str] = []
    if isinstance(expression, exp.Array):
        for item in expression.expressions or []:
            values.extend(_extract_string_values_from_sql_ast(item, macros=macros))
        return values
    if isinstance(expression, exp.Anonymous) and expression.name.strip().lower() == "array":
        for item in expression.expressions or []:
            values.extend(_extract_string_values_from_sql_ast(item, macros=macros))
        return values
    literal = _literal_string_value_sql(expression)
    if literal is not None:
        if literal.startswith("$"):
            resolved = macros.get(literal[1:])
            if isinstance(resolved, str) and resolved:
                return [resolved]
            return []
        return [literal]
    return values


def _literal_string_value_sql(expression: exp.Expression | None) -> str | None:
    if expression is None:
        return None
    if isinstance(expression, exp.Literal) and expression.args.get("is_string"):
        return str(expression.this)
    return None


def _named_argument_key_sql(expression: exp.Expression | None) -> str | None:
    if expression is None:
        return None
    if isinstance(expression, exp.Column):
        identifier = expression.this
        if isinstance(identifier, exp.Identifier) and identifier.this:
            return str(identifier.this).strip()
    if isinstance(expression, exp.Identifier) and expression.this:
        return str(expression.this).strip()
    return None


def _function_call_name(call: exp.Func) -> str | None:
    if isinstance(call, exp.Anonymous):
        name = call.name
        if isinstance(name, str) and name.strip():
            return name.strip()
    try:
        sql_name = call.sql_name()
        if isinstance(sql_name, str) and sql_name.strip():
            return sql_name.strip()
    except Exception:
        pass
    this_expr = call.this
    if isinstance(this_expr, exp.Identifier) and this_expr.this:
        return str(this_expr.this).strip()
    if isinstance(this_expr, str) and this_expr.strip():
        return this_expr.strip()
    return None


def _extract_namespaced_call(expression: exp.Expression) -> tuple[str, exp.Anonymous] | None:
    if not isinstance(expression, exp.Dot) or not isinstance(expression.expression, exp.Anonymous):
        return None
    namespace_parts = _dot_namespace_parts(expression.this)
    if not namespace_parts:
        return None
    return ".".join([*namespace_parts, expression.expression.name]), expression.expression


def _dot_namespace_parts(expression: exp.Expression) -> list[str]:
    if isinstance(expression, exp.Identifier):
        return [expression.this]
    if isinstance(expression, exp.Dot) and isinstance(expression.expression, exp.Identifier):
        left = _dot_namespace_parts(expression.this)
        if not left:
            return []
        return [*left, expression.expression.this]
    return []
