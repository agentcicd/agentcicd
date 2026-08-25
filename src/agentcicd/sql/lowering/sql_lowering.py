from __future__ import annotations

from typing import Dict, Optional, Set

import sqlglot
from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import CallExpr, ColumnRefExpr, ExprIR, KeywordArgExpr, LiteralExpr, SqlAstExpr, VariantPathExpr
from agentcicd.sql.json_semantics import (
    function_name,
    is_variant_expression,
    json_path_from_index_expression,
    lower_bracket_json_access,
    lower_dynamic_variant_object_access,
    lower_json_access,
    lower_parse_json,
    lower_tolerant_get_access,
    lower_variant_array_for_collection_size,
)
from agentcicd.sql.lowering.sql_function_lowering import lower_sql_function_call
from agentcicd.sql.lowering.variant_lowering import lower_variant_path
from agentcicd.sql.runtime.controls import _pool_fixture_id
from agentcicd.sql.semantics.arg_binding import bind_function_arguments
from agentcicd.sql.semantics.registry import FunctionRegistry
from agentcicd.sql.semantics.types import is_function_type
from agentcicd.sql.surface.sqlglot_bridge import _extract_namespaced_call, expression_to_ir


def lower_expr(
    expression: ExprIR,
    registry: Optional[FunctionRegistry] = None,
    scope: Optional[Dict[str, exp.Expression]] = None,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    scope = scope or {}
    if isinstance(expression, SqlAstExpr):
        return expression.expression.copy().transform(
            lambda node: _lower_ast_node(node, registry=registry, scope=scope, variant_columns=variant_columns),
            copy=True,
        )
    if isinstance(expression, ColumnRefExpr):
        lowered = scope.get(expression.name.lower())
        if lowered is not None:
            return lowered.copy()
        column = _parse_scalar(expression.name)
        if variant_columns is not None and expression.name.lower() in variant_columns:
            column.meta["agentcicd_variant_access"] = True
        return column
    if isinstance(expression, LiteralExpr):
        return _literal_to_sql(expression.value)
    if isinstance(expression, VariantPathExpr):
        return lower_variant_path(
            lower_expr(expression.base, registry=registry, scope=scope, variant_columns=variant_columns),
            expression.path,
            variant_columns=variant_columns,
        )
    if isinstance(expression, CallExpr):
        if registry is not None:
            definition = registry.resolve(expression.function_name)
            if definition is not None and definition.kind == "sql" and definition.sql_body is not None:
                bound_arguments = {
                    item.parameter.name.lower(): lower_expr(
                        item.value,
                        registry=registry,
                        scope=scope,
                        variant_columns=variant_columns,
                    )
                    for item in bind_function_arguments(expression, definition.parameters)
                }
                return lower_sql_function_call(
                    definition,
                    bound_arguments,
                    lambda expr, local_scope: lower_expr(
                        expr,
                        registry=registry,
                        scope=local_scope,
                        variant_columns=variant_columns,
                    ),
                )
            if definition is not None:
                bound_arguments = bind_function_arguments(expression, definition.parameters)
                lowered_call = exp.Anonymous(
                    this=definition.runtime_alias,
                    expressions=[
                        _lower_bound_argument_value(
                            argument,
                            definition=definition,
                            registry=registry,
                            scope=scope,
                            variant_columns=variant_columns,
                        )
                        for argument in bound_arguments
                    ],
                )
                if _definition_returns_json(definition):
                    lowered_call.meta["agentcicd_variant_access"] = True
                return lowered_call
            function_name = expression.function_name
        else:
            function_name = expression.function_name
        lowered_args = []
        for argument in expression.args:
            if isinstance(argument, KeywordArgExpr):
                lowered_args.append(
                    exp.EQ(
                        this=exp.column(argument.name),
                        expression=lower_expr(
                            argument.value,
                            registry=registry,
                            scope=scope,
                            variant_columns=variant_columns,
                        ),
                    )
                )
            else:
                lowered_args.append(lower_expr(argument, registry=registry, scope=scope, variant_columns=variant_columns))
        if function_name.lower() == "parse_json":
            base = lowered_args[0] if lowered_args else exp.Null()
            return lower_parse_json(base, variant_columns=variant_columns)
        if function_name.lower() == "get":
            lowered_get = exp.Anonymous(this="GET", expressions=[arg.copy() for arg in lowered_args])
            tolerant_get = lower_tolerant_get_access(lowered_get, variant_columns=variant_columns)
            return tolerant_get if tolerant_get is not None else lowered_get
        return exp.Anonymous(this=function_name, expressions=lowered_args)
    raise TypeError(f"Unsupported IR expression type: {type(expression).__name__}")


def _parse_scalar(sql_text: str) -> exp.Expression:
    parsed = sqlglot.parse_one(f"SELECT {sql_text}", read="spark")
    return parsed.expressions[0]


def _literal_to_sql(value) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return exp.Literal.number(str(value))
    return exp.Literal.string(str(value))


def _lower_ast_node(
    node: exp.Expression,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, exp.Expression],
    variant_columns: Optional[Set[str]],
) -> exp.Expression:
    if isinstance(node, exp.ArraySize) and isinstance(node.this, exp.Expression):
        lowered_arg = lower_expr(
            SqlAstExpr(node.this),
            registry=registry,
            scope=scope,
            variant_columns=variant_columns,
        )
        variant_array_arg = lower_variant_array_for_collection_size(
            lowered_arg,
            variant_columns=variant_columns,
            force=isinstance(lowered_arg, exp.Bracket),
        )
        if variant_array_arg is not None:
            clone = node.copy()
            clone.set("this", variant_array_arg)
            return clone
    if isinstance(node, exp.DPipe):
        parts = [
            lower_expr(
                SqlAstExpr(part),
                registry=registry,
                scope=scope,
                variant_columns=variant_columns,
            )
            for part in _flatten_dpipe(node)
        ]
        return _build_string_dpipe(parts)
    if isinstance(node, exp.ParseJSON):
        if isinstance(node.this, exp.Expression):
            base = lower_expr(
                SqlAstExpr(node.this),
                registry=registry,
                scope=scope,
                variant_columns=variant_columns,
            )
        else:
            base = exp.Null()
        return lower_parse_json(base, variant_columns=variant_columns)
    if isinstance(node, exp.Bracket):
        lowered = _lower_bracket_json_access(
            node,
            registry=registry,
            scope=scope,
            variant_columns=variant_columns,
        )
        if lowered is not None:
            return lowered
        dynamic_lowered = _lower_dynamic_bracket_json_access(
            node,
            registry=registry,
            scope=scope,
            variant_columns=variant_columns,
        )
        if dynamic_lowered is not None:
            return dynamic_lowered
    if isinstance(node, exp.Column):
        lowered = scope.get(node.sql(dialect="spark").lower())
        if lowered is not None:
            return lowered.copy()
        if variant_columns is not None and node.sql(dialect="spark").lower() in variant_columns:
            column = node.copy()
            column.meta["agentcicd_variant_access"] = True
            return column
    if isinstance(node, exp.Anonymous):
        if function_name(node) == "get":
            args = list(node.expressions or [])
            lowered_args = [
                lower_expr(
                    SqlAstExpr(arg),
                    registry=registry,
                    scope=scope,
                    variant_columns=variant_columns,
                )
                if isinstance(arg, exp.Expression)
                else arg
                for arg in args
            ]
            lowered_get = exp.Anonymous(this="GET", expressions=lowered_args)
            tolerant_get = lower_tolerant_get_access(lowered_get, variant_columns=variant_columns)
            return tolerant_get if tolerant_get is not None else lowered_get
        return lower_expr(expression_to_ir(node), registry=registry, scope=scope, variant_columns=variant_columns)
    if _extract_namespaced_call(node) is not None:
        return lower_expr(expression_to_ir(node), registry=registry, scope=scope, variant_columns=variant_columns)
    return node


