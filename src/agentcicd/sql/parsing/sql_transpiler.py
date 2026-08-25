from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Optional, Set

from sqlglot import expressions as exp
from sqlglot import parse_one

from agentcicd.sql.json_semantics import (
    explicit_parse_json,
    function_name,
    is_variant_expression,
    json_path_from_index_expression,
    lower_json_access,
    lower_parse_json,
)
from agentcicd.sql.parsing.function_args import (
    is_keyword_argument_target,
    keyword_argument_name,
)
from agentcicd.sql.parsing.runtime_signature_registry import (
    RuntimeFunctionSignature,
    get_runtime_signature,
)


_NULL_STRING = "CAST(NULL AS STRING)"
_NULL_METADATA_SQL = "named_struct('error', CAST(NULL AS STRING), 'subdatatype', CAST(NULL AS STRING))"
@dataclass(frozen=True)
class _BuiltinUdfSignature:
    runtime_alias: str
    input_args: tuple[str, ...]
    has_default_by_name: dict[str, bool]
    returns_json: bool = False


@dataclass(frozen=True)
class _WrappedSelectContext:
    where_predicate: Optional[str]
    join_predicates: List[str]
    non_cell_columns: Set[str]


def transpile_query_expression(expression: exp.Expression) -> exp.Expression:
    """Transpile SQL AST to wrapped-cell aware Spark SQL AST."""
    return transpile_query_expression_with_options(expression)


def normalize_sql_function_expression(expression: exp.Expression) -> exp.Expression:
    """Normalize SQL function bodies without applying wrapped-cell query semantics."""
    return _normalize_sql_function_expression_with_scope(expression.copy())


def normalize_sql_function_step_expression(
    expression: exp.Expression,
    *,
    variant_columns: Set[str],
) -> exp.Expression:
    """Normalize a single SQL-function step using the current local variant scope."""
    return _normalize_select_like_expression(expression.copy(), variant_columns=set(variant_columns))


def transpile_query_expression_with_options(
    expression: exp.Expression,
) -> exp.Expression:
    """Transpile SQL AST to wrapped-cell aware Spark SQL AST."""
    normalized_expression = _normalize_colon_path_calls(expression)
    return normalized_expression.transform(
        _transform_select,
        copy=True,
    )


