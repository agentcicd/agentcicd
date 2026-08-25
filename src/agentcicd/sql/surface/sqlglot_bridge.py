from __future__ import annotations

from typing import List

from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import CallExpr, ColumnRefExpr, ExprIR, KeywordArgExpr, LiteralExpr, SqlAstExpr, VariantPathExpr


def expression_to_ir(expression: exp.Expression) -> ExprIR:
    if isinstance(expression, exp.Column):
        return ColumnRefExpr(name=expression.sql(dialect="spark"))
    if isinstance(expression, exp.Literal):
        if expression.args.get("is_string"):
            return LiteralExpr(value=expression.this)
        text = str(expression.this)
        try:
            if "." in text:
                return LiteralExpr(value=float(text))
            return LiteralExpr(value=int(text))
        except ValueError:
            return LiteralExpr(value=text)
    namespaced_call = _extract_namespaced_call(expression)
    if namespaced_call is not None:
        function_name, call_expression = namespaced_call
        return CallExpr(
            function_name=function_name,
            args=_function_args_to_ir(list(call_expression.expressions or [])),
        )
    if isinstance(expression, exp.Anonymous):
        if expression.name.lower() == "__agentcicd_variant_path" and list(expression.expressions or []):
            raw_args = list(expression.expressions or [])
            base = expression_to_ir(raw_args[0])
            path: list[str | int] = []
            for item in raw_args[1:]:
                if isinstance(item, exp.Literal) and not item.args.get("is_string"):
                    path.append(int(item.this))
                elif isinstance(item, exp.Literal):
                    path.append(str(item.this))
                else:
                    raise ValueError(f"Unsupported variant path segment: {item.sql(dialect='spark')}")
            return VariantPathExpr(base=base, path=path)
        return CallExpr(
            function_name=expression.name,
            args=_function_args_to_ir(list(expression.expressions or [])),
        )
    if isinstance(expression, exp.Func):
        return SqlAstExpr(expression=expression)
    return SqlAstExpr(expression=expression)


def _function_args_to_ir(arguments: List[exp.Expression]) -> List[ExprIR]:
    args: List[ExprIR] = []
    for argument in arguments:
        if isinstance(argument, exp.EQ) and isinstance(argument.this, exp.Column):
            args.append(
                KeywordArgExpr(
                    name=argument.this.sql(dialect="spark"),
                    value=expression_to_ir(argument.expression),
                )
            )
        else:
            args.append(expression_to_ir(argument))
    return args


def _func_args_to_list(function_expression: exp.Func) -> List[exp.Expression]:
    arguments: List[exp.Expression] = []
    for argument in function_expression.iter_expressions():
        if isinstance(argument, exp.Expression):
            arguments.append(argument)
    return arguments


def _extract_namespaced_call(expression: exp.Expression) -> tuple[str, exp.Anonymous] | None:
    if not isinstance(expression, exp.Dot) or not isinstance(expression.expression, exp.Anonymous):
        return None
    namespace_parts = _dot_namespace_parts(expression.this)
    if not namespace_parts:
        return None
    return ".".join([*namespace_parts, expression.expression.name]), expression.expression


def _dot_namespace_parts(expression: exp.Expression) -> List[str]:
    if isinstance(expression, exp.Identifier):
        return [expression.this]
    if isinstance(expression, exp.Dot) and isinstance(expression.expression, exp.Identifier):
        left = _dot_namespace_parts(expression.this)
        if not left:
            return []
        return [*left, expression.expression.this]
    return []
