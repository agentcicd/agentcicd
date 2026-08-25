from __future__ import annotations

from dataclasses import dataclass
from typing import List

from agentcicd.sql.ir.expressions import CallExpr, ExprIR, KeywordArgExpr
from agentcicd.sql.ir.functions import FunctionParameterIR


@dataclass
class BoundArgument:
    parameter: FunctionParameterIR
    value: ExprIR
    supplied: bool = True


def bind_function_arguments(call: CallExpr, parameters: List[FunctionParameterIR]) -> List[BoundArgument]:
    remaining = {parameter.name.lower(): parameter for parameter in parameters}
    bound_by_name: dict[str, BoundArgument] = {}
    positional_index = 0
    saw_keyword_argument = False

    for argument in call.args:
        if isinstance(argument, KeywordArgExpr):
            normalized_name = argument.name.lower()
            if normalized_name in bound_by_name:
                raise ValueError(
                    f"Duplicate argument '{argument.name}' for function '{call.function_name}'"
                )
            saw_keyword_argument = True
            parameter = remaining.pop(normalized_name, None)
            if parameter is None:
                raise ValueError(
                    f"Invalid keyword argument '{argument.name}' for function '{call.function_name}'"
                )
            bound_by_name[normalized_name] = BoundArgument(parameter=parameter, value=argument.value)
            continue

        if saw_keyword_argument:
            raise ValueError(
                f"Positional argument cannot follow keyword binding in '{call.function_name}'"
            )
        if positional_index >= len(parameters):
            raise ValueError(f"Too many positional arguments for function '{call.function_name}'")
        parameter = parameters[positional_index]
        if parameter.name.lower() not in remaining:
            raise ValueError(
                f"Positional argument cannot follow keyword binding for '{parameter.name}' in '{call.function_name}'"
            )
        normalized_name = parameter.name.lower()
        remaining.pop(normalized_name, None)
        bound_by_name[normalized_name] = BoundArgument(parameter=parameter, value=argument)
        positional_index += 1

    bound: list[BoundArgument] = []
    for parameter in parameters:
        normalized_name = parameter.name.lower()
        existing = bound_by_name.get(normalized_name)
        if existing is not None:
            bound.append(existing)
            continue
        if parameter.has_default and parameter.default_value is not None:
            bound.append(BoundArgument(parameter=parameter, value=parameter.default_value, supplied=False))
            continue
        raise ValueError(
            f"Missing required argument '{parameter.name}' for function '{call.function_name}'"
        )

    return bound