def _lower_bracket_json_access(
    expression: exp.Bracket,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, exp.Expression],
    variant_columns: Optional[Set[str]],
) -> Optional[exp.Expression]:
    path_parts: list[str] = []
    current: exp.Expression = expression
    while isinstance(current, exp.Bracket):
        indexes = list(current.expressions or [])
        if len(indexes) != 1:
            return lower_bracket_json_access(expression, variant_columns=variant_columns)
        path_part = json_path_from_index_expression(indexes[0])
        if path_part is None:
            return lower_bracket_json_access(expression, variant_columns=variant_columns)
        path_parts.insert(0, path_part)
        if not isinstance(current.this, exp.Expression):
            return None
        current = current.this

    lowered_base = lower_expr(
        SqlAstExpr(current),
        registry=registry,
        scope=scope,
        variant_columns=variant_columns,
    )
    if not path_parts or not is_variant_expression(lowered_base, variant_columns=variant_columns):
        return None
    return lower_json_access(lowered_base, "$" + "".join(path_parts), variant_columns=variant_columns)


def _lower_dynamic_bracket_json_access(
    expression: exp.Bracket,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, exp.Expression],
    variant_columns: Optional[Set[str]],
) -> Optional[exp.Expression]:
    indexes = list(expression.expressions or [])
    if len(indexes) != 1 or not isinstance(expression.this, exp.Expression):
        return None
    lowered_base = lower_expr(
        SqlAstExpr(expression.this),
        registry=registry,
        scope=scope,
        variant_columns=variant_columns,
    )
    lowered_index = lower_expr(
        SqlAstExpr(indexes[0]),
        registry=registry,
        scope=scope,
        variant_columns=variant_columns,
    )
    rewritten = expression.copy()
    rewritten.set("this", lowered_base)
    rewritten.set("expressions", [lowered_index])
    return lower_dynamic_variant_object_access(rewritten, variant_columns=variant_columns)


