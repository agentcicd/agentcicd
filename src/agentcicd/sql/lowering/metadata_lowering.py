from __future__ import annotations

from functools import lru_cache
from typing import Dict, Optional, Set

from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import CallExpr, ColumnRefExpr, ExprIR, KeywordArgExpr, LiteralExpr, SqlAstExpr, VariantPathExpr
from agentcicd.sql.ir.metadata import CellComponentsIR
from agentcicd.sql.json_semantics import (
    function_name,
    lower_bracket_json_access,
    lower_dynamic_variant_object_access,
    lower_parse_json,
    lower_safe_array_access,
    lower_tolerant_get_access,
    lower_variant_array_for_collection_size,
)
from agentcicd.sql.engine.runtime_aliases import wrapped_runtime_alias
from agentcicd.sql.engine.cell_metadata import ERROR_ARRAY_SQL_TYPE, FIXTURE_TRACE_SQL_TYPE
from agentcicd.sql.lowering.sql_function_lowering import lower_sql_function_call
from agentcicd.sql.lowering.variant_lowering import lower_variant_path
from agentcicd.sql.runtime.controls import _pool_fixture_id
from agentcicd.sql.semantics.arg_binding import bind_function_arguments
from agentcicd.sql.semantics.registry import FunctionRegistry
from agentcicd.sql.surface.sqlglot_bridge import _extract_namespaced_call, expression_to_ir


