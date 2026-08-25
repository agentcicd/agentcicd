from __future__ import annotations

from typing import Optional

from sqlglot import expressions as exp


def keyword_argument_name(argument: exp.Expression) -> str:
    if not isinstance(argument, exp.EQ):
        raise ValueError("Expected keyword argument")
    target = argument.this
    if isinstance(target, exp.Column):
        table_ref = target.args.get("table")
        if table_ref:
            raise ValueError("Keyword argument names cannot be table-qualified")
        target = target.args.get("this") or target
    if isinstance(target, exp.Identifier):
        return str(target.this).strip()
    var_cls = getattr(exp, "Var", None)
    if var_cls and isinstance(target, var_cls):
        inner = getattr(target, "this", None)
        if isinstance(inner, exp.Identifier):
            return str(inner.this).strip()
        if isinstance(inner, str):
            return inner.strip()
    raise ValueError("Keyword argument names must be identifiers")


def is_keyword_argument_target(node: exp.Expression) -> bool:
    current: Optional[exp.Expression] = node
    while current is not None:
        parent = current.parent
        if isinstance(parent, exp.EQ):
            call = parent.parent
            if not isinstance(call, (exp.Func, exp.Anonymous)):
                return False
            try:
                keyword_argument_name(parent)
            except ValueError:
                return False
            return parent.this is current
        current = parent
    return False