def _normalize_colon_path_calls(
    expression: exp.Expression,
    *,
    unwrap_columns: bool = True,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    def _transform(node: exp.Expression) -> exp.Expression:
        if function_name(node) != "__agentcicd_colon_path":
            return node
        base = node.expressions[0] if len(node.expressions) > 0 else None
        path = node.expressions[1] if len(node.expressions) > 1 else None
        if base is None or not isinstance(path, exp.Literal) or not path.is_string:
            return node

        rewritten_base = (
            _unwrap_columns_to_value(base, skip_python_udf_columns=False)
            if unwrap_columns
            else base.copy()
        )
        return lower_json_access(rewritten_base, path.this, variant_columns=variant_columns)

    rewritten = expression
    for _ in range(8):
        next_expression = rewritten.transform(_transform, copy=True)
        if next_expression.sql(dialect="spark") == rewritten.sql(dialect="spark"):
            return next_expression
        rewritten = next_expression
    return rewritten


def _normalize_variant_access(
    expression: exp.Expression,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Bracket):
            return _rewrite_variant_bracket(node, variant_columns=variant_columns)
        return node

    rewritten = expression
    for _ in range(8):
        next_expression = rewritten.transform(_transform, copy=True)
        if next_expression.sql(dialect="spark") == rewritten.sql(dialect="spark"):
            return next_expression
        rewritten = next_expression
    return rewritten


def _rewrite_variant_bracket(
    node: exp.Bracket,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    base = node.this
    bracket_args = list(node.expressions or [])
    if base is None or len(bracket_args) != 1:
        return node

    index_expression = bracket_args[0]
    if isinstance(index_expression, exp.Slice):
        if not is_variant_expression(base, variant_columns=variant_columns):
            return node
        return _rewrite_variant_slice(base, index_expression)

    if not is_variant_expression(base, variant_columns=variant_columns):
        return node

    json_path = json_path_from_index_expression(index_expression)
    if json_path is None:
        return node
    return lower_json_access(base, f"${json_path}", variant_columns=variant_columns)


def _normalize_sql_function_expression_with_scope(expression: exp.Expression) -> exp.Expression:
    return _normalize_expr_with_variant_scope(expression, variant_columns=set())


def _normalize_expr_with_variant_scope(
    expression: exp.Expression,
    *,
    variant_columns: Set[str],
) -> exp.Expression:
    with_clause = expression.args.get("with") or expression.args.get("with_")
    if isinstance(with_clause, exp.With):
        current_variant_columns = set(variant_columns)
        rewritten_ctes: list[exp.Expression] = []
        for cte in with_clause.expressions or []:
            if not isinstance(cte, exp.CTE):
                rewritten_ctes.append(
                    _normalize_expr_with_variant_scope(cte.copy(), variant_columns=current_variant_columns)
                )
                continue
            rewritten_cte_body = _normalize_select_like_expression(
                cte.this.copy(),
                variant_columns=current_variant_columns,
            )
            rewritten_cte = cte.copy()
            rewritten_cte.set("this", rewritten_cte_body)
            rewritten_ctes.append(rewritten_cte)
            current_variant_columns.update(_variant_output_columns(rewritten_cte_body, current_variant_columns))

        rewritten = expression.copy()
        rewritten_with = with_clause.copy()
        rewritten_with.set("expressions", rewritten_ctes)
        rewritten.set("with_", rewritten_with)
        if isinstance(rewritten, exp.Select):
            return _normalize_select_like_expression(rewritten, variant_columns=current_variant_columns)
        body = rewritten.this
        if isinstance(body, exp.Expression):
            rewritten.set("this", _normalize_expr_with_variant_scope(body.copy(), variant_columns=current_variant_columns))
        return rewritten
    return _normalize_select_like_expression(expression, variant_columns=variant_columns)


def _normalize_select_like_expression(
    expression: exp.Expression,
    *,
    variant_columns: Set[str],
) -> exp.Expression:
    rewritten = _normalize_colon_path_calls(
        expression.copy(),
        unwrap_columns=False,
        variant_columns=variant_columns,
    )
    rewritten = _normalize_variant_access(rewritten, variant_columns=variant_columns)
    return rewritten


def _variant_output_columns(expression: exp.Expression, variant_columns: Set[str]) -> Set[str]:
    if not isinstance(expression, exp.Select):
        return set()
    outputs: Set[str] = set()
    for projection in expression.expressions or []:
        alias_name = _projection_alias_name(projection)
        if alias_name is None:
            continue
        projection_expr = projection.this if isinstance(projection, exp.Alias) else projection
        if is_variant_expression(projection_expr, variant_columns=variant_columns):
            outputs.add(alias_name)
    return outputs


def _projection_alias_name(projection: exp.Expression) -> Optional[str]:
    if isinstance(projection, exp.Alias):
        alias_name = projection.alias_or_name
        if alias_name:
            return alias_name.lower()
    return None


def _rewrite_variant_slice(base: exp.Expression, slice_expression: exp.Slice) -> exp.Expression:
    array_sql = f"from_json(to_json({base.sql(dialect='spark')}), 'array<variant>')"
    size_sql = f"size({array_sql})"
    start_sql = _slice_bound_sql(slice_expression.this, size_sql, is_end=False)
    end_sql = _slice_bound_sql(slice_expression.expression, size_sql, is_end=True)
    normalized_start_sql = f"greatest({start_sql}, 0)"
    normalized_end_sql = f"least(greatest({end_sql}, 0), {size_sql})"
    length_sql = f"greatest(({normalized_end_sql}) - ({normalized_start_sql}), 0)"
    rewritten = _parse_sql(
        "CASE "
        f"WHEN {array_sql} IS NULL THEN NULL "
        f"ELSE to_variant_object(slice({array_sql}, ({normalized_start_sql}) + 1, {length_sql})) "
        "END"
    )
    rewritten.meta["agentcicd_variant_access"] = True
    return rewritten


def _slice_bound_sql(bound: Optional[exp.Expression], size_sql: str, *, is_end: bool) -> str:
    if bound is None:
        return size_sql if is_end else "0"
    integer_value = _literal_integer(bound)
    if integer_value is None:
        raise ValueError("Variant slice bounds must be integer literals.")
    if integer_value >= 0:
        return str(integer_value)
    return f"({size_sql}) + ({integer_value})"


def _literal_integer(expression: exp.Expression) -> Optional[int]:
    if isinstance(expression, exp.Literal) and not expression.is_string:
        return int(expression.this)
    if isinstance(expression, exp.Neg) and isinstance(expression.this, exp.Literal) and not expression.this.is_string:
        return -int(expression.this.this)
    return None


def _transform_select(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.Select):
        return node

    select = node.copy()
    context = _WrappedSelectContext(
        where_predicate=_select_where_sql(node),
        join_predicates=_select_join_predicates_sql(node),
        non_cell_columns=_select_subquery_output_columns(node),
    )

    where = select.args.get("where")
    if isinstance(where, exp.Where):
        where.set(
            "this",
            _rewrite_condition(
                where.this,
                throw_on_error=False,
                aggregate_context=False,
                non_cell_columns=context.non_cell_columns,
            ),
        )

    having = select.args.get("having")
    if isinstance(having, exp.Having):
        # HAVING runs post-aggregation; error merging must be aggregate-aware.
        having.set(
            "this",
            _rewrite_condition(
                having.this,
                throw_on_error=False,
                aggregate_context=True,
                non_cell_columns=context.non_cell_columns,
            ),
        )

    joins = select.args.get("joins") or []
    rewritten_joins: List[exp.Expression] = []
    for join in joins:
        if isinstance(join, exp.Join):
            on = join.args.get("on")
            if on is not None:
                join = join.copy()
                join.set(
                    "on",
                    _rewrite_condition(
                        on,
                        throw_on_error=True,
                        aggregate_context=False,
                        non_cell_columns=context.non_cell_columns,
                    ),
                )
        rewritten_joins.append(join)
    if rewritten_joins:
        select.set("joins", rewritten_joins)

    rewritten_projections: List[exp.Expression] = []
    for projection in select.expressions:
        rewritten_projections.append(
            _rewrite_projection(
                projection,
                context=context,
            )
        )
    select.set("expressions", rewritten_projections)
    return select


def _select_where_sql(select_node: exp.Select) -> Optional[str]:
    where = select_node.args.get("where")
    if isinstance(where, exp.Where):
        return where.this.sql(dialect="spark")
    return None


def _select_join_predicates_sql(select_node: exp.Select) -> List[str]:
    predicates: List[str] = []
    joins = select_node.args.get("joins") or []
    for join in joins:
        if isinstance(join, exp.Join):
            on = join.args.get("on")
            if on is not None:
                predicates.append(on.sql(dialect="spark"))
    return predicates


def _rewrite_projection(
    projection: exp.Expression,
    *,
    context: _WrappedSelectContext,
) -> exp.Expression:
    if isinstance(projection, exp.Star):
        return projection

    alias_name: Optional[str] = None
    body = projection
    if isinstance(projection, exp.Alias):
        alias_name = projection.alias_or_name
        body = projection.this

    rewritten = _rewrite_projection_body(
        body,
        context=context,
    )
    if alias_name:
        return exp.alias_(rewritten, alias_name, quoted=False)
    return rewritten


def _rewrite_projection_body(
    body: exp.Expression,
    *,
    context: _WrappedSelectContext,
) -> exp.Expression:
    if not _is_python_udf_call(body):
        body = _normalize_nested_python_udf_calls(body)
    if isinstance(body, exp.Column):
        return body

    if _is_python_udf_call(body):
        rewritten_call = _rewrite_python_udf_call(body)
        aggregate_context = _contains_aggregate(body)
        merged_error_sql = _merged_error_sql(
            body,
            aggregate_context=aggregate_context,
            skip_python_udf_columns=False,
            non_cell_columns=context.non_cell_columns,
        )
        value_sql = rewritten_call.sql(dialect="spark")
        wrapped_sql = _wrap_cell_sql(
            f"CASE WHEN ({merged_error_sql}) IS NULL THEN {value_sql} ELSE NULL END",
            merged_error_sql,
        )
        return _parse_sql(wrapped_sql)

    value_expr = _rewrite_embedded_conditions(
        _unwrap_columns_to_value(
            body,
            skip_python_udf_columns=True,
            non_cell_columns=context.non_cell_columns,
        ),
        non_cell_columns=context.non_cell_columns,
    )
    aggregate_context = _contains_aggregate(body)
    merged_error_sql = _merged_error_sql(
        body,
        aggregate_context=aggregate_context,
        skip_python_udf_columns=True,
        non_cell_columns=context.non_cell_columns,
    )
    value_sql = value_expr.sql(dialect="spark")
    wrapped_sql = _wrap_cell_sql(
        f"CASE WHEN ({merged_error_sql}) IS NULL THEN {value_sql} ELSE NULL END",
        merged_error_sql,
    )
    return _parse_sql(wrapped_sql)


def _rewrite_python_udf_call(call: exp.Expression) -> exp.Expression:
    rewritten = _normalize_python_udf_call_arguments(call.copy())
    args = list(rewritten.expressions)
    rewritten_args: List[exp.Expression] = []
    for arg in args:
        rewritten_args.append(_rewrite_python_udf_argument(arg))
    rewritten.set("expressions", rewritten_args)
    return rewritten


def _normalize_nested_python_udf_calls(expression: exp.Expression) -> exp.Expression:
    def _transform(node: exp.Expression) -> exp.Expression:
        if node is expression:
            return node
        if _is_python_udf_call(node):
            return _rewrite_python_udf_call(node)
        return node

    return expression.transform(_transform, copy=True)


def _normalize_python_udf_call_arguments(call: exp.Expression) -> exp.Expression:
    rewritten = call.copy()
    args = list(rewritten.expressions)
    if not any(isinstance(arg, exp.EQ) for arg in args):
        return rewritten

    signature = _builtin_udf_signature_for_call(rewritten)
    if signature is None:
        return rewritten

    ordered_args: List[exp.Expression] = []
    parameter_by_name = {name.lower(): idx for idx, name in enumerate(signature.input_args)}
    bound_args: dict[str, exp.Expression] = {}
    seen_keyword = False
    positional_index = 0

    for arg in args:
        if isinstance(arg, exp.EQ):
            seen_keyword = True
            keyword_name = keyword_argument_name(arg).lower()
            if keyword_name not in parameter_by_name:
                return rewritten
            if keyword_name in bound_args:
                return rewritten
            bound_args[keyword_name] = arg.expression.copy()
            continue

        if seen_keyword:
            return rewritten
        if positional_index >= len(signature.input_args):
            return rewritten
        parameter_name = signature.input_args[positional_index].lower()
        bound_args[parameter_name] = arg.copy()
        positional_index += 1

    missing_required = [
        name
        for name in signature.input_args
        if not signature.has_default_by_name.get(name.lower(), False)
        and name.lower() not in bound_args
    ]
    if missing_required:
        return rewritten

    for name in signature.input_args:
        value = bound_args.get(name.lower())
        ordered_args.append(value.copy() if value is not None else exp.Null())
    rewritten.set("expressions", ordered_args)

    alias = signature.runtime_alias
    if isinstance(rewritten, exp.Anonymous):
        rewritten.set("this", alias)
    elif isinstance(rewritten, exp.Func):
        rewritten = _parse_sql(f"{alias}({', '.join(arg.sql(dialect='spark') for arg in ordered_args)})")
    return rewritten


def _builtin_udf_signature_for_call(call: exp.Expression) -> Optional[_BuiltinUdfSignature]:
    name = function_name(call)
    if name:
        direct = _load_runtime_udf_signature(name)
        if direct is not None:
            return direct
    sql_name = call.sql_name().lower() if isinstance(call, exp.Func) else ""
    if sql_name:
        direct = _load_runtime_udf_signature(sql_name)
        if direct is not None:
            return direct
    return None


def _load_runtime_udf_signature(name: str) -> Optional[_BuiltinUdfSignature]:
    signature = get_runtime_signature(name)
    if signature is None:
        return None
    return _BuiltinUdfSignature(
        runtime_alias=signature.runtime_alias,
        input_args=signature.input_args,
        has_default_by_name=signature.has_default_by_name,
        returns_json=signature.returns_json,
    )


def _rewrite_python_udf_argument(arg: exp.Expression) -> exp.Expression:
    if isinstance(arg, exp.Literal):
        return arg
    if isinstance(arg, exp.Column):
        return _parse_sql(f"{arg.sql(dialect='spark')}.value")
    if _is_python_udf_call(arg):
        return _rewrite_python_udf_call(arg)
    return _unwrap_columns_to_value(arg, skip_python_udf_columns=False)


def _rewrite_expression_inside_python_udf(expression: exp.Expression) -> exp.Expression:
    value_expr = _unwrap_columns_to_value(expression, skip_python_udf_columns=False)
    aggregate_context = _contains_aggregate(expression)
    merged_error_sql = _merged_error_sql(
        expression,
        aggregate_context=aggregate_context,
        skip_python_udf_columns=False,
    )
    value_sql = value_expr.sql(dialect="spark")
    wrapped_sql = _wrap_cell_sql(
        f"CASE WHEN ({merged_error_sql}) IS NULL THEN {value_sql} ELSE NULL END",
        merged_error_sql,
    )
    return _parse_sql(wrapped_sql)


def _rewrite_condition(
    condition: exp.Expression,
    *,
    throw_on_error: bool,
    aggregate_context: bool,
    non_cell_columns: Set[str],
) -> exp.Expression:
    merged_error_sql = _merged_error_sql(
        condition,
        aggregate_context=aggregate_context,
        skip_python_udf_columns=True,
        non_cell_columns=non_cell_columns,
    )
    value_condition = _unwrap_columns_to_value(
        condition,
        skip_python_udf_columns=True,
        non_cell_columns=non_cell_columns,
    ).sql(dialect="spark")
    if throw_on_error:
        sql = f"(assert_true(({merged_error_sql}) IS NULL) IS NULL) AND ({value_condition})"
    else:
        sql = f"(({merged_error_sql}) IS NOT NULL) OR ({value_condition})"
    return _parse_sql(sql)


def _rewrite_embedded_conditions(
    expression: exp.Expression,
    *,
    non_cell_columns: Set[str],
) -> exp.Expression:
    """Rewrite boolean subexpressions embedded inside projections.

    Projection bodies can contain nested boolean logic, most notably CASE WHEN
    conditions. Those predicates still need wrapped-cell semantics even though
    the projection as a whole is not itself a filter/join clause.
    """

    def _replace(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.If):
            rewritten = node.copy()
            rewritten.set(
                "this",
                _rewrite_condition(
                    node.this,
                    throw_on_error=False,
                    aggregate_context=_contains_aggregate(node.this),
                    non_cell_columns=non_cell_columns,
                ),
            )
            return rewritten
        return node

    return expression.transform(_replace, copy=True)


def _unwrap_columns_to_value(
    expression: exp.Expression,
    *,
    skip_python_udf_columns: bool,
    non_cell_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    non_cell_names = non_cell_columns or set()

    def _replace(node: exp.Expression) -> exp.Expression:
        if is_keyword_argument_target(node):
            return node
        if isinstance(node, exp.Column):
            if isinstance(node.parent, exp.Dot):
                return node
            if skip_python_udf_columns and _is_in_python_udf(node):
                return node
            if _column_name(node) in non_cell_names:
                return node
            if _cell_ref_from_column(node) != node.sql(dialect="spark"):
                # Explicit `.value` path already present (e.g. `x.value.y`); do not append `.value` again.
                return node
            return _parse_sql(f"{node.sql(dialect='spark')}.value")
        return node

    return expression.transform(_replace, copy=True)


def _collect_cell_column_refs(
    expression: exp.Expression,
    *,
    skip_python_udf_columns: bool,
    non_cell_columns: Optional[Set[str]] = None,
) -> List[str]:
    non_cell_names = non_cell_columns or set()
    refs: List[str] = []
    seen = set()
    for col in expression.find_all(exp.Column):
        if isinstance(col.parent, exp.Dot):
            continue
        if skip_python_udf_columns and _is_in_python_udf(col):
            continue
        if _column_name(col) in non_cell_names:
            continue
        cell_ref = _cell_ref_from_column(col)
        if cell_ref not in seen:
            seen.add(cell_ref)
            refs.append(cell_ref)
    return refs


def _column_name(column: exp.Column) -> str:
    parts = [part.name for part in column.parts]
    if not parts:
        return column.sql(dialect="spark")
    return parts[-1].lower()


def _cell_ref_from_column(column: exp.Column) -> str:
    """Return the wrapped-cell root reference for a column.

    For explicit `.value` accesses such as `x.value.y`, this returns `x`.
    For table-qualified explicit accesses such as `t.x.value.y`, this returns `t.x`.
    Otherwise it returns the original column SQL.
    """
    parts = [part.name for part in column.parts]
    if not parts:
        return column.sql(dialect="spark")
    try:
        value_idx = parts.index("value")
    except ValueError:
        return column.sql(dialect="spark")
    if value_idx <= 0:
        return column.sql(dialect="spark")
    return ".".join(parts[:value_idx])


def _select_subquery_output_columns(select_node: exp.Select) -> Set[str]:
    names: Set[str] = set()

    def _collect_from_expression(source: Optional[exp.Expression]) -> None:
        if source is None:
            return
        if isinstance(source, exp.Subquery):
            names.update(_query_output_columns(source.this))

    from_clause = select_node.args.get("from") or select_node.args.get("from_")
    if isinstance(from_clause, exp.From):
        _collect_from_expression(from_clause.this)

    for join in select_node.args.get("joins") or []:
        if isinstance(join, exp.Join):
            _collect_from_expression(join.this)

    return names


def _query_output_columns(query: Optional[exp.Expression]) -> Set[str]:
    if query is None:
        return set()
    if isinstance(query, exp.Select):
        out: Set[str] = set()
        for projection in query.expressions:
            alias_name = projection.alias_or_name
            if alias_name:
                out.add(alias_name.lower())
        return out
    if isinstance(query, exp.SetOperation):
        return _query_output_columns(query.this) | _query_output_columns(query.expression)
    return set()


def _merged_error_sql(
    expression: exp.Expression,
    *,
    aggregate_context: bool,
    skip_python_udf_columns: bool,
    non_cell_columns: Optional[Set[str]] = None,
) -> str:
    refs = _collect_cell_column_refs(
        expression,
        skip_python_udf_columns=skip_python_udf_columns,
        non_cell_columns=non_cell_columns,
    )
    if not refs:
        return _NULL_STRING
    if aggregate_context:
        pieces = [f"first({ref}.metadata.error, true)" for ref in refs]
    else:
        pieces = [f"{ref}.metadata.error" for ref in refs]
    if len(pieces) == 1:
        return pieces[0]
    return f"coalesce({', '.join(pieces)})"


def _sql_string_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _is_in_python_udf(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if _is_python_udf_call(parent):
            return True
        parent = parent.parent
    return False


def _is_python_udf_call(node: exp.Expression) -> bool:
    name = function_name(node)
    return bool(
        name
        and name.startswith(
            (
                "aisystems_",
                "envs_",
                "simulators_",
                "ctr_",
                "container_",
                "agent_",
                "data_",
                "http_",
                "ragas_",
                "ranking_",
                "string_",
                "tool_",
                "trajectory_",
                "zip_",
            )
        )
    )


def _contains_aggregate(expression: exp.Expression) -> bool:
    return any(isinstance(node, exp.AggFunc) for node in expression.walk())


def _arg_sql(call: exp.Expression, index: int) -> str:
    args = list(call.expressions)
    if len(args) <= index:
        raise ValueError(f"Function {call.sql(dialect='spark')} expects argument index {index}")
    return args[index].sql(dialect="spark")


def _wrap_cell_sql(
    value_sql: str,
    error_sql: str,
    metadata_sql: Optional[str] = None,
) -> str:
    metadata = metadata_sql or (
        "named_struct("
        f"'error', {error_sql}, "
        "'subdatatype', CAST(NULL AS STRING)"
        ")"
    )
    return f"named_struct('value', {value_sql}, 'metadata', {metadata}, '__agentcicd_cell', true)"


def _parse_sql(sql: str) -> exp.Expression:
    parsed = parse_one(sql, read="spark")
    return parsed.transform(
        lambda node: lower_parse_json(node.this) if isinstance(node, exp.ParseJSON) and isinstance(node.this, exp.Expression) else node,
        copy=True,
    )