def lower_expr_to_cell(
    expression: ExprIR,
    registry: Optional[FunctionRegistry] = None,
    scope: Optional[Dict[str, CellComponentsIR]] = None,
    *,
    assume_cell_columns: bool = False,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
    source_table_name: Optional[str] = None,
    source_relation_names: Optional[Set[str]] = None,
) -> CellComponentsIR:
    scope = scope or {}
    non_cell_names = non_cell_columns or set()

    if isinstance(expression, SqlAstExpr):
        return lower_sql_expression_to_cell(
            expression.expression,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )

    if isinstance(expression, ColumnRefExpr):
        lowered = scope.get(expression.name.lower())
        if lowered is not None:
            value_sql = lowered.value_sql.copy()
            if lowered.value_sql.meta.get("agentcicd_variant_access") or lowered.representation == "variant":
                value_sql.meta["agentcicd_variant_access"] = True
            return CellComponentsIR(
                value_sql=value_sql,
                error_sql=_copy_or_null(lowered.error_sql),
                latency_sql=_copy_or_scalar_null(lowered.latency_sql),
                cell_sql=_copy_or_none(lowered.cell_sql),
                representation=lowered.representation,
            )
        if "." in expression.name and assume_cell_columns:
            parts = [part for part in expression.name.split(".") if part]
            source_names = {name.lower() for name in source_relation_names or set()}
            if len(parts) > 1 and parts[0].lower() not in source_names and expression.name.lower() not in non_cell_names:
                parsed_column = _parse_scalar(expression.name)
                if isinstance(parsed_column, exp.Column):
                    return lower_sql_expression_to_cell(
                        parsed_column,
                        registry=registry,
                        scope=scope,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                        source_table_name=source_table_name,
                        source_relation_names=source_relation_names,
                    )
        if expression.name.lower() in non_cell_names:
            return CellComponentsIR(
                value_sql=_parse_scalar(expression.name),
                error_sql=_empty_errors(),
                representation="raw",
            )
        if assume_cell_columns:
            value_sql = _parse_scalar(f"{expression.name}.value")
            if variant_columns is not None and expression.name.lower() in variant_columns:
                value_sql.meta["agentcicd_variant_access"] = True
            return CellComponentsIR(
                value_sql=value_sql,
                error_sql=_parse_scalar(f"{expression.name}.metadata.errors"),
                latency_sql=_parse_scalar(f"{expression.name}.metadata.latency_ms"),
                cell_sql=_parse_scalar(expression.name),
                representation="cell",
            )
        column_sql = _parse_scalar(expression.name)
        return CellComponentsIR(
            value_sql=column_sql,
            error_sql=_empty_errors(),
            representation="raw",
        )

    if isinstance(expression, LiteralExpr):
        from agentcicd.sql.lowering.sql_lowering import lower_expr

        return CellComponentsIR(
            value_sql=lower_expr(expression, registry=registry),
            error_sql=_empty_errors(),
            representation="raw",
        )

    if isinstance(expression, VariantPathExpr):
        base = lower_expr_to_cell(
            expression.base,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        return _json_access_cell(
            base,
            lower_variant_path(
                base.value_sql.copy(),
                expression.path,
                variant_columns=variant_columns,
            ),
        )

    if isinstance(expression, CallExpr):
        function_name = expression.function_name
        argument_cells = []
        for argument in expression.args:
            if isinstance(argument, KeywordArgExpr):
                argument_cells.append(
                    (
                        argument.name,
                        lower_expr_to_cell(
                            argument.value,
                            registry=registry,
                            scope=scope,
                            assume_cell_columns=assume_cell_columns,
                            variant_columns=variant_columns,
                            non_cell_columns=non_cell_names,
                            source_table_name=source_table_name,
                            source_relation_names=source_relation_names,
                        ),
                    )
                )
            else:
                argument_cells.append(
                    (
                        None,
                        lower_expr_to_cell(
                            argument,
                            registry=registry,
                            scope=scope,
                            assume_cell_columns=assume_cell_columns,
                            variant_columns=variant_columns,
                            non_cell_columns=non_cell_names,
                            source_table_name=source_table_name,
                            source_relation_names=source_relation_names,
                        ),
                    )
                )

        if function_name.lower() == "is_err":
            if len(argument_cells) != 1 or argument_cells[0][0] is not None:
                raise ValueError("is_err expects exactly one positional argument")
            target = argument_cells[0][1]
            return CellComponentsIR(
                value_sql=exp.GT(
                    this=exp.Anonymous(this="SIZE", expressions=[_copy_or_empty_errors(target.error_sql)]),
                    expression=exp.Literal.number("0"),
                ),
                error_sql=_empty_errors(),
                representation="raw",
            )

        if function_name.lower() == "err_or":
            if len(argument_cells) != 2 or any(argument_name is not None for argument_name, _ in argument_cells):
                raise ValueError("err_or expects exactly two positional arguments")
            target = argument_cells[0][1]
            fallback = argument_cells[1][1]
            target_errors = _copy_or_empty_errors(target.error_sql)
            return CellComponentsIR(
                value_sql=exp.Case(
                    ifs=[
                        exp.If(
                            this=exp.GT(
                                this=exp.Anonymous(this="SIZE", expressions=[target_errors.copy()]),
                                expression=exp.Literal.number("0"),
                            ),
                            true=fallback.value_sql.copy(),
                        )
                    ],
                    default=target.value_sql.copy(),
                ),
                error_sql=_empty_errors(),
                representation="raw",
            )

        if function_name.lower() == "latency":
            if len(argument_cells) != 1 or argument_cells[0][0] is not None:
                raise ValueError("latency expects exactly one positional argument")
            target = argument_cells[0][1]
            return CellComponentsIR(
                value_sql=_copy_or_scalar_null(target.latency_sql),
                error_sql=_empty_errors(),
                representation="raw",
            )

        if registry is not None:
            definition = registry.resolve(expression.function_name)
            if definition is not None and definition.kind == "sql" and definition.sql_body is not None:
                bound_arguments = {
                    item.parameter.name.lower(): lower_expr_to_cell(
                        item.value,
                        registry=registry,
                        scope=scope,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                        source_table_name=source_table_name,
                        source_relation_names=source_relation_names,
                    )
                    for item in bind_function_arguments(expression, definition.parameters)
                }
                from agentcicd.sql.lowering.sql_lowering import lower_expr

                value_sql = lower_sql_function_call(
                    definition,
                    {name: cell.value_sql for name, cell in bound_arguments.items()},
                    lambda expr, local_scope: lower_expr(
                        expr,
                        registry=registry,
                        scope=local_scope,
                        variant_columns=variant_columns,
                    ),
                )
                return CellComponentsIR(
                    value_sql=value_sql,
                    error_sql=_merge_errors([cell for _, cell in argument_cells]),
                )
            if definition is not None:
                bound_items = bind_function_arguments(expression, definition.parameters)

                def _lower_bound_item(item) -> CellComponentsIR:
                    parameter_type = item.parameter.type_sql.strip().upper()
                    if parameter_type == "RATELIMIT":
                        if not item.supplied or _is_null_expr(item.value):
                            return _null_cell()
                        return _ratelimit_argument_cell(item.value, registry=registry, scope=scope)
                    if parameter_type == "POOL":
                        if not item.supplied or _is_null_expr(item.value):
                            return _null_cell()
                        return _pool_argument_cell(
                            item.value,
                            registry=registry,
                            scope=scope,
                            fixture_id=_pool_fixture_id(definition),
                        )
                    return lower_expr_to_cell(
                        item.value,
                        registry=registry,
                        scope=scope,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                        source_table_name=source_table_name,
                        source_relation_names=source_relation_names,
                    )

                bound_cell_map = {
                    item.parameter.name.lower(): _lower_bound_item(item)
                    for item in bound_items
                }
                cell_sql = exp.Anonymous(
                    this=wrapped_runtime_alias(definition.runtime_alias),
                    expressions=[
                        build_cell_struct(bound_cell_map[item.parameter.name.lower()])
                        for item in bound_items
                    ],
                )
                value_sql = _cell_field(cell_sql, "value")
                if _definition_returns_json(definition):
                    value_sql.meta["agentcicd_variant_access"] = True
                return CellComponentsIR(
                    value_sql=value_sql,
                    error_sql=_cell_metadata_field(cell_sql, "errors"),
                    latency_sql=_cell_metadata_field(cell_sql, "latency_ms"),
                    cell_sql=cell_sql,
                    representation="cell",
                )

        lowered_args = []
        for argument_name, argument_cell in argument_cells:
            if argument_name is not None:
                lowered_args.append(exp.EQ(this=exp.column(argument_name), expression=argument_cell.value_sql.copy()))
            else:
                lowered_args.append(argument_cell.value_sql.copy())
        if function_name.lower() == "parse_json":
            if argument_cells:
                return _safe_parse_json_cell(argument_cells[0][1])
            value_sql = lower_parse_json(exp.Null(), variant_columns=variant_columns)
        elif function_name.lower() == "try_variant_get" and argument_cells:
            value_sql = exp.Anonymous(
                this=function_name,
                expressions=[
                    _safe_parse_json_value(argument_cells[0][1]),
                    *[cell.value_sql.copy() for _, cell in argument_cells[1:]],
                ],
            )
            value_sql.meta["agentcicd_variant_access"] = True
            return _json_access_cell(argument_cells[0][1], value_sql)
        else:
            value_sql = exp.Anonymous(this=function_name, expressions=lowered_args)
        if function_name.lower() == "get":
            tolerant_get = lower_tolerant_get_access(value_sql, variant_columns=variant_columns)
            if tolerant_get is not None:
                return _tolerant_json_access_cell([cell for _, cell in argument_cells], tolerant_get)
        return CellComponentsIR(
            value_sql=value_sql,
            error_sql=_merge_errors([cell for _, cell in argument_cells]),
        )

    raise TypeError(f"Unsupported IR expression type: {type(expression).__name__}")


def lower_expr_to_cell_values_only(
    expression: ExprIR,
    *,
    registry: Optional[FunctionRegistry],
    scope_cells: Dict[str, CellComponentsIR],
    assume_cell_columns: bool = False,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    cell = lower_expr_to_cell(
        expression,
        registry=registry,
        scope=scope_cells,
        assume_cell_columns=assume_cell_columns,
        variant_columns=variant_columns,
        non_cell_columns=non_cell_columns,
    )
    return cell.value_sql


def _is_null_expr(value: ExprIR) -> bool:
    if isinstance(value, LiteralExpr):
        return value.value is None
    if isinstance(value, SqlAstExpr):
        return isinstance(value.expression, exp.Null)
    return False


def _null_cell() -> CellComponentsIR:
    return CellComponentsIR(
        value_sql=exp.Null(),
        error_sql=_empty_errors(),
        representation="raw",
    )


def build_cell_struct(cell: CellComponentsIR) -> exp.Expression:
    if cell.representation == "cell" and cell.cell_sql is not None:
        return cell.cell_sql.copy()
    errors_sql = _coalesce_or_empty_errors(cell.error_sql, copy=False)
    value_sql = _value_null_when_errors(cell.value_sql, errors_sql)
    return exp.Anonymous(
        this="NAMED_STRUCT",
        expressions=[
            exp.Literal.string("cell_id"),
            exp.Null(),
            exp.Literal.string("value"),
            value_sql,
            exp.Literal.string("metadata"),
            exp.Anonymous(
                this="NAMED_STRUCT",
                expressions=[
                    exp.Literal.string("errors"),
                    errors_sql,
                    exp.Literal.string("latency_ms"),
                    _copy_or_scalar_null(cell.latency_sql),
                    exp.Literal.string("fixture_trace"),
                    exp.Cast(this=exp.Null(), to=exp.DataType.build(FIXTURE_TRACE_SQL_TYPE)),
                ],
            ),
            exp.Literal.string("__agentcicd_cell"),
            exp.Boolean(this=True),
        ],
    )


def _ratelimit_argument_cell(
    value: ExprIR,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
) -> CellComponentsIR:
    if isinstance(value, CallExpr) and value.function_name.strip().lower() == "named_struct":
        return _control_named_struct_cell(value, registry=registry, scope=scope, value_fields={"max_in_flight"})
    if not isinstance(value, ColumnRefExpr):
        raise ValueError("RATELIMIT arguments must lower from declared input references")
    lowered = scope.get(value.name.lower())
    value_sql = lowered.value_sql.copy() if lowered is not None else _parse_scalar(f"{value.name}.value")
    error_sql = _copy_or_empty_errors(lowered.error_sql) if lowered is not None else _parse_scalar(f"{value.name}.metadata.errors")
    return CellComponentsIR(
        value_sql=exp.Anonymous(
            this="NAMED_STRUCT",
            expressions=[
                exp.Literal.string("key"),
                exp.Literal.string(value.name),
                exp.Literal.string("max_in_flight"),
                value_sql,
            ],
        ),
        error_sql=error_sql,
        representation="raw",
    )


def _control_named_struct_cell(
    value: CallExpr,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
    value_fields: set[str],
) -> CellComponentsIR:
    from agentcicd.sql.lowering.sql_lowering import lower_expr

    expressions: list[exp.Expression] = []
    child_cells: list[CellComponentsIR] = []
    args = list(value.args)
    index = 0
    while index < len(args):
        key = args[index]
        key_name = str(getattr(key, "value", "") or "").strip().lower() if isinstance(key, LiteralExpr) else ""
        expressions.append(lower_expr(key, registry=registry, scope={}))
        if index + 1 >= len(args):
            break
        raw_value = args[index + 1]
        if key_name in value_fields:
            cell = lower_expr_to_cell(raw_value, registry=registry, scope=scope, assume_cell_columns=True)
            expressions.append(cell.value_sql)
            child_cells.append(cell)
        else:
            expressions.append(lower_expr(raw_value, registry=registry, scope={}))
        index += 2
    return CellComponentsIR(
        value_sql=exp.Anonymous(this="NAMED_STRUCT", expressions=expressions),
        error_sql=_merge_errors(child_cells),
        representation="raw",
    )


def _pool_argument_cell(
    value: ExprIR,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
    fixture_id: str = "",
) -> CellComponentsIR:
    if isinstance(value, CallExpr) and value.function_name.strip().lower() == "named_struct":
        return _control_named_struct_cell(value, registry=registry, scope=scope, value_fields={"config_json"})
    if not isinstance(value, ColumnRefExpr):
        raise ValueError("POOL arguments must lower from declared input references")
    lowered = scope.get(value.name.lower())
    value_sql = lowered.value_sql.copy() if lowered is not None else _parse_scalar(f"{value.name}.value")
    error_sql = _copy_or_empty_errors(lowered.error_sql) if lowered is not None else _parse_scalar(f"{value.name}.metadata.errors")
    expressions = [
        exp.Literal.string("key"),
        exp.Literal.string(value.name),
        exp.Literal.string("config_json"),
        value_sql,
    ]
    if fixture_id:
        expressions.extend([exp.Literal.string("fixture_id"), exp.Literal.string(fixture_id)])
    return CellComponentsIR(
        value_sql=exp.Anonymous(
            this="NAMED_STRUCT",
            expressions=expressions,
        ),
        error_sql=error_sql,
        representation="raw",
    )


def lower_sql_expression_to_cell(
    expression: exp.Expression,
    *,
    registry: Optional[FunctionRegistry],
    scope: Optional[Dict[str, CellComponentsIR]] = None,
    assume_cell_columns: bool = False,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
    source_table_name: Optional[str] = None,
    source_relation_names: Optional[Set[str]] = None,
) -> CellComponentsIR:
    scope = scope or {}
    non_cell_names = non_cell_columns or set()

    if isinstance(expression, exp.ArraySize) and isinstance(expression.this, exp.Expression):
        argument_cell = lower_sql_expression_to_cell(
            expression.this,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        variant_array_arg = lower_variant_array_for_collection_size(
            argument_cell.value_sql,
            variant_columns=variant_columns,
            force=isinstance(argument_cell.value_sql, exp.Bracket),
        )
        clone = expression.copy()
        clone.set("this", variant_array_arg if variant_array_arg is not None else argument_cell.value_sql.copy())
        return CellComponentsIR(
            value_sql=clone,
            error_sql=_copy_or_null(argument_cell.error_sql),
        )

    if isinstance(expression, exp.DPipe):
        child_cells = [
            lower_sql_expression_to_cell(
                part,
                registry=registry,
                scope=scope,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            for part in _flatten_dpipe(expression)
        ]
        return CellComponentsIR(
            value_sql=_build_string_dpipe([cell.value_sql for cell in child_cells]),
            error_sql=_merge_errors(child_cells),
        )

    if isinstance(expression, exp.Column):
        scoped_field = _lower_scoped_column_field(expression, scope)
        if scoped_field is not None:
            return scoped_field
        unqualified_field = _lower_unqualified_cell_field_access(
            expression,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        if unqualified_field is not None:
            return unqualified_field
        return lower_expr_to_cell(
            ColumnRefExpr(name=expression.sql(dialect="spark")),
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
    if isinstance(expression, exp.Literal):
        return lower_expr_to_cell(
            expression_to_ir(expression),
            registry=registry,
            scope=scope,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
    if isinstance(expression, exp.Window):
        return _lower_window_expression_to_cell(
            expression,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
    if isinstance(expression, exp.AggFunc):
        child_cells: list[CellComponentsIR] = []
        clone = expression.copy()
        for arg_name, arg_value in list(clone.args.items()):
            if isinstance(arg_value, exp.Expression):
                cell = lower_sql_expression_to_cell(
                    arg_value,
                    registry=registry,
                    scope=scope,
                    assume_cell_columns=assume_cell_columns,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_names,
                    source_table_name=source_table_name,
                    source_relation_names=source_relation_names,
                )
                clone.set(arg_name, cell.value_sql)
                child_cells.append(cell)
            elif isinstance(arg_value, list):
                rewritten = []
                for item in arg_value:
                    if isinstance(item, exp.Expression):
                        cell = lower_sql_expression_to_cell(
                            item,
                            registry=registry,
                            scope=scope,
                            assume_cell_columns=assume_cell_columns,
                            variant_columns=variant_columns,
                            non_cell_columns=non_cell_names,
                            source_table_name=source_table_name,
                            source_relation_names=source_relation_names,
                        )
                        rewritten.append(cell.value_sql)
                        child_cells.append(cell)
                    else:
                        rewritten.append(item)
                clone.set(arg_name, rewritten)
        return CellComponentsIR(
            value_sql=clone,
            error_sql=_aggregate_error_expression(_merge_errors(child_cells)),
        )
    if isinstance(expression, exp.Cast) and isinstance(expression.this, exp.Expression):
        source_cell = lower_sql_expression_to_cell(
            expression.this,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        target_type = expression.args.get("to")
        target_sql = target_type.sql(dialect="spark") if isinstance(target_type, exp.Expression) else "STRING"
        value_sql = _parse_scalar(f"TRY_CAST({source_cell.value_sql.sql(dialect='spark')} AS {target_sql})")
        input_errors = _coalesce_or_empty_errors(source_cell.error_sql, copy=False)
        cast_error = _error_item(
            "AGENTCICD_CAST_ERROR",
            f"Could not cast value to {target_sql}",
            "CAST",
        )
        error_sql = _propagate_or_error(
            input_errors,
            exp.and_(
                exp.Is(this=source_cell.value_sql.copy(), expression=exp.Not(this=exp.Null())),
                exp.Is(this=value_sql.copy(), expression=exp.Null()),
            ),
            cast_error,
        )
        return CellComponentsIR(
            value_sql=value_sql,
            error_sql=error_sql,
        )
    if isinstance(expression, exp.ParseJSON) and isinstance(expression.this, exp.Expression):
        return _safe_parse_json_cell(
            lower_sql_expression_to_cell(
                expression.this,
                registry=registry,
                scope=scope,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
        )
    if function_name(expression) == "get":
        args = list(expression.expressions or [])
        if len(args) == 2 and all(isinstance(arg, exp.Expression) for arg in args):
            child_cells = [
                lower_sql_expression_to_cell(
                    arg,
                    registry=registry,
                    scope=scope,
                    assume_cell_columns=assume_cell_columns,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_names,
                    source_table_name=source_table_name,
                source_relation_names=source_relation_names,
                )
                for arg in args
            ]
            value_sql = exp.Anonymous(
                this="GET",
                expressions=[cell.value_sql.copy() for cell in child_cells],
            )
            tolerant_get = lower_tolerant_get_access(value_sql, variant_columns=variant_columns)
            if tolerant_get is not None:
                return _tolerant_json_access_cell(child_cells, tolerant_get)
            return CellComponentsIR(
                value_sql=value_sql,
                error_sql=_merge_errors(child_cells),
            )
    if _is_auto_json_access_expression(expression):
        args = list(expression.expressions or [])
        if args and isinstance(args[0], exp.Expression):
            if expression.meta.get("agentcicd_dynamic_variant_access") and function_name(expression) == "element_at":
                child_cells = [
                    lower_sql_expression_to_cell(
                        arg,
                        registry=registry,
                        scope=scope,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                        source_table_name=source_table_name,
                source_relation_names=source_relation_names,
                    )
                    for arg in args
                    if isinstance(arg, exp.Expression)
                ]
                value_sql = expression.copy()
                value_sql.set("expressions", [cell.value_sql.copy() for cell in child_cells])
                value_sql.meta["agentcicd_variant_access"] = True
                value_sql.meta["agentcicd_dynamic_variant_access"] = True
                return _dynamic_json_access_cell(child_cells, value_sql)
            base_cell = lower_sql_expression_to_cell(
                args[0],
                registry=registry,
                scope=scope,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            lowered_args = [base_cell.value_sql.copy(), *[arg.copy() for arg in args[1:]]]
            value_sql = expression.copy()
            value_sql.set("expressions", lowered_args)
            value_sql.meta["agentcicd_variant_access"] = True
            if expression.meta.get("agentcicd_tolerant_variant_access"):
                value_sql.meta["agentcicd_tolerant_variant_access"] = True
                return _tolerant_json_access_cell([base_cell], value_sql)
            return _json_access_cell(base_cell, value_sql)
    if isinstance(expression, exp.Bracket) and isinstance(expression.this, exp.Column):
        scoped_base = scope.get(expression.this.sql(dialect="spark").lower())
        if scoped_base is not None and (
            scoped_base.value_sql.meta.get("agentcicd_variant_access") or scoped_base.representation == "variant"
        ):
            base_value = scoped_base.value_sql.copy()
            base_value.meta["agentcicd_variant_access"] = True
            rewritten = expression.copy()
            rewritten.set("this", base_value)
            lowered = lower_bracket_json_access(rewritten, variant_columns=variant_columns)
            if lowered is not None:
                return _json_access_cell(scoped_base, lowered)
        base_cell = lower_expr_to_cell(
            ColumnRefExpr(name=expression.this.sql(dialect="spark")),
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        if base_cell.value_sql.meta.get("agentcicd_variant_access"):
            base_value = base_cell.value_sql.copy()
            base_value.meta["agentcicd_variant_access"] = True
            rewritten = expression.copy()
            rewritten.set("this", base_value)
            lowered = lower_bracket_json_access(rewritten, variant_columns=variant_columns)
            if lowered is not None:
                return _json_access_cell(base_cell, lowered)
    if isinstance(expression, exp.Anonymous):
        return lower_expr_to_cell(
            expression_to_ir(expression),
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
    if _extract_namespaced_call(expression) is not None:
        return lower_expr_to_cell(
            expression_to_ir(expression),
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )

    child_cells: list[CellComponentsIR] = []
    clone = expression.copy()
    for arg_name, arg_value in list(clone.args.items()):
        if isinstance(arg_value, exp.Expression):
            cell = lower_sql_expression_to_cell(
                arg_value,
                registry=registry,
                scope=scope,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            clone.set(arg_name, cell.value_sql)
            child_cells.append(cell)
        elif isinstance(arg_value, list):
            rewritten = []
            for item in arg_value:
                if isinstance(item, exp.Expression):
                    if isinstance(clone, exp.Bracket) and isinstance(item, exp.Literal):
                        rewritten.append(item.copy())
                        continue
                    cell = lower_sql_expression_to_cell(
                        item,
                        registry=registry,
                        scope=scope,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                        source_table_name=source_table_name,
                        source_relation_names=source_relation_names,
                    )
                    rewritten.append(cell.value_sql)
                    child_cells.append(cell)
                else:
                    rewritten.append(item)
            clone.set(arg_name, rewritten)

    if isinstance(clone, exp.Bracket):
        base_cell = child_cells[0] if child_cells else None
        if base_cell is not None and (
            base_cell.value_sql.meta.get("agentcicd_variant_access") or base_cell.representation == "variant"
        ):
            base_value = clone.this.copy()
            base_value.meta["agentcicd_variant_access"] = True
            clone.set("this", base_value)
        lowered = lower_bracket_json_access(clone, variant_columns=variant_columns)
        if lowered is not None:
            if base_cell is not None:
                return _json_access_cell(base_cell, lowered)
            clone = lowered
        else:
            lowered = lower_dynamic_variant_object_access(clone, variant_columns=variant_columns)
            if lowered is not None:
                if base_cell is not None:
                    return _dynamic_json_access_cell(child_cells, lowered)
                clone = lowered
            else:
                lowered = lower_safe_array_access(clone)
                if lowered is not None:
                    if base_cell is not None:
                        return _collection_access_cell(child_cells, lowered)
                    clone = lowered
                elif base_cell is not None:
                    return _collection_access_cell(child_cells, clone)

    return CellComponentsIR(
        value_sql=clone,
        error_sql=_merge_errors(child_cells),
    )


def _lower_window_expression_to_cell(
    expression: exp.Window,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
    assume_cell_columns: bool,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Set[str],
    source_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> CellComponentsIR:
    clone = expression.copy()
    decision_cells: list[CellComponentsIR] = []

    partition_by = []
    for item in list(expression.args.get("partition_by") or []):
        if isinstance(item, exp.Expression):
            cell = lower_sql_expression_to_cell(
                item,
                registry=registry,
                scope=scope,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_columns,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            partition_by.append(cell.value_sql)
            decision_cells.append(cell)
        else:
            partition_by.append(item)
    clone.set("partition_by", partition_by)

    order = expression.args.get("order")
    if isinstance(order, exp.Order):
        rewritten_order = []
        for ordered in list(order.expressions or []):
            if isinstance(ordered, exp.Ordered) and isinstance(ordered.this, exp.Expression):
                cell = lower_sql_expression_to_cell(
                    ordered.this,
                    registry=registry,
                    scope=scope,
                    assume_cell_columns=assume_cell_columns,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_columns,
                    source_table_name=source_table_name,
                    source_relation_names=source_relation_names,
                )
                rewritten_order.append(
                    exp.Ordered(
                        this=cell.value_sql,
                        desc=ordered.args.get("desc"),
                        nulls_first=ordered.args.get("nulls_first"),
                    )
                )
                decision_cells.append(cell)
            else:
                rewritten_order.append(ordered.copy())
        clone.set("order", exp.Order(expressions=rewritten_order))

    window_function = expression.this
    if isinstance(window_function, exp.Expression):
        function_value, function_cells = _lower_window_function_arguments(
            window_function,
            registry=registry,
            scope=scope,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        clone.set("this", function_value)
        decision_cells.extend(function_cells)

    return CellComponentsIR(
        value_sql=clone,
        error_sql=_merge_errors(decision_cells),
    )


def _lower_window_function_arguments(
    expression: exp.Expression,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
    assume_cell_columns: bool,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Set[str],
    source_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> tuple[exp.Expression, list[CellComponentsIR]]:
    clone = expression.copy()
    child_cells: list[CellComponentsIR] = []
    for arg_name, arg_value in list(clone.args.items()):
        if isinstance(arg_value, exp.Expression):
            cell = lower_sql_expression_to_cell(
                arg_value,
                registry=registry,
                scope=scope,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_columns,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            clone.set(arg_name, cell.value_sql)
            child_cells.append(cell)
        elif isinstance(arg_value, list):
            rewritten = []
            for item in arg_value:
                if isinstance(item, exp.Expression):
                    cell = lower_sql_expression_to_cell(
                        item,
                        registry=registry,
                        scope=scope,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_columns,
                        source_table_name=source_table_name,
                        source_relation_names=source_relation_names,
                    )
                    rewritten.append(cell.value_sql)
                    child_cells.append(cell)
                else:
                    rewritten.append(item)
            clone.set(arg_name, rewritten)
    return clone, child_cells


def _dynamic_json_access_cell(child_cells: list[CellComponentsIR], value_sql: exp.Expression) -> CellComponentsIR:
    source_cell = child_cells[0]
    key_cell = child_cells[1] if len(child_cells) > 1 else None
    input_errors = _merge_errors(child_cells)
    access_error = _error_item(
        "AGENTCICD_JSON_ACCESS_ERROR",
        "Could not access JSON path",
        "JSON_ACCESS",
    )
    missing_conditions = [
        exp.Is(this=source_cell.value_sql.copy(), expression=exp.Null()),
        exp.Is(this=value_sql.copy(), expression=exp.Null()),
    ]
    if key_cell is not None:
        missing_conditions.append(exp.Is(this=key_cell.value_sql.copy(), expression=exp.Null()))

    missing_condition = missing_conditions[0]
    for condition in missing_conditions[1:]:
        missing_condition = exp.or_(missing_condition, condition)

    error_sql = _propagate_or_error(input_errors, missing_condition, access_error)
    return CellComponentsIR(
        value_sql=value_sql,
        error_sql=error_sql,
        representation="variant",
    )


def _lower_scoped_column_field(
    expression: exp.Column,
    scope: Dict[str, CellComponentsIR],
) -> CellComponentsIR | None:
    if expression.db or expression.catalog or not expression.table:
        return None
    base = scope.get(str(expression.table).lower())
    if base is None:
        return None
    field_name = expression.name
    if not field_name:
        return None
    value_sql = _cell_field(base.value_sql, field_name)
    if base.value_sql.meta.get("agentcicd_variant_access") or base.representation == "variant":
        value_sql.meta["agentcicd_variant_access"] = True
    return CellComponentsIR(
        value_sql=value_sql,
        error_sql=_copy_or_null(base.error_sql),
        representation=base.representation,
    )


def _lower_unqualified_cell_field_access(
    expression: exp.Column,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
    assume_cell_columns: bool,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Set[str],
    source_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> CellComponentsIR | None:
    if not assume_cell_columns or expression.catalog:
        return None
    parts = [part.name for part in expression.parts]
    if len(parts) < 2:
        return None
    base_name = parts[0]
    if not base_name or base_name.lower() in non_cell_columns:
        return None
    source_names = {name.lower() for name in source_relation_names or set()}
    if base_name.lower() in source_names:
        return None
    base_cell = lower_expr_to_cell(
        ColumnRefExpr(name=base_name),
        registry=registry,
        scope=scope,
        assume_cell_columns=assume_cell_columns,
        variant_columns=variant_columns,
        non_cell_columns=non_cell_columns,
        source_table_name=source_table_name,
        source_relation_names=source_relation_names,
    )
    value_sql = base_cell.value_sql.copy()
    for field_name in parts[1:]:
        value_sql = _cell_field(value_sql, field_name)
    if base_cell.value_sql.meta.get("agentcicd_variant_access") or base_cell.representation == "variant":
        value_sql.meta["agentcicd_variant_access"] = True
    return CellComponentsIR(
        value_sql=value_sql,
        error_sql=_copy_or_null(base_cell.error_sql),
        latency_sql=_copy_or_scalar_null(base_cell.latency_sql),
        representation="variant" if base_cell.representation == "variant" else "raw",
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


def _is_auto_json_access_expression(expression: exp.Expression) -> bool:
    if not expression.meta.get("agentcicd_variant_access"):
        return False
    return function_name(expression) == "try_variant_get" or bool(expression.meta.get("agentcicd_dynamic_variant_access"))


def _safe_parse_json_cell(source_cell: CellComponentsIR) -> CellComponentsIR:
    source_value_sql = exp.Cast(this=source_cell.value_sql.copy(), to=exp.DataType.build("STRING"))
    value_sql = exp.Anonymous(this="TRY_PARSE_JSON", expressions=[source_value_sql])
    value_sql.meta["agentcicd_variant_access"] = True
    input_errors = _coalesce_or_empty_errors(source_cell.error_sql, copy=False)
    parse_error = _error_item(
        "AGENTCICD_JSON_PARSE_ERROR",
        "Could not parse JSON value",
        "PARSE_JSON",
    )
    error_sql = _propagate_or_error(
        input_errors,
        exp.and_(
            exp.Is(this=source_cell.value_sql.copy(), expression=exp.Not(this=exp.Null())),
            exp.Is(this=value_sql.copy(), expression=exp.Null()),
        ),
        parse_error,
    )
    return CellComponentsIR(
        value_sql=value_sql,
        error_sql=error_sql,
    )


def _json_access_cell(source_cell: CellComponentsIR, value_sql: exp.Expression) -> CellComponentsIR:
    safe_value_sql = _safe_json_access_value_sql(source_cell, value_sql)
    input_errors = _coalesce_or_empty_errors(source_cell.error_sql, copy=False)
    access_error = _error_item(
        "AGENTCICD_JSON_ACCESS_ERROR",
        "Could not access JSON path",
        "JSON_ACCESS",
    )
    error_sql = _propagate_or_error(
        input_errors,
        exp.or_(
            exp.Is(this=source_cell.value_sql.copy(), expression=exp.Null()),
            exp.Is(this=safe_value_sql.copy(), expression=exp.Null()),
        ),
        access_error,
    )
    return CellComponentsIR(
        value_sql=safe_value_sql,
        error_sql=error_sql,
    )


def _safe_json_access_value_sql(source_cell: CellComponentsIR, value_sql: exp.Expression) -> exp.Expression:
    if function_name(value_sql) != "try_variant_get":
        return value_sql
    args = list(value_sql.expressions or [])
    if len(args) < 2:
        return value_sql
    parsed_value = exp.Anonymous(
        this="TRY_PARSE_JSON",
        expressions=[_cast_cell_value_to_string(source_cell)],
    )
    parsed_value.meta["agentcicd_variant_access"] = True
    rewritten = value_sql.copy()
    rewritten.set("expressions", [parsed_value, *[arg.copy() for arg in args[1:]]])
    rewritten.meta["agentcicd_variant_access"] = True
    return rewritten


def _safe_parse_json_value(source_cell: CellComponentsIR) -> exp.Expression:
    parsed_value = exp.Anonymous(
        this="TRY_PARSE_JSON",
        expressions=[_cast_cell_value_to_string(source_cell)],
    )
    parsed_value.meta["agentcicd_variant_access"] = True
    return parsed_value


def _cast_cell_value_to_string(source_cell: CellComponentsIR) -> exp.Expression:
    return exp.Cast(
        this=source_cell.value_sql.copy(),
        to=exp.DataType.build("STRING"),
    )


def _tolerant_json_access_cell(child_cells: list[CellComponentsIR], value_sql: exp.Expression) -> CellComponentsIR:
    return CellComponentsIR(
        value_sql=value_sql,
        error_sql=_merge_errors(child_cells),
    )


def _collection_access_cell(child_cells: list[CellComponentsIR], value_sql: exp.Expression) -> CellComponentsIR:
    source_cell = child_cells[0]
    input_errors = _merge_errors(child_cells)
    access_error = _error_item(
        "AGENTCICD_ACCESS_ERROR",
        "Could not access collection value",
        "ACCESS",
    )
    error_sql = _propagate_or_error(
        input_errors,
        exp.or_(
            exp.Is(this=source_cell.value_sql.copy(), expression=exp.Null()),
            exp.Is(this=value_sql.copy(), expression=exp.Null()),
        ),
        access_error,
    )
    return CellComponentsIR(
        value_sql=value_sql,
        error_sql=error_sql,
    )


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


def _lower_ast_node_to_value(
    node: exp.Expression,
    *,
    registry: Optional[FunctionRegistry],
    scope: Dict[str, CellComponentsIR],
) -> exp.Expression:
    if isinstance(node, exp.Func):
        return lower_expr_to_cell(expression_to_ir(node), registry=registry, scope=scope).value_sql
    if _extract_namespaced_call(node) is not None:
        return lower_expr_to_cell(expression_to_ir(node), registry=registry, scope=scope).value_sql
    return node


def _merge_errors(cells: list[CellComponentsIR]) -> exp.Expression:
    errors = [
        _coalesce_or_empty_errors(cell.error_sql, copy=False)
        for cell in cells
        if not _is_empty_array(cell.error_sql)
    ]
    if not errors:
        return _empty_errors()
    if len(errors) == 1:
        return errors[0]
    return exp.Anonymous(this="CONCAT", expressions=errors)


def _cell_field(cell_sql: exp.Expression, field: str) -> exp.Expression:
    return exp.Dot(this=cell_sql, expression=exp.Identifier(this=field))


def _cell_metadata_field(cell_sql: exp.Expression, field: str) -> exp.Expression:
    return exp.Dot(this=_cell_field(cell_sql, "metadata"), expression=exp.Identifier(this=field))


def _propagate_or_error(
    input_errors: exp.Expression,
    error_condition: exp.Expression,
    new_error: exp.Expression,
) -> exp.Expression:
    return exp.Case(
        ifs=[
            exp.If(
                this=exp.GT(
                    this=exp.Anonymous(this="SIZE", expressions=[input_errors]),
                    expression=exp.Literal.number("0"),
                ),
                true=input_errors,
            ),
            exp.If(this=error_condition, true=new_error),
        ],
        default=_empty_errors(),
    )


def _aggregate_error_expression(expression: exp.Expression) -> exp.Expression:
    if _is_empty_array(expression):
        return expression
    if _contains_non_window_aggregate(expression):
        return expression
    return exp.Anonymous(
        this="FLATTEN",
        expressions=[exp.Anonymous(this="COLLECT_LIST", expressions=[expression.copy()])],
    )


def _contains_non_window_aggregate(expression: exp.Expression) -> bool:
    for node in expression.walk():
        if not _is_aggregate_expression(node):
            continue
        parent = node.parent
        inside_window = False
        while parent is not None:
            if isinstance(parent, exp.Window):
                inside_window = True
                break
            parent = parent.parent
        if not inside_window:
            return True
    return False


def _is_aggregate_expression(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.AggFunc):
        return True
    if isinstance(expression, exp.Anonymous):
        return str(expression.this or "").upper() in {"COLLECT_LIST", "COLLECT_SET"}
    return False


def _copy_or_null(expression: Optional[exp.Expression]) -> exp.Expression:
    if expression is None:
        return _empty_errors()
    return expression.copy()


def _copy_or_scalar_null(expression: Optional[exp.Expression]) -> exp.Expression:
    if expression is None:
        return exp.Null()
    return expression.copy()


def _copy_or_none(expression: Optional[exp.Expression]) -> exp.Expression | None:
    if expression is None:
        return None
    return expression.copy()


def _copy_or_empty_errors(expression: Optional[exp.Expression]) -> exp.Expression:
    return _coalesce_or_empty_errors(expression, copy=True)


def _coalesce_or_empty_errors(expression: Optional[exp.Expression], *, copy: bool) -> exp.Expression:
    if expression is None or isinstance(expression, exp.Null):
        return _empty_errors()
    if _is_empty_array(expression):
        return expression.copy() if copy else expression
    return exp.Coalesce(this=expression.copy() if copy else expression, expressions=[_empty_errors()])


def _empty_errors() -> exp.Expression:
    return _parse_scalar(f"CAST(ARRAY() AS {ERROR_ARRAY_SQL_TYPE})")


def _error_item(code: str, message: str, source: str) -> exp.Expression:
    return _parse_scalar(
        "ARRAY(NAMED_STRUCT("
        f"'code', '{_escape_sql(code)}', "
        f"'message', '{_escape_sql(message)}', "
        f"'source', '{_escape_sql(source)}', "
        "'path', CAST(NULL AS STRING), "
        "'recoverable', true, "
        "'cause_code', CAST(NULL AS STRING), "
        "'cause_message', CAST(NULL AS STRING), "
        "'details', CAST(MAP() AS MAP<STRING,STRING>)"
        "))"
    )


def _value_null_when_errors(value_sql: exp.Expression, errors_sql: exp.Expression) -> exp.Expression:
    return exp.Case(
        ifs=[
            exp.If(
                this=exp.GT(this=exp.Anonymous(this="SIZE", expressions=[errors_sql]), expression=exp.Literal.number("0")),
                true=exp.Null(),
            )
        ],
        default=value_sql,
    )


def _is_empty_array(expression: Optional[exp.Expression]) -> bool:
    if isinstance(expression, exp.Array):
        return not list(expression.expressions or [])
    if isinstance(expression, exp.Cast):
        return _is_empty_array(expression.this)
    return False


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _parse_scalar(sql_text: str) -> exp.Expression:
    return _parse_scalar_cached(sql_text).copy()


@lru_cache(maxsize=4096)
def _parse_scalar_cached(sql_text: str) -> exp.Expression:
    import sqlglot

    parsed = sqlglot.parse_one(f"SELECT {sql_text}", read="spark")
    return parsed.expressions[0]
