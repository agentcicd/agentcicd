from __future__ import annotations

from functools import lru_cache
from typing import Optional, Set

from sqlglot import expressions as exp

from agentcicd.sql.json_semantics import is_variant_expression
from agentcicd.sql.ir.expressions import SqlAstExpr
from agentcicd.sql.ir.metadata import CellComponentsIR
from agentcicd.sql.ir.statements import BatchTableStmt, DeclareInputStmt, StatementIR, StreamTableStmt
from agentcicd.sql.engine.cell_metadata import ERROR_ARRAY_SQL_TYPE, FIXTURE_TRACE_SQL_TYPE
from agentcicd.sql.pool_inputs import canonical_pool_default_json
from agentcicd.sql.lowering.metadata_lowering import (
    build_cell_struct,
    lower_expr_to_cell,
    lower_sql_expression_to_cell,
)
from agentcicd.sql.lowering.sql_lowering import lower_expr
from agentcicd.sql.semantics.registry import FunctionRegistry


def lower_statement_sql(
    statement: StatementIR,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> str:
    if isinstance(statement, BatchTableStmt):
        if statement.query is None:
            raise ValueError(f"Batch table '{statement.name}' is missing a query")
        visible_variant_columns = _statement_query_variant_columns(statement, registry=registry, variant_columns=variant_columns)
        return lower_expr(
            statement.query,
            registry=registry,
            variant_columns=visible_variant_columns,
        ).sql(dialect="spark")
    if isinstance(statement, StreamTableStmt):
        if statement.query is None:
            raise ValueError(f"Stream table '{statement.name}' is missing a query")
        visible_variant_columns = _statement_query_variant_columns(statement, registry=registry, variant_columns=variant_columns)
        return lower_expr(
            statement.query,
            registry=registry,
            variant_columns=visible_variant_columns,
        ).sql(dialect="spark")
    if isinstance(statement, DeclareInputStmt):
        return lower_declare_input_sql(statement)
    raise ValueError(f"Lowering not implemented for statement type '{type(statement).__name__}'")


def lower_declare_input_sql(statement: DeclareInputStmt) -> str:
    spark_type = _spark_input_type(statement.input_type)
    sql = f"DECLARE OR REPLACE VARIABLE {statement.name} {spark_type}"
    if statement.default_sql is not None:
        default_sql = statement.default_sql
        if statement.input_type.strip().upper() == "POOL":
            default_sql = _quote_sql_string(canonical_pool_default_json(statement))
        sql += f" DEFAULT {default_sql}"
    return sql


def lower_declare_input_cell_sql(statement: DeclareInputStmt) -> str:
    spark_type = _spark_input_type(statement.input_type)
    cell_type = _cell_sql_type(spark_type)
    default_sql = statement.default_sql
    if statement.input_type.strip().upper() == "POOL":
        default_sql = _quote_sql_string(canonical_pool_default_json(statement))
    value_sql = f"CAST({default_sql if default_sql is not None else 'NULL'} AS {spark_type})"
    default_cell = (
        "named_struct("
        "'cell_id', CAST(NULL AS STRING), "
        f"'value', {value_sql}, "
        "'metadata', named_struct("
        f"'errors', CAST(array() AS {ERROR_ARRAY_SQL_TYPE}), "
        "'latency_ms', CAST(NULL AS BIGINT), "
        f"'fixture_trace', CAST(NULL AS {FIXTURE_TRACE_SQL_TYPE})"
        "), "
        "'__agentcicd_cell', true"
        ")"
    )
    return f"DECLARE OR REPLACE VARIABLE {statement.name} {cell_type} DEFAULT {default_cell}"


def _cell_sql_type(value_type: str) -> str:
    return (
        "STRUCT<"
        "cell_id:STRING,"
        f"value:{value_type},"
        "metadata:STRUCT<"
        f"errors:{ERROR_ARRAY_SQL_TYPE},"
        "latency_ms:BIGINT,"
        f"fixture_trace:{FIXTURE_TRACE_SQL_TYPE}"
        ">,"
        "__agentcicd_cell:BOOLEAN"
        ">"
    )


def _spark_input_type(input_type: str) -> str:
    normalized = input_type.strip().upper()
    if normalized in {"AISYSTEM", "DATASET", "SECRET"}:
        return "STRING"
    if normalized == "RATELIMIT":
        return "INT"
    if normalized == "POOL":
        return "STRING"
    return normalized


def _quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def lower_statement_cells_sql(
    statement: StatementIR,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
) -> str:
    return lower_statement_cells_expression(
        statement,
        registry,
        variant_columns=variant_columns,
        non_cell_columns=non_cell_columns,
    ).sql(dialect="spark")


def lower_statement_cells_expression(
    statement: StatementIR,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    if isinstance(statement, (BatchTableStmt, StreamTableStmt)):
        if statement.query is None:
            raise ValueError(f"Statement '{statement.name}' is missing a query")
        visible_variant_columns = _statement_query_variant_columns(statement, registry=registry, variant_columns=variant_columns)
        lowered = lower_expr(statement.query, registry=registry, variant_columns=visible_variant_columns)
        return _lower_query_to_cells(
            lowered,
            registry,
            variant_columns=visible_variant_columns,
            non_cell_columns=non_cell_columns,
            output_table_name=statement.name,
        )
    raise ValueError(f"Cell lowering not implemented for statement type '{type(statement).__name__}'")


def _lower_query_to_cells(
    query: exp.Expression,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
    output_table_name: Optional[str] = None,
) -> exp.Expression:
    non_cell_names = non_cell_columns or set()
    if isinstance(query, exp.Select):
        lowered_select = query.copy()
        _lower_derived_sources_to_cells(
            lowered_select,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            output_table_name=output_table_name,
        )
        reads_raw_values = _select_reads_raw_values(lowered_select)
        assume_cell_columns = not reads_raw_values
        source_table_name = output_table_name if reads_raw_values else None
        source_relation_names = _select_source_relation_names(lowered_select)
        if reads_raw_values and _select_has_generator_projection(lowered_select):
            return _lower_projection_generator_select_to_cells(
                lowered_select,
                registry,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                output_table_name=output_table_name,
                source_relation_names=source_relation_names,
            )
        if not reads_raw_values and _select_has_generator_projection(lowered_select):
            return _lower_projection_generator_select_to_cells(
                lowered_select,
                registry,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=None,
                output_table_name=output_table_name,
                source_relation_names=source_relation_names,
            )
        with_clause = lowered_select.args.get("with") or lowered_select.args.get("with_")
        if isinstance(with_clause, exp.With):
            rewritten_ctes = []
            for cte in list(with_clause.expressions or []):
                if isinstance(cte, exp.CTE) and isinstance(cte.this, exp.Expression):
                    rewritten_ctes.append(
                        exp.CTE(
                            this=_lower_query_to_cells(
                                cte.this,
                                registry,
                                variant_columns=variant_columns,
                                non_cell_columns=non_cell_names,
                                output_table_name=output_table_name,
                            ),
                            alias=cte.args.get("alias"),
                            materialized=cte.args.get("materialized"),
                            scalar=cte.args.get("scalar"),
                        )
                    )
                else:
                    rewritten_ctes.append(cte)
            lowered_select.set("with_", exp.With(expressions=rewritten_ctes, recursive=with_clause.args.get("recursive")))
        if isinstance(lowered_select.args.get("where"), exp.Where):
            where_cell = lower_sql_expression_to_cell(
                lowered_select.args["where"].this,
                registry=registry,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            lowered_select.set("where", exp.Where(this=_semantic_decision_value(where_cell, "WHERE")))
        if isinstance(lowered_select.args.get("having"), exp.Having):
            having_cell = lower_sql_expression_to_cell(
                lowered_select.args["having"].this,
                registry=registry,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                source_relation_names=source_relation_names,
            )
            lowered_select.set("having", exp.Having(this=_semantic_decision_value(having_cell, "HAVING")))
        group = lowered_select.args.get("group")
        if isinstance(group, exp.Group):
            lowered_select.set(
                "group",
                exp.Group(
                    expressions=[
                        lower_sql_expression_to_cell(
                            item,
                            registry=registry,
                            assume_cell_columns=assume_cell_columns,
                            variant_columns=variant_columns,
                            non_cell_columns=non_cell_names,
                            source_table_name=source_table_name,
                            source_relation_names=source_relation_names,
                        ).value_sql
                        for item in list(group.expressions or [])
                    ]
                ),
            )
        aggregate_context = _query_has_aggregate_context(lowered_select)
        projection_alias_expressions = _select_projection_alias_expressions(lowered_select) if aggregate_context else {}
        order = lowered_select.args.get("order")
        if isinstance(order, exp.Order):
            rewritten_order = []
            for ordered in list(order.expressions or []):
                if isinstance(ordered, exp.Ordered):
                    alias_value = _order_projection_alias_value(
                        ordered.this,
                        projection_alias_expressions,
                    )
                    if aggregate_context and alias_value is not None:
                        rewritten_order.append(
                            exp.Ordered(
                                this=alias_value,
                                desc=ordered.args.get("desc"),
                                nulls_first=ordered.args.get("nulls_first"),
                            )
                        )
                        continue
                    order_expression = _resolve_order_alias_expression(
                        ordered.this,
                        projection_alias_expressions,
                    )
                    ordered_cell = lower_sql_expression_to_cell(
                        order_expression,
                        registry=registry,
                        assume_cell_columns=assume_cell_columns,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                        source_table_name=source_table_name,
                        source_relation_names=source_relation_names,
                    )
                    if aggregate_context:
                        rewritten_order.append(
                            exp.Ordered(
                                this=_aggregate_order_value(ordered_cell.value_sql),
                                desc=ordered.args.get("desc"),
                                nulls_first=ordered.args.get("nulls_first"),
                            )
                        )
                        continue
                    rewritten_order.append(
                        exp.Ordered(
                            this=_semantic_decision_value(ordered_cell, "ORDER BY"),
                            desc=ordered.args.get("desc"),
                            nulls_first=ordered.args.get("nulls_first"),
                        )
                    )
                else:
                    rewritten_order.append(ordered)
            lowered_select.set("order", exp.Order(expressions=rewritten_order))
        for join in lowered_select.args.get("joins") or []:
            on_expr = join.args.get("on")
            if isinstance(on_expr, exp.Expression):
                join.set(
                    "on",
                    _semantic_decision_value(
                        lower_sql_expression_to_cell(
                            on_expr,
                            registry=registry,
                            assume_cell_columns=assume_cell_columns,
                            variant_columns=variant_columns,
                            non_cell_columns=non_cell_names,
                            source_table_name=source_table_name,
                            source_relation_names=source_relation_names,
                        ),
                        "JOIN ON",
                    ),
                )
        if isinstance(lowered_select.args.get("distinct"), exp.Distinct) and not aggregate_context:
            return _lower_distinct_select_to_cells(
                lowered_select,
                registry,
                assume_cell_columns=assume_cell_columns,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                source_table_name=source_table_name,
                output_table_name=output_table_name,
                source_relation_names=source_relation_names,
            )
        lowered_select.set(
            "expressions",
            [
                _lower_projection_to_cell(
                    projection,
                    registry,
                    aggregate_context=aggregate_context,
                    assume_cell_columns=assume_cell_columns,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_names,
                    source_table_name=source_table_name,
                    output_table_name=output_table_name,
                    source_relation_names=source_relation_names,
                )
                for projection in _expand_raw_values_star_projections(lowered_select)
            ],
        )
        return lowered_select
    if isinstance(query, exp.SetOperation):
        if bool(query.args.get("distinct", True)):
            return _lower_distinct_set_operation_to_cells(
                query,
                registry,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_names,
                output_table_name=output_table_name,
            )
        lowered_set = query.copy()
        _lower_with_ctes_to_cells(
            lowered_set,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_names,
            output_table_name=output_table_name,
        )
        if isinstance(lowered_set.this, exp.Expression):
            lowered_set.set(
                "this",
                _lower_query_to_cells(
                    lowered_set.this,
                    registry,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_names,
                    output_table_name=output_table_name,
                ),
            )
        if isinstance(lowered_set.expression, exp.Expression):
            lowered_set.set(
                "expression",
                _lower_query_to_cells(
                    lowered_set.expression,
                    registry,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_names,
                    output_table_name=output_table_name,
                ),
            )
        order = lowered_set.args.get("order")
        if isinstance(order, exp.Order):
            rewritten_order = []
            for ordered in list(order.expressions or []):
                if isinstance(ordered, exp.Ordered):
                    ordered_cell = lower_sql_expression_to_cell(
                        ordered.this,
                        registry=registry,
                        assume_cell_columns=True,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_names,
                    )
                    rewritten_order.append(
                        exp.Ordered(
                            this=_semantic_decision_value(ordered_cell, "ORDER BY"),
                            desc=ordered.args.get("desc"),
                            nulls_first=ordered.args.get("nulls_first"),
                        )
                    )
                else:
                    rewritten_order.append(ordered)
            lowered_set.set("order", exp.Order(expressions=rewritten_order))
        return lowered_set
    raise ValueError("Cell lowering currently requires a SELECT or set-operation query")


def _lower_distinct_select_to_cells(
    query: exp.Select,
    registry: FunctionRegistry,
    *,
    assume_cell_columns: bool,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    source_table_name: Optional[str],
    output_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> exp.Select:
    raw_query, columns = _lower_select_to_raw_cells(
        query,
        registry,
        assume_cell_columns=assume_cell_columns,
        variant_columns=variant_columns,
        non_cell_columns=non_cell_columns,
        source_table_name=source_table_name,
        output_table_name=output_table_name,
        source_relation_names=source_relation_names,
    )
    return _wrap_grouped_raw_cells(
        raw_query,
        columns,
        registry=registry,
        output_table_name=output_table_name,
        operation="distinct",
    )


def _lower_distinct_set_operation_to_cells(
    query: exp.SetOperation,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    output_table_name: Optional[str],
) -> exp.Select:
    raw_query, columns = _lower_query_to_raw_cells(
        query,
        registry,
        variant_columns=variant_columns,
        non_cell_columns=non_cell_columns,
        output_table_name=output_table_name,
    )
    return _wrap_grouped_raw_cells(
        raw_query,
        columns,
        registry=registry,
        output_table_name=output_table_name,
        operation="union",
    )


def _lower_query_to_raw_cells(
    query: exp.Expression,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    output_table_name: Optional[str],
) -> tuple[exp.Expression, list[dict[str, str]]]:
    if isinstance(query, exp.Select):
        lowered_select = query.copy()
        _lower_derived_sources_to_cells(
            lowered_select,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            output_table_name=output_table_name,
        )
        assume_cell_columns = not _select_reads_raw_values(lowered_select)
        source_table_name = output_table_name if not assume_cell_columns else None
        source_relation_names = _select_source_relation_names(lowered_select)
        return _lower_select_to_raw_cells(
            lowered_select,
            registry,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            source_table_name=source_table_name,
            output_table_name=output_table_name,
            source_relation_names=source_relation_names,
        )
    if isinstance(query, exp.SetOperation):
        if not isinstance(query, exp.Union):
            raise ValueError("Wrapped DISTINCT lowering currently supports UNION; INTERSECT/EXCEPT need explicit value/error models")
        lowered_query = query.copy()
        _lower_with_ctes_to_cells(
            lowered_query,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            output_table_name=output_table_name,
        )
        left_query, left_columns = _lower_query_to_raw_cells(
            lowered_query.this,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            output_table_name=output_table_name,
        )
        right_query, right_columns = _lower_query_to_raw_cells(
            lowered_query.expression,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            output_table_name=output_table_name,
        )
        raw_union = exp.Union(
            this=_normalize_raw_cell_query(left_query, left_columns, left_columns, "__agentcicd_set_left"),
            expression=_normalize_raw_cell_query(right_query, right_columns, left_columns, "__agentcicd_set_right"),
            distinct=False,
        )
        return raw_union, left_columns
    return query.copy(), []


def _lower_select_to_raw_cells(
    query: exp.Select,
    registry: FunctionRegistry,
    *,
    assume_cell_columns: bool,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    source_table_name: Optional[str],
    output_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> tuple[exp.Select, list[dict[str, str]]]:
    raw_query = query.copy()
    raw_query.set("distinct", None)
    columns: list[dict[str, str]] = []
    raw_projections: list[exp.Expression] = []
    for index, projection in enumerate(_expand_raw_values_star_projections(raw_query)):
        if _is_star_projection(projection):
            raise ValueError("DISTINCT/UNION lowering cannot preserve metadata for star projections")
        alias_name = _projection_output_name(projection)
        value_alias = _hidden_set_column_alias(index, "value")
        errors_alias = _hidden_set_column_alias(index, "errors")
        columns.append(
            {
                "output": alias_name,
                "value": value_alias,
                "errors": errors_alias,
            }
        )
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        cell = lower_sql_expression_to_cell(
            expression.copy(),
            registry=registry,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        raw_projections.append(exp.alias_(cell.value_sql, value_alias, copy=False))
        raw_projections.append(exp.alias_(cell.error_sql, errors_alias, copy=False))
    raw_query.set("expressions", raw_projections)
    return raw_query, columns


def _normalize_raw_cell_query(
    raw_query: exp.Expression,
    source_columns: list[dict[str, str]],
    target_columns: list[dict[str, str]],
    table_alias: str,
) -> exp.Select:
    projections: list[exp.Expression] = []
    for source, target in zip(source_columns, target_columns):
        projections.append(exp.alias_(exp.column(source["value"]), target["value"], copy=False))
        projections.append(exp.alias_(exp.column(source["errors"]), target["errors"], copy=False))
    return exp.select(*projections).from_(
        exp.Subquery(
            this=raw_query,
            alias=exp.TableAlias(this=exp.to_identifier(table_alias)),
        )
    )


def _wrap_grouped_raw_cells(
    raw_query: exp.Expression,
    columns: list[dict[str, str]],
    *,
    registry: FunctionRegistry,
    output_table_name: Optional[str],
    operation: str,
) -> exp.Select:
    projections = [
        exp.alias_(
            build_cell_struct(
                CellComponentsIR(
                    value_sql=exp.column(column["value"]),
                    error_sql=_aggregate_error_expression(exp.column(column["errors"])),
                    latency_sql=exp.Null(),
                    representation="raw",
                )
            ),
            column["output"],
            copy=False,
        )
        for column in columns
    ]
    wrapped = exp.select(*projections).from_(
        exp.Subquery(
            this=raw_query,
            alias=exp.TableAlias(this=exp.to_identifier("__agentcicd_set_values")),
        )
    )
    wrapped.set("group", exp.Group(expressions=[exp.column(column["value"]) for column in columns]))
    return wrapped


def _hidden_set_column_alias(index: int, role: str) -> str:
    return f"__agentcicd_set_{index}_{role}"


def _lower_derived_sources_to_cells(
    query: exp.Select,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    output_table_name: Optional[str],
) -> None:
    from_expr = query.args.get("from_")
    if isinstance(from_expr, exp.From):
        from_expr.set(
            "this",
            _lower_derived_source_to_cells(
                from_expr.this,
                registry,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_columns,
                output_table_name=output_table_name,
            ),
        )
    for join in query.args.get("joins") or []:
        join.set(
            "this",
            _lower_derived_source_to_cells(
                join.this,
                registry,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_columns,
                output_table_name=output_table_name,
            ),
        )


def _lower_with_ctes_to_cells(
    query: exp.Expression,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    output_table_name: Optional[str],
) -> None:
    with_clause = query.args.get("with") or query.args.get("with_")
    if not isinstance(with_clause, exp.With):
        return
    rewritten_ctes = []
    for cte in list(with_clause.expressions or []):
        if isinstance(cte, exp.CTE) and isinstance(cte.this, exp.Expression):
            rewritten_ctes.append(
                exp.CTE(
                    this=_lower_query_to_cells(
                        cte.this,
                        registry,
                        variant_columns=variant_columns,
                        non_cell_columns=non_cell_columns,
                        output_table_name=output_table_name,
                    ),
                    alias=cte.args.get("alias"),
                    materialized=cte.args.get("materialized"),
                    scalar=cte.args.get("scalar"),
                )
            )
        else:
            rewritten_ctes.append(cte)
    query.set("with_", exp.With(expressions=rewritten_ctes, recursive=with_clause.args.get("recursive")))


def _lower_derived_source_to_cells(
    source: exp.Expression,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    output_table_name: Optional[str],
) -> exp.Expression:
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Expression):
        lowered = source.copy()
        lowered.set(
            "this",
            _lower_query_to_cells(
                source.this,
                registry,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_columns,
                output_table_name=output_table_name,
            ),
        )
        return lowered
    return source


def _lower_projection_to_cell(
    projection: exp.Expression,
    registry: FunctionRegistry,
    *,
    aggregate_context: bool = False,
    assume_cell_columns: bool = True,
    variant_columns: Optional[Set[str]] = None,
    non_cell_columns: Optional[Set[str]] = None,
    source_table_name: Optional[str] = None,
    output_table_name: Optional[str] = None,
    scope: Optional[dict[str, CellComponentsIR]] = None,
    source_relation_names: Optional[Set[str]] = None,
) -> exp.Expression:
    if _is_star_projection(projection):
        return projection.copy()
    if isinstance(projection, exp.Alias):
        alias_name = projection.alias
        expression = projection.this
    else:
        alias_name = projection.output_name or projection.sql(dialect="spark")
        expression = projection
    if (
        assume_cell_columns
        and not aggregate_context
        and isinstance(expression, exp.Column)
        and expression.sql(dialect="spark").lower() not in (scope or {})
        and not _column_reads_scope_field(expression, scope or {})
        and _column_is_direct_cell_projection(expression, source_relation_names or set())
        and expression.sql(dialect="spark").lower() not in (non_cell_columns or set())
    ):
        return exp.alias_(expression.copy(), alias_name, copy=False)
    cell = lower_expr_to_cell(
        SqlAstExpr(expression=expression.copy()),
        registry=registry,
        scope=scope,
        assume_cell_columns=assume_cell_columns,
        variant_columns=variant_columns,
        non_cell_columns=non_cell_columns,
        source_table_name=source_table_name,
        source_relation_names=source_relation_names,
    )
    if aggregate_context:
        cell.error_sql = _aggregate_projection_errors(cell.error_sql)
        cell.latency_sql = exp.Null()
        cell.cell_sql = None
        cell.representation = "raw"
    return exp.alias_(build_cell_struct(cell), alias_name, copy=False)


def _lower_raw_generator_select_to_cells(
    query: exp.Select,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    source_table_name: Optional[str],
    output_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> exp.Select:
    raw_query = query.copy()
    outer_projections = []
    for projection in list(query.expressions or []):
        alias_name = _projection_alias_name(projection)
        if alias_name is None:
            alias_name = projection.sql(dialect="spark")
        outer_projections.append(
            _lower_projection_to_cell(
                exp.column(alias_name),
                registry,
                assume_cell_columns=False,
                variant_columns=variant_columns,
                non_cell_columns=non_cell_columns,
            source_table_name=source_table_name,
            output_table_name=output_table_name,
            source_relation_names=source_relation_names,
        )
        )
    return exp.select(*outer_projections).from_(
        exp.Subquery(
            this=raw_query,
            alias=exp.TableAlias(this=exp.to_identifier("__agentcicd_raw")),
        )
    )


def _lower_projection_generator_select_to_cells(
    query: exp.Select,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]],
    non_cell_columns: Optional[Set[str]],
    source_table_name: Optional[str],
    output_table_name: Optional[str],
    source_relation_names: Optional[Set[str]],
) -> exp.Select:
    raw_query = query.copy()
    raw_projections: list[exp.Expression] = [exp.Star()]
    outer_projections: list[exp.Expression] = []
    generator_scope: dict[str, CellComponentsIR] = {}
    reads_raw_values = _select_reads_raw_values(query)
    assume_cell_columns = not reads_raw_values

    for index, projection in enumerate(list(query.expressions or [])):
        generator_info = _projection_generator_info(projection)
        if generator_info is None:
            continue
        generator, output_columns = generator_info
        argument = generator.this if isinstance(generator.this, exp.Expression) else exp.Null()
        argument_cell = lower_sql_expression_to_cell(
            argument,
            registry=registry,
            assume_cell_columns=assume_cell_columns,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
            source_table_name=source_table_name,
            source_relation_names=source_relation_names,
        )
        generator_value = generator.copy()
        generator_argument_was_variant = is_variant_expression(argument_cell.value_sql, variant_columns=variant_columns)
        generator_value.set(
            "this",
            _collection_generator_argument(argument_cell.value_sql.copy(), variant_columns=variant_columns),
        )
        raw_projections.append(_generator_projection(generator_value, output_columns))
        errors_alias = f"__agentcicd_gen_{index}_errors"
        raw_projections.append(exp.alias_(argument_cell.error_sql.copy(), errors_alias, copy=False))

        for output_index, output_column in enumerate(output_columns):
            value_sql = exp.column(output_column, table="__agentcicd_generated")
            output_is_variant = generator_argument_was_variant and _generator_output_is_element(generator, output_index)
            if output_is_variant:
                value_sql.meta["agentcicd_variant_access"] = True
            generator_scope[output_column.lower()] = CellComponentsIR(
                value_sql=value_sql,
                error_sql=exp.column(errors_alias, table="__agentcicd_generated"),
                representation="variant" if output_is_variant else "raw",
            )

    for projection in list(query.expressions or []):
        generator_info = _projection_generator_info(projection)
        if generator_info is not None:
            _, output_columns = generator_info
            for output_column in output_columns:
                outer_projections.append(
                    exp.alias_(
                        build_cell_struct(generator_scope[output_column.lower()]),
                        output_column,
                        copy=False,
                    )
                )
        else:
            outer_projections.append(
                _lower_projection_to_cell(
                    projection,
                    registry,
                    assume_cell_columns=assume_cell_columns,
                    variant_columns=variant_columns,
                    non_cell_columns=non_cell_columns,
                    source_table_name=source_table_name,
                    output_table_name=output_table_name,
                    scope=generator_scope,
                    source_relation_names=source_relation_names,
                )
            )

    raw_query.set("expressions", raw_projections)
    return exp.select(*outer_projections).from_(
        exp.Subquery(
            this=raw_query,
            alias=exp.TableAlias(this=exp.to_identifier("__agentcicd_generated")),
        )
    )


def _column_reads_scope_field(expression: exp.Column, scope: dict[str, CellComponentsIR]) -> bool:
    return bool(expression.table and str(expression.table).lower() in scope and not expression.db and not expression.catalog)


def _select_source_relation_names(query: exp.Select) -> Set[str]:
    names: Set[str] = set()

    def _add_source(source: Optional[exp.Expression]) -> None:
        if source is None:
            return
        alias_or_name = source.alias_or_name
        if alias_or_name:
            names.add(alias_or_name.lower())

    from_clause = query.args.get("from") or query.args.get("from_")
    if isinstance(from_clause, exp.From):
        _add_source(from_clause.this)

    for join in query.args.get("joins") or []:
        if isinstance(join, exp.Join):
            _add_source(join.this)

    return names


def _column_is_direct_cell_projection(expression: exp.Column, source_relation_names: Set[str]) -> bool:
    parts = [part.name for part in expression.parts]
    if len(parts) == 1:
        return True
    if len(parts) == 2 and parts[0].lower() in {name.lower() for name in source_relation_names}:
        return True
    return False


def _collection_generator_argument(
    expression: exp.Expression,
    *,
    variant_columns: Optional[Set[str]],
) -> exp.Expression:
    from agentcicd.sql.json_semantics import lower_variant_array_for_collection_size

    variant_array = lower_variant_array_for_collection_size(
        expression,
        variant_columns=variant_columns,
        force=bool(expression.meta.get("agentcicd_variant_access")),
    )
    return variant_array if variant_array is not None else expression


def _select_has_generator_projection(query: exp.Select) -> bool:
    return any(_projection_generator_info(projection) is not None for projection in list(query.expressions or []))


def _projection_generator_info(projection: exp.Expression) -> tuple[exp.Expression, list[str]] | None:
    if (
        isinstance(projection, exp.Alias)
        and isinstance(projection.this, exp.Explode)
        and not isinstance(projection.this, exp.Posexplode)
    ):
        alias_name = projection.alias_or_name
        if alias_name:
            return projection.this, [alias_name.lower()]
    if isinstance(projection, exp.Aliases) and isinstance(projection.this, (exp.Explode, exp.Posexplode)):
        output_columns = [column.name.lower() for column in list(projection.expressions or []) if column.name]
        expected_columns = 2 if isinstance(projection.this, exp.Posexplode) else 1
        if len(output_columns) == expected_columns:
            return projection.this, output_columns
    return None


def _generator_projection(generator: exp.Expression, output_columns: list[str]) -> exp.Expression:
    if len(output_columns) == 1:
        return exp.alias_(generator, output_columns[0], copy=False)
    return exp.Aliases(
        this=generator,
        expressions=[exp.to_identifier(column) for column in output_columns],
    )


def _generator_output_is_element(generator: exp.Expression, output_index: int) -> bool:
    if isinstance(generator, exp.Posexplode):
        return output_index == 1
    return isinstance(generator, exp.Explode)


def _query_has_aggregate_context(query: exp.Select) -> bool:
    group = query.args.get("group")
    if isinstance(group, exp.Group) and list(group.expressions or []):
        return True
    having = query.args.get("having")
    if isinstance(having, exp.Having) and _contains_non_window_aggregate(having):
        return True
    return any(
        isinstance(expression, exp.Expression) and _contains_non_window_aggregate(expression)
        for expression in list(query.expressions or [])
    )


def _select_projection_alias_expressions(query: exp.Select) -> dict[str, exp.Expression]:
    aliases: dict[str, exp.Expression] = {}
    for projection in list(query.expressions or []):
        if not isinstance(projection, exp.Alias):
            output_name = projection.output_name
            if output_name:
                aliases[output_name.lower()] = projection.copy()
            continue
        alias_name = projection.alias
        if alias_name:
            aliases[alias_name.lower()] = projection.this.copy()
    return aliases


def _resolve_order_alias_expression(
    expression: exp.Expression,
    projection_alias_expressions: dict[str, exp.Expression],
) -> exp.Expression:
    if not projection_alias_expressions:
        return expression
    if not isinstance(expression, exp.Column):
        return expression
    if expression.table or expression.db or expression.catalog:
        return expression
    alias_expression = projection_alias_expressions.get(expression.name.lower())
    if alias_expression is None:
        return expression
    return alias_expression.copy()


def _order_projection_alias_value(
    expression: exp.Expression,
    projection_alias_expressions: dict[str, exp.Expression],
) -> exp.Expression | None:
    if not projection_alias_expressions:
        return None
    if not isinstance(expression, exp.Column):
        return None
    if expression.table or expression.db or expression.catalog:
        return None
    alias_name = expression.name.lower()
    if alias_name not in projection_alias_expressions:
        return None
    return _parse_scalar(f"{expression.name}.value")


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


def _aggregate_error_expression(expression: exp.Expression) -> exp.Expression:
    if _is_empty_array(expression):
        return expression
    if _contains_non_window_aggregate(expression):
        return expression
    return exp.Anonymous(
        this="FLATTEN",
        expressions=[
            exp.Anonymous(this="COLLECT_LIST", expressions=[expression.copy()])
        ],
    )


def _aggregate_unaggregated_metadata_fields(expression: exp.Expression, *, field: str) -> exp.Expression:
    target_field = field.lower()

    def _replace(node: exp.Expression) -> exp.Expression:
        if _is_aggregate_expression(node):
            return node.copy()
        if isinstance(node, exp.Column) and _is_cell_metadata_column(node, target_field):
            if target_field == "errors":
                return exp.Anonymous(
                    this="FLATTEN",
                    expressions=[
                        exp.Anonymous(this="COLLECT_LIST", expressions=[node.copy()])
                    ],
                )
        clone = node.copy()
        for arg_name, arg_value in list(clone.args.items()):
            if isinstance(arg_value, exp.Expression):
                clone.set(arg_name, _replace(arg_value))
            elif isinstance(arg_value, list):
                clone.set(
                    arg_name,
                    [
                        _replace(item) if isinstance(item, exp.Expression) else item
                        for item in arg_value
                    ],
                )
        return clone

    return _replace(expression)


def _aggregate_order_value(expression: exp.Expression) -> exp.Expression:
    if _contains_non_window_aggregate(expression):
        return expression
    return exp.Anonymous(
        this="FIRST",
        expressions=[expression.copy(), exp.Boolean(this=True)],
    )


def _is_cell_metadata_column(column: exp.Column, field: str) -> bool:
    return (
        str(column.this or "").lower() == field
        and str(column.args.get("table") or "").lower() == "metadata"
        and bool(str(column.args.get("db") or "").strip())
    )


def _has_aggregate_ancestor(expression: exp.Expression) -> bool:
    parent = expression.parent
    while parent is not None:
        if _is_aggregate_expression(parent):
            return True
        parent = parent.parent
    return False


def _aggregate_projection_errors(expression: exp.Expression) -> exp.Expression:
    aggregated = _aggregate_unaggregated_metadata_fields(expression, field="errors")
    if _contains_non_window_aggregate(aggregated):
        return aggregated
    return _aggregate_error_expression(aggregated)


def _is_empty_array(expression: exp.Expression | None) -> bool:
    if isinstance(expression, exp.Array):
        return not list(expression.expressions or [])
    if isinstance(expression, exp.Cast):
        return _is_empty_array(expression.this)
    return False


def _semantic_decision_value(cell, clause_name: str) -> exp.Expression:
    errors = cell.error_sql
    if errors is None or _is_empty_array(errors):
        return cell.value_sql
    check_sql = _parse_scalar(
        f"assert_true(size({errors.sql(dialect='spark')}) = 0, "
        f"'Wrapped-mode {clause_name} consumed an errored cell') IS NULL"
    )
    return exp.Case(
        ifs=[exp.If(this=check_sql, true=cell.value_sql.copy())],
        default=cell.value_sql.copy(),
    )


def _parse_scalar(sql_text: str) -> exp.Expression:
    return _parse_scalar_cached(sql_text).copy()


@lru_cache(maxsize=4096)
def _parse_scalar_cached(sql_text: str) -> exp.Expression:
    import sqlglot

    parsed = sqlglot.parse_one(f"SELECT {sql_text}", read="spark")
    return parsed.expressions[0]


def _is_star_projection(projection: exp.Expression) -> bool:
    if isinstance(projection, exp.Star):
        return True
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return True
    return False


def _select_reads_raw_values(query: exp.Select) -> bool:
    from_expr = query.args.get("from_")
    return bool(isinstance(from_expr, exp.From) and isinstance(from_expr.this, exp.Values))


def _expand_raw_values_star_projections(query: exp.Select) -> list[exp.Expression]:
    projections = list(query.expressions or [])
    if not any(_is_star_projection(projection) for projection in projections):
        return projections
    if not _select_reads_raw_values(query):
        return projections

    from_expr = query.args.get("from_")
    values_expr = from_expr.this if isinstance(from_expr, exp.From) else None
    alias = values_expr.args.get("alias") if isinstance(values_expr, exp.Values) else None
    columns = list(alias.args.get("columns") or []) if isinstance(alias, exp.TableAlias) else []
    if not columns:
        return projections

    expanded: list[exp.Expression] = []
    for projection in projections:
        if _is_star_projection(projection):
            expanded.extend(exp.column(column.name) for column in columns if column.name)
        else:
            expanded.append(projection)
    return expanded


def infer_statement_variant_outputs(
    statement: StatementIR,
    registry: FunctionRegistry,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> Set[str]:
    if not isinstance(statement, (BatchTableStmt, StreamTableStmt)) or statement.query is None:
        return set()
    visible_variant_columns = _statement_query_variant_columns(statement, registry=registry, variant_columns=variant_columns)
    lowered = lower_expr(statement.query, registry=registry, variant_columns=visible_variant_columns)
    return _infer_query_variant_outputs(lowered, set(visible_variant_columns), registry=registry)


def _infer_query_variant_outputs(
    expression: exp.Expression,
    visible_variant_columns: Set[str],
    *,
    registry: FunctionRegistry,
) -> Set[str]:
    if isinstance(expression, exp.SetOperation):
        discovered: Set[str] = set()
        if isinstance(expression.this, exp.Expression):
            discovered.update(_infer_query_variant_outputs(expression.this, visible_variant_columns, registry=registry))
        if isinstance(expression.expression, exp.Expression):
            discovered.update(
                _infer_query_variant_outputs(expression.expression, visible_variant_columns, registry=registry)
            )
        return discovered
    if not isinstance(expression, exp.Select):
        return set()
    discovered: Set[str] = set()
    for projection in list(expression.expressions or []):
        alias_name = _projection_alias_name(projection)
        if alias_name is None:
            continue
        projection_expr = projection.this if isinstance(projection, exp.Alias) else projection
        if is_variant_expression(projection_expr, variant_columns=visible_variant_columns):
            discovered.add(alias_name)
    return discovered


def _statement_query_variant_columns(
    statement: StatementIR,
    *,
    registry: FunctionRegistry,
    variant_columns: Optional[Set[str]] = None,
) -> Set[str]:
    visible_variant_columns = set(variant_columns or set())
    if isinstance(statement, (BatchTableStmt, StreamTableStmt)) and isinstance(statement.query, SqlAstExpr):
        visible_variant_columns.update(
            _query_variant_columns(statement.query.expression, visible_variant_columns, registry=registry)
        )
    return visible_variant_columns


def _query_variant_columns(
    expression: exp.Expression,
    variant_columns: Set[str],
    *,
    registry: FunctionRegistry,
) -> Set[str]:
    discovered = set(variant_columns)
    if isinstance(expression, exp.Select):
        with_clause = expression.args.get("with") or expression.args.get("with_")
        if isinstance(with_clause, exp.With):
            for cte in list(with_clause.expressions or []):
                if isinstance(cte, exp.CTE) and isinstance(cte.this, exp.Expression):
                    cte_discovered = _query_variant_columns(cte.this, discovered, registry=registry)
                    discovered.update(cte_discovered)
                    cte_name = str(cte.alias_or_name or "").strip().lower()
                    if cte_name and isinstance(cte.this, exp.Select):
                        for column in _select_variant_projection_aliases(
                            cte.this,
                            cte_discovered,
                            registry=registry,
                        ):
                            discovered.add(f"{cte_name}.{column}")
        discovered.update(_query_table_alias_variant_columns(expression, discovered))
        for projection in list(expression.expressions or []):
            alias_name = _projection_alias_name(projection)
            if alias_name is None:
                continue
            projection_expr = projection.this if isinstance(projection, exp.Alias) else projection
            lowered_projection = lower_expr(
                SqlAstExpr(projection_expr.copy()),
                registry=registry,
                variant_columns=discovered,
            )
            if is_variant_expression(lowered_projection, variant_columns=discovered):
                discovered.add(alias_name)
        return discovered
    if isinstance(expression, exp.SetOperation):
        if isinstance(expression.this, exp.Expression):
            discovered.update(_query_variant_columns(expression.this, discovered, registry=registry))
        if isinstance(expression.expression, exp.Expression):
            discovered.update(_query_variant_columns(expression.expression, discovered, registry=registry))
    return discovered


def _select_variant_projection_aliases(
    select: exp.Select,
    variant_columns: Set[str],
    *,
    registry: FunctionRegistry,
) -> Set[str]:
    discovered: Set[str] = set()
    visible_variant_columns = set(variant_columns)
    for projection in list(select.expressions or []):
        alias_name = _projection_alias_name(projection) or projection.output_name
        alias_name = alias_name.lower() if alias_name else None
        if alias_name is None:
            continue
        projection_expr = projection.this if isinstance(projection, exp.Alias) else projection
        lowered_projection = lower_expr(
            SqlAstExpr(projection_expr.copy()),
            registry=registry,
            variant_columns=visible_variant_columns,
        )
        if is_variant_expression(lowered_projection, variant_columns=visible_variant_columns):
            discovered.add(alias_name)
            visible_variant_columns.add(alias_name)
    return discovered


def _query_table_alias_variant_columns(select: exp.Select, variant_columns: Set[str]) -> Set[str]:
    discovered: Set[str] = set()
    for table in select.find_all(exp.Table):
        table_name = str(table.name or "").strip().lower()
        alias = str(table.alias or "").strip().lower()
        if not table_name:
            continue
        prefix = f"{table_name}."
        for column in variant_columns:
            if column.startswith(prefix):
                unqualified = column[len(prefix):]
                if alias:
                    discovered.add(f"{alias}.{unqualified}")
                else:
                    discovered.add(unqualified)
    return discovered


def _projection_alias_name(projection: exp.Expression) -> Optional[str]:
    if isinstance(projection, exp.Alias):
        alias_name = projection.alias_or_name
        if alias_name:
            return alias_name.lower()
    return None


def _projection_output_name(projection: exp.Expression) -> str:
    alias_name = _projection_alias_name(projection)
    if alias_name:
        return alias_name
    output_name = projection.output_name
    if output_name:
        return output_name.lower()
    return projection.sql(dialect="spark").lower()
