from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlglot import expressions as exp

from agentcicd.sql.ir.statements import BatchTableStmt, LoadStmt, StatementIR, StreamTableStmt


@dataclass(frozen=True)
class WrappedValidationError(ValueError):
    message: str
    construct: str | None = None
    rewrite: str | None = None

    def __str__(self) -> str:
        parts = [self.message]
        if self.construct:
            parts.append(f"construct={self.construct}")
        if self.rewrite:
            parts.append(f"rewrite={self.rewrite}")
        return " | ".join(parts)


def validate_wrapped_statements(statements: Iterable[StatementIR]) -> None:
    for statement in statements:
        validate_wrapped_statement(statement)


def validate_wrapped_statement(statement: StatementIR) -> None:
    if isinstance(statement, LoadStmt):
        _validate_load_options(statement)
        return
    if isinstance(statement, (BatchTableStmt, StreamTableStmt)) and statement.query is not None:
        expression = getattr(statement.query, "expression", None)
        if isinstance(expression, exp.Expression):
            validate_wrapped_query_ast(expression)


def validate_wrapped_query_ast(expression: exp.Expression) -> None:
    expression_sql = expression.sql(dialect="spark").lower()
    if " rows between " in expression_sql or " range between " in expression_sql:
        _unsupported("window frame", "Use supported window functions without explicit frames.")
    for node in expression.walk():
        if _is_instance(node, "Pivot"):
            _unsupported("PIVOT", "Rewrite as explicit GROUP BY aggregates before wrapped execution.")
        if _is_instance(node, "Unpivot"):
            _unsupported("UNPIVOT", "Rewrite as explicit UNION ALL branches.")
        if _is_instance(node, "Lateral"):
            _unsupported("LATERAL VIEW", "Use projection generators such as SELECT explode(col) AS x or posexplode(col) AS (pos, x).")
        if isinstance(node, exp.Subquery) and _is_scalar_subquery(node):
            _unsupported("scalar subquery", "Materialize the subquery as a CTE/table and join explicitly.")
        if node.__class__.__name__.lower() == "qualify":
            _unsupported("QUALIFY", "Move the window query into a CTE and filter in an outer SELECT.")
        if isinstance(node, exp.Join):
            if node.args.get("using"):
                _unsupported("JOIN ... USING", "Use JOIN ... ON left.key = right.key.")
            method = str(node.args.get("method") or "").lower()
            side = str(node.args.get("side") or "").lower()
            kind = str(node.args.get("kind") or "").lower()
            if method == "natural" or kind == "natural":
                _unsupported("NATURAL JOIN", "Use explicit JOIN ... ON predicates.")
            if kind in {"semi", "anti", "cross"} or side in {"semi", "anti", "cross"}:
                _unsupported(f"{kind or side} join", "Use explicit INNER/LEFT/RIGHT/FULL JOIN ... ON.")
            join_on = node.args.get("on")
            if not isinstance(join_on, exp.Expression):
                _unsupported("join without ON", "Use an explicit JOIN ... ON predicate.")
            if not _is_supported_join_predicate(join_on):
                _unsupported("non-equi join", "Use explicit equality predicates joined with AND.")
        if _is_instance(node, "Rollup", "Cube", "GroupingSets"):
            _unsupported(node.__class__.__name__.upper(), "Use explicit GROUP BY queries.")
        if isinstance(node, exp.Column) and _is_physical_cell_field_access(node):
            _unsupported("physical cell field access", "Use value SQL directly or wrapped helpers such as is_err(...).")


def _validate_load_options(statement: LoadStmt) -> None:
    for key in ("wrap", "wrap_cells"):
        value = statement.options.get(key)
        if value is not None and str(value).strip().lower() in {"0", "false", "no", "off", "raw"}:
            raise WrappedValidationError(
                f"Wrapped mode always wraps loaded columns; LOAD option {key.upper()}={value!s} is unsupported.",
                construct=f"LOAD WITH {key.upper()}=false",
                rewrite=f"Remove {key.upper()} or run without include_cells.",
            )


def _unsupported(construct: str, rewrite: str) -> None:
    raise WrappedValidationError(
        f"Unsupported wrapped-mode SQL construct: {construct}.",
        construct=construct,
        rewrite=rewrite,
    )


def _is_scalar_subquery(node: exp.Subquery) -> bool:
    parent = node.parent
    return parent is not None and not isinstance(parent, (exp.From, exp.Join, exp.CTE))


def _is_physical_cell_field_access(node: exp.Column) -> bool:
    parts = [str(part.name if isinstance(part, exp.Identifier) else part).strip().lower() for part in node.parts]
    if len(parts) >= 2 and parts[-1] == "__agentcicd_cell":
        return True
    return len(parts) >= 3 and parts[-2:] in (["metadata", "error"], ["metadata", "errors"])


def _is_supported_join_predicate(node: exp.Expression) -> bool:
    if isinstance(node, exp.EQ):
        return True
    if isinstance(node, exp.And):
        return _is_supported_join_predicate(node.this) and _is_supported_join_predicate(node.expression)
    return False


def _is_instance(node: exp.Expression, *class_names: str) -> bool:
    return node.__class__.__name__ in set(class_names)