def _lower_bound_argument_value(
    argument,
    *,
    definition=None,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, exp.Expression],
    variant_columns: Optional[Set[str]],
) -> exp.Expression:
    parameter_type = argument.parameter.type_sql.strip().upper()
    if parameter_type == "RATELIMIT":
        if not argument.supplied or _is_null_expr(argument.value):
            return exp.Null()
        return _lower_ratelimit_argument(argument.value, registry=registry, scope=scope, variant_columns=variant_columns)
    if parameter_type == "POOL":
        if not argument.supplied or _is_null_expr(argument.value):
            return exp.Null()
        return _lower_pool_argument(
            argument.value,
            registry=registry,
            scope=scope,
            variant_columns=variant_columns,
            fixture_id=_pool_fixture_id(definition) if definition is not None else "",
        )
    if is_function_type(argument.parameter.type_sql):
        value = argument.value
        if isinstance(value, ColumnRefExpr):
            return exp.Literal.string(value.name)
        raise ValueError(
            f"Function argument '{argument.parameter.name}' must lower from a function reference"
        )
    return lower_expr(argument.value, registry=registry, scope=scope, variant_columns=variant_columns)


def _is_null_expr(value: ExprIR) -> bool:
    if isinstance(value, LiteralExpr):
        return value.value is None
    if isinstance(value, SqlAstExpr):
        return isinstance(value.expression, exp.Null)
    return False


def _lower_ratelimit_argument(
    value: ExprIR,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, exp.Expression],
    variant_columns: Optional[Set[str]],
) -> exp.Expression:
    if not isinstance(value, ColumnRefExpr):
        raise ValueError("RATELIMIT arguments must lower from declared input references")
    return exp.Anonymous(
        this="NAMED_STRUCT",
        expressions=[
            exp.Literal.string("key"),
            exp.Literal.string(value.name),
            exp.Literal.string("max_in_flight"),
            lower_expr(value, registry=registry, scope=scope, variant_columns=variant_columns),
        ],
    )


def _lower_pool_argument(
    value: ExprIR,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, exp.Expression],
    variant_columns: Optional[Set[str]],
    fixture_id: str = "",
) -> exp.Expression:
    if not isinstance(value, ColumnRefExpr):
        raise ValueError("POOL arguments must lower from declared input references")
    expressions = [
        exp.Literal.string("key"),
        exp.Literal.string(value.name),
        exp.Literal.string("config_json"),
        lower_expr(value, registry=registry, scope=scope, variant_columns=variant_columns),
    ]
    if fixture_id:
        expressions.extend([exp.Literal.string("fixture_id"), exp.Literal.string(fixture_id)])
    return exp.Anonymous(
        this="NAMED_STRUCT",
        expressions=expressions,
    )


def _definition_returns_json(definition) -> bool:
    raw_value = definition.metadata.get("returns_json")
    if isinstance(raw_value, bool):
        return raw_value
    output_type = str(definition.metadata.get("output_type") or "").strip().lower()
    if output_type in {"json", "variant"}:
        return True
    output_schema = definition.metadata.get("output_schema")
    if isinstance(output_schema, dict):
        return str(output_schema.get("type") or "").strip().lower() in {"json", "variant"}
    return False


def _flatten_dpipe(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.DPipe):
        parts: list[exp.Expression] = []
        if isinstance(expression.this, exp.Expression):
            parts.extend(_flatten_dpipe(expression.this))
        if isinstance(expression.expression, exp.Expression):
            parts.extend(_flatten_dpipe(expression.expression))
        return parts
    return [expression]


def _build_string_dpipe(parts: list[exp.Expression]) -> exp.Expression:
    if not parts:
        return exp.Literal.string("")
    expression = _cast_for_string_concat(parts[0])
    for part in parts[1:]:
        expression = exp.DPipe(
            this=expression,
            expression=_cast_for_string_concat(part),
            safe=True,
        )
    return expression


def _cast_for_string_concat(expression: exp.Expression) -> exp.Expression:
    if isinstance(expression, exp.Literal) and expression.is_string:
        return expression.copy()
    if _is_string_cast(expression):
        return expression.copy()
    return exp.Cast(this=expression.copy(), to=exp.DataType.build("STRING"))


def _is_string_cast(expression: exp.Expression) -> bool:
    if not isinstance(expression, exp.Cast):
        return False
    target = expression.args.get("to")
    return isinstance(target, exp.DataType) and target.sql(dialect="spark").upper() == "STRING"
