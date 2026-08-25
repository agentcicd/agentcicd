from __future__ import annotations

from agentcicd.sql.ir.expressions import CallExpr, KeywordArgExpr, LiteralExpr
from agentcicd.sql.ir.functions import FunctionParameterIR
from agentcicd.sql.semantics.arg_binding import bind_function_arguments


def test_bind_function_arguments_preserves_declared_parameter_order_with_defaults() -> None:
    parameters = [
        FunctionParameterIR(name="prompt", type_sql="STRING", has_default=True, default_value=LiteralExpr(value=None)),
        FunctionParameterIR(name="aisystem_id", type_sql="STRING"),
        FunctionParameterIR(name="messages", type_sql="ANY", has_default=True, default_value=LiteralExpr(value=None)),
        FunctionParameterIR(name="response_format", type_sql="ANY", has_default=True, default_value=LiteralExpr(value=None)),
    ]
    call = CallExpr(
        function_name="aisystems.llm.chat",
        args=[
            KeywordArgExpr(name="messages", value=LiteralExpr(value="messages_payload")),
            KeywordArgExpr(name="aisystem_id", value=LiteralExpr(value="aisystem.test")),
            KeywordArgExpr(name="response_format", value=LiteralExpr(value="json_object")),
        ],
    )

    bound = bind_function_arguments(call, parameters)

    assert [item.parameter.name for item in bound] == [
        "prompt",
        "aisystem_id",
        "messages",
        "response_format",
    ]
    assert [getattr(item.value, "value", None) for item in bound] == [
        None,
        "aisystem.test",
        "messages_payload",
        "json_object",
    ]


def test_bind_function_arguments_preserves_positional_then_default_order() -> None:
    parameters = [
        FunctionParameterIR(name="text", type_sql="STRING"),
        FunctionParameterIR(name="model", type_sql="STRING", has_default=True, default_value=LiteralExpr(value="bge")),
        FunctionParameterIR(name="truncate", type_sql="BOOLEAN", has_default=True, default_value=LiteralExpr(value=False)),
    ]
    call = CallExpr(
        function_name="embed",
        args=[LiteralExpr(value="alice")],
    )

    bound = bind_function_arguments(call, parameters)

    assert [item.parameter.name for item in bound] == ["text", "model", "truncate"]
    assert [getattr(item.value, "value", None) for item in bound] == ["alice", "bge", False]
