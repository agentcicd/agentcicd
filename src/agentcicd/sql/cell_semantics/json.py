from __future__ import annotations

import re
from typing import Optional, Sequence, Set

from sqlglot import expressions as exp

from agentcicd.sql.parsing.runtime_signature_registry import get_runtime_signature


_SIMPLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def function_name(expression: exp.Expression) -> str:
    if isinstance(expression, exp.Dot) and isinstance(expression.expression, exp.Anonymous):
        namespace_parts = _dot_namespace_parts(expression.this)
        if namespace_parts:
            return ".".join([*namespace_parts, expression.expression.name]).lower()
    if isinstance(expression, exp.Anonymous):
        return expression.name.lower()
    if isinstance(expression, exp.Func):
        sql_name = expression.sql_name()
        if sql_name:
            return sql_name.lower()
    return ""


def _dot_namespace_parts(expression: exp.Expression) -> list[str]:
    if isinstance(expression, exp.Identifier):
        return [expression.this]
    if isinstance(expression, exp.Dot) and isinstance(expression.expression, exp.Identifier):
        left = _dot_namespace_parts(expression.this)
        if not left:
            return []
        return [*left, expression.expression.this]
    return []


def explicit_parse_json(
    expression: exp.Expression,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    if isinstance(expression, exp.ParseJSON):
        base = expression.this.copy() if isinstance(expression.this, exp.Expression) else exp.Null()
        return lower_parse_json(base, variant_columns=variant_columns)
    return expression.copy()


def lower_parse_json(
    base: exp.Expression,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    normalized_base = base.copy()
    if is_variant_expression(normalized_base, variant_columns=variant_columns):
        normalized_base = exp.Cast(this=normalized_base, to=exp.DataType.build("STRING"))
    lowered = exp.Anonymous(this="PARSE_JSON", expressions=[normalized_base])
    lowered.meta["agentcicd_variant_access"] = True
    return lowered


def lower_variant_array_for_collection_size(
    expression: exp.Expression,
    *,
    variant_columns: Optional[Set[str]] = None,
    force: bool = False,
) -> Optional[exp.Expression]:
    normalized = explicit_parse_json(expression, variant_columns=variant_columns)
    if not force and not is_variant_expression(normalized, variant_columns=variant_columns):
        return None
    return exp.Anonymous(
        this="FROM_JSON",
        expressions=[
            exp.Anonymous(this="TO_JSON", expressions=[normalized.copy()]),
            exp.Literal.string("array<variant>"),
        ],
    )


def is_variant_expression(
    expression: exp.Expression,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> bool:
    normalized = explicit_parse_json(expression, variant_columns=variant_columns)
    if normalized.meta.get("agentcicd_variant_access"):
        return True
    if isinstance(normalized, exp.Column):
        if variant_columns is not None and _column_matches_variant_output(normalized, variant_columns):
            return True
    name = function_name(normalized)
    if name in {"parse_json", "variant_get", "try_variant_get", "to_variant_object"}:
        return True
    if name:
        signature = get_runtime_signature(name)
        if signature is not None and signature.returns_json:
            return True
    return False


def _column_matches_variant_output(column: exp.Column, variant_columns: Set[str]) -> bool:
    column_name = column.sql(dialect="spark").lower()
    candidates = {column_name}
    if column_name.endswith(".value"):
        candidates.add(column_name[: -len(".value")])

    return any(candidate in variant_columns for candidate in candidates)


def json_path_from_segments(path: Sequence[str | int]) -> str:
    json_path = "$"
    for segment in path:
        if isinstance(segment, int):
            json_path += f"[{segment}]"
        else:
            json_path += f".{segment}"
    return json_path


def json_path_from_index_expression(expression: exp.Expression) -> Optional[str]:
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            key = expression.this
            if _SIMPLE_IDENTIFIER_RE.match(key):
                return f".{key}"
            escaped = key.replace("\\", "\\\\").replace("'", "\\'")
            return f"['{escaped}']"
        return f"[{expression.this}]"
    if isinstance(expression, exp.Neg) and isinstance(expression.this, exp.Literal) and not expression.this.is_string:
        return f"[{expression.sql(dialect='spark')}]"
    return None


def lower_bracket_json_access(
    expression: exp.Bracket,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> Optional[exp.Expression]:
    path_parts: list[str] = []
    current: exp.Expression = expression
    while isinstance(current, exp.Bracket):
        indexes = list(current.expressions or [])
        if len(indexes) != 1:
            return None
        path_part = json_path_from_index_expression(indexes[0])
        if path_part is None:
            return None
        path_parts.insert(0, path_part)
        if not isinstance(current.this, exp.Expression):
            return None
        current = current.this

    if not path_parts or not is_variant_expression(current, variant_columns=variant_columns):
        return None
    return lower_json_access(current.copy(), "$" + "".join(path_parts), variant_columns=variant_columns)


def lower_safe_array_access(expression: exp.Bracket) -> Optional[exp.Expression]:
    indexes = list(expression.expressions or [])
    if len(indexes) != 1:
        return None
    if isinstance(indexes[0], exp.Literal) and indexes[0].is_string:
        return None
    if not isinstance(expression.this, exp.Expression):
        return None
    return exp.Anonymous(
        this="GET",
        expressions=[expression.this.copy(), indexes[0].copy()],
    )


def lower_dynamic_variant_object_access(
    expression: exp.Bracket,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> Optional[exp.Expression]:
    indexes = list(expression.expressions or [])
    if len(indexes) != 1:
        return None
    if isinstance(indexes[0], exp.Literal):
        return None
    if not isinstance(expression.this, exp.Expression):
        return None
    if not is_variant_expression(expression.this, variant_columns=variant_columns):
        return None

    object_map = exp.Anonymous(
        this="FROM_JSON",
        expressions=[
            exp.Anonymous(this="TO_JSON", expressions=[expression.this.copy()]),
            exp.Literal.string("map<string,variant>"),
        ],
    )
    lowered = exp.Anonymous(
        this="ELEMENT_AT",
        expressions=[
            object_map,
            exp.Cast(this=indexes[0].copy(), to=exp.DataType.build("STRING")),
        ],
    )
    lowered.meta["agentcicd_variant_access"] = True
    lowered.meta["agentcicd_dynamic_variant_access"] = True
    return lowered


def lower_tolerant_get_access(
    expression: exp.Expression,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> Optional[exp.Expression]:
    if function_name(expression) != "get":
        return None
    args = list(expression.expressions or [])
    if len(args) != 2 or not isinstance(args[0], exp.Expression) or not isinstance(args[1], exp.Expression):
        return None
    if not is_variant_expression(args[0], variant_columns=variant_columns):
        return None
    path_part = json_path_from_index_expression(args[1])
    if path_part is None:
        return None
    lowered = lower_json_access(args[0].copy(), "$" + path_part, variant_columns=variant_columns)
    lowered.meta["agentcicd_tolerant_variant_access"] = True
    return lowered


def lower_json_access(
    base: exp.Expression,
    json_path: str,
    *,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    normalized = explicit_parse_json(base, variant_columns=variant_columns)
    if is_variant_expression(normalized, variant_columns=variant_columns):
        lowered = exp.Anonymous(
            this="TRY_VARIANT_GET",
            expressions=[normalized.copy(), exp.Literal.string(json_path)],
        )
        lowered.meta["agentcicd_variant_access"] = True
        return lowered
    raise ValueError(
        "JSON path access requires a json_variant value; use parse_json(...) on JSON strings before applying :path or [] access."
    )
