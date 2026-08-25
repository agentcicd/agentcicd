from __future__ import annotations

import re
from typing import Iterable

import sqlglot
from sqlglot import expressions as exp

from agentcicd.sql.injections import validate_table_executor_pools
from agentcicd.sql.ir.expressions import CallExpr, ColumnRefExpr, KeywordArgExpr, LiteralExpr, SqlAstExpr
from agentcicd.sql.ir.statements import BatchTableStmt, DeclareInputStmt, PublishReportsStmt, StatementIR, StreamTableStmt
from agentcicd.sql.ir.visitors import walk_ir
from agentcicd.sql.pool_inputs import canonical_pool_default_json, pool_kind_from_statement
from agentcicd.sql.semantics.arg_binding import bind_function_arguments
from agentcicd.sql.semantics.registry import FunctionRegistry
from agentcicd.sql.semantics.types import TypeSpec, is_function_type, parse_type_spec
from agentcicd.sql.surface.sqlglot_bridge import expression_to_ir
from agentcicd.sql.surface.top_level_parser import _tokenize_statement


_REGISTERED_FUNCTION_PREFIXES = (
    "agent.",
    "aisystems.",
    "data.",
    "envs.",
    "http.",
    "ranking.",
    "simulators.",
    "string.",
    "zip.",
)

_RESOURCE_INPUT_TYPES = {"AISYSTEM", "DATASET", "SECRET"}
_RUNTIME_CONTROL_INPUT_TYPES = {"RATELIMIT", "POOL"}
_KNOWN_AISYSTEM_INTERFACES = {"llm.chat", "llm.responses", "llm.messages", "agent_a2a", "openai.codex", "http"}
_LLM_AISYSTEM_INTERFACES = {"llm.chat", "llm.responses", "llm.messages"}
_AISYSTEM_FUNCTION_INTERFACES = {
    "aisystems.llm.chat": "llm.chat",
    "aisystems.llm.responses": "llm.responses",
    "aisystems.llm.messages": "llm.messages",
    "aisystems.a2a.send_message": "agent_a2a",
}
_SIMULATOR_OBSERVER_SCHEDULES = {"after_turn", "final"}
_SCALAR_INPUT_TYPES = {
    "BOOLEAN",
    "BYTE",
    "SHORT",
    "INT",
    "INTEGER",
    "LONG",
    "BIGINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "STRING",
    "DATE",
    "TIMESTAMP",
}
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_script(
    statements: Iterable[StatementIR],
    *,
    registry: FunctionRegistry | None = None,
) -> None:
    statement_list = list(statements)
    table_producers = {
        statement.name.lower(): statement
        for statement in statement_list
        if isinstance(statement, (BatchTableStmt, StreamTableStmt)) and statement.name
    }
    _validate_declared_inputs(statement_list)
    _validate_declared_input_function_usage(statement_list)
    validate_table_executor_pools(statement_list)

    for statement in statement_list:
        _validate_source_text(statement)
        if isinstance(statement, PublishReportsStmt) and not statement.table:
            raise ValueError("PUBLISH TO REPORTS requires a source table")
        if isinstance(statement, PublishReportsStmt) and statement.component == "metric":
            _validate_publish_metrics_shape(statement, table_producers)
        if registry is not None:
            declared_input_types = {
                item.name.lower(): item.input_type.upper()
                for item in statement_list
                if isinstance(item, DeclareInputStmt)
            }
            declared_aisystem_interfaces = {
                item.name.lower(): str(item.options.get("interface") or "").strip()
                for item in statement_list
                if isinstance(item, DeclareInputStmt) and item.input_type.upper() == "AISYSTEM"
            }
            declared_pool_kinds = {
                item.name.lower(): pool_kind_from_statement(item)
                for item in statement_list
                if isinstance(item, DeclareInputStmt) and item.input_type.upper() == "POOL"
            }
            _validate_registered_calls(statement, registry, declared_input_types, declared_pool_kinds, declared_aisystem_interfaces)


def _validate_declared_inputs(statements: list[StatementIR]) -> None:
    seen: set[str] = set()
    for statement in statements:
        if not isinstance(statement, DeclareInputStmt):
            continue
        normalized_name = statement.name.strip().lower()
        if not normalized_name:
            raise ValueError("DECLARE INPUT requires an input name")
        if normalized_name in seen:
            raise ValueError(f"Duplicate DECLARE INPUT name '{statement.name}'")
        seen.add(normalized_name)
        if not _is_valid_identifier(statement.name):
            raise ValueError(f"DECLARE INPUT name '{statement.name}' is not a valid identifier")
        input_type = statement.input_type.strip().upper()
        if (
            input_type not in _RESOURCE_INPUT_TYPES
            and input_type not in _SCALAR_INPUT_TYPES
            and input_type not in _RUNTIME_CONTROL_INPUT_TYPES
        ):
            raise ValueError(f"Unsupported DECLARE INPUT type '{statement.input_type}'")
        options = {str(key).strip().lower(): value for key, value in statement.options.items()}
        if input_type == "AISYSTEM":
            unsupported = sorted(key for key in options if key not in {"interface", "system_under_test"})
            if unsupported:
                raise ValueError(f"Unsupported AISYSTEM input option(s): {', '.join(unsupported)}")
            interface = options.get("interface")
            if interface is not None and str(interface).strip() not in _KNOWN_AISYSTEM_INTERFACES:
                raise ValueError(f"Unsupported AISYSTEM interface '{interface}'")
            system_under_test = options.get("system_under_test")
            if system_under_test is not None and not isinstance(system_under_test, bool):
                raise ValueError("DECLARE INPUT AISYSTEM option system_under_test must be boolean")
        elif input_type == "POOL":
            unsupported = sorted(key for key in options if key != "kind")
            if unsupported:
                raise ValueError(f"Unsupported POOL input option(s): {', '.join(unsupported)}")
            pool_kind_from_statement(statement)
        elif options:
            raise ValueError(f"DECLARE INPUT type '{input_type}' does not support WITH options")
        if statement.default_sql is not None and input_type == "POOL":
            canonical_pool_default_json(statement)
        elif statement.default_sql is not None:
            _validate_input_default(input_type, statement.default_sql)


def _is_valid_identifier(value: str) -> bool:
    return bool(_IDENTIFIER_PATTERN.match(value))


def _validate_input_default(input_type: str, default_sql: str) -> None:
    if input_type in _RESOURCE_INPUT_TYPES:
        expression = sqlglot.parse_one(default_sql, read="spark")
        if not isinstance(expression, exp.Literal) or not expression.args.get("is_string"):
            raise ValueError(f"DECLARE INPUT {input_type} DEFAULT must be a string literal")
        default_value = str(expression.this or "").strip()
        if input_type == "AISYSTEM" and not default_value.startswith("aisystem."):
            raise ValueError(
                "DECLARE INPUT AISYSTEM DEFAULT must be an AI system id like 'aisystem.<id>', "
                f"got '{default_value}'"
            )
        if input_type == "SECRET" and not default_value.startswith("secret."):
            raise ValueError(
                "DECLARE INPUT SECRET DEFAULT must be a secret id like 'secret.<id>', "
                f"got '{default_value}'"
            )
        return
    if input_type == "RATELIMIT":
        expression = sqlglot.parse_one(default_sql, read="spark")
        if not isinstance(expression, exp.Literal) or expression.args.get("is_string"):
            raise ValueError("DECLARE INPUT RATELIMIT DEFAULT must be a positive integer")
        try:
            value = int(str(expression.this))
        except (TypeError, ValueError) as exc:
            raise ValueError("DECLARE INPUT RATELIMIT DEFAULT must be a positive integer") from exc
        if value < 1:
            raise ValueError("DECLARE INPUT RATELIMIT DEFAULT must be a positive integer")
        return
    try:
        sqlglot.parse_one(f"SELECT {default_sql}", read="spark")
    except Exception as exc:
        raise ValueError(f"DECLARE INPUT DEFAULT is not valid SQL: {default_sql}") from exc


def _validate_declared_input_function_usage(statements: list[StatementIR]) -> None:
    declared_aisystem_interfaces = {
        statement.name.lower(): str(statement.options.get("interface")).strip()
        for statement in statements
        if isinstance(statement, DeclareInputStmt)
        and statement.input_type.upper() == "AISYSTEM"
        and statement.options.get("interface") is not None
    }
    if not declared_aisystem_interfaces:
        return

    def validate_call(call: CallExpr) -> None:
        expected_interface = _AISYSTEM_FUNCTION_INTERFACES.get(call.function_name.lower())
        if expected_interface is None:
            return
        for argument in call.args:
            if not isinstance(argument, KeywordArgExpr):
                continue
            if argument.name.lower() != "aisystem_id" or not isinstance(argument.value, ColumnRefExpr):
                continue
            declared_interface = declared_aisystem_interfaces.get(argument.value.name.lower())
            if declared_interface is None or declared_interface == expected_interface:
                continue
            raise ValueError(
                f"AISYSTEM input '{argument.value.name}' declares interface '{declared_interface}' "
                f"but function '{call.function_name}' requires '{expected_interface}'"
            )

    for statement in statements:
        def visit(node: object) -> None:
            if isinstance(node, CallExpr):
                validate_call(node)
                return
            if isinstance(node, SqlAstExpr):
                for sql_node in node.expression.walk():
                    ir = expression_to_ir(sql_node)
                    if isinstance(ir, CallExpr):
                        validate_call(ir)

        walk_ir(statement, visit)


def _validate_source_text(statement: StatementIR) -> None:
    tokens = _tokenize_statement(statement.source_text)
    for left, right in zip(tokens, tokens[1:]):
        if left.kind == "symbol" and right.kind == "symbol" and left.text == "=" and right.text == ">":
            raise ValueError("Use 'name = value' for keyword arguments; '=>' is not supported")


def _validate_publish_metrics_shape(
    statement: PublishReportsStmt,
    table_producers: dict[str, BatchTableStmt | StreamTableStmt],
) -> None:
    producer = table_producers.get(statement.table.lower())
    if producer is None or not isinstance(producer.query, SqlAstExpr):
        return
    output_names = [name.lower() for name in _named_selects(producer.query.expression)]
    if not output_names:
        return
    if "metric" not in output_names or "value" not in output_names:
        raise ValueError(
            f"PUBLISH TO REPORTS WITH COMPONENT = METRIC requires source table '{statement.table}' to expose 'metric' and 'value' columns"
        )


def _named_selects(expression: exp.Expression) -> list[str]:
    names = getattr(expression, "named_selects", None)
    if isinstance(names, list):
        return [str(name) for name in names]
    return []


def _validate_registered_calls(
    statement: StatementIR,
    registry: FunctionRegistry,
    declared_input_types: dict[str, str],
    declared_pool_kinds: dict[str, str],
    declared_aisystem_interfaces: dict[str, str],
) -> None:
    def visit(node: object) -> None:
        if isinstance(node, CallExpr):
            _validate_call(node, registry, declared_input_types, declared_pool_kinds, declared_aisystem_interfaces)
            return
        if isinstance(node, SqlAstExpr):
            for sql_node in node.expression.walk():
                ir = expression_to_ir(sql_node)
                if isinstance(ir, CallExpr):
                    _validate_call(ir, registry, declared_input_types, declared_pool_kinds, declared_aisystem_interfaces)

    walk_ir(statement, visit)


def _validate_call(
    call: CallExpr,
    registry: FunctionRegistry,
    declared_input_types: dict[str, str],
    declared_pool_kinds: dict[str, str],
    declared_aisystem_interfaces: dict[str, str],
) -> None:
    resolved = registry.resolve(call.function_name)
    if resolved is None:
        if _looks_like_registered_function(call.function_name):
            raise ValueError(f"Unknown registered function '{call.function_name}'")
        return
    bound = bind_function_arguments(call, resolved.parameters)
    for argument in bound:
        _validate_simulator_observer_schedule(call, argument.parameter.name, argument.value)
        parameter_type = argument.parameter.type_sql.strip().upper()
        if parameter_type == "AISYSTEM":
            if not argument.supplied or _is_null_expr(argument.value):
                continue
            if not isinstance(argument.value, ColumnRefExpr):
                raise ValueError(
                    f"Argument '{argument.parameter.name}' for function '{call.function_name}' "
                    "must reference a declared AISYSTEM input"
                )
            if declared_input_types.get(argument.value.name.lower()) != "AISYSTEM":
                raise ValueError(
                    f"Argument '{argument.parameter.name}' for function '{call.function_name}' "
                    f"must reference a declared AISYSTEM input, got '{argument.value.name}'"
                )
            expected_interface = _aisystem_parameter_expected_interface(call.function_name, argument.parameter.name)
            declared_interface = declared_aisystem_interfaces.get(argument.value.name.lower())
            if expected_interface and declared_interface not in expected_interface:
                raise ValueError(
                    f"AISYSTEM input '{argument.value.name}' declares interface '{declared_interface or 'unspecified'}' "
                    f"but function '{call.function_name}' requires one of {sorted(expected_interface)}"
                )
            continue
        if parameter_type in {"RATELIMIT", "POOL"}:
            if not argument.supplied or _is_null_expr(argument.value):
                continue
            if not isinstance(argument.value, ColumnRefExpr):
                raise ValueError(
                    f"Argument '{argument.parameter.name}' for function '{call.function_name}' "
                    f"must reference a declared {parameter_type} input"
                )
            if declared_input_types.get(argument.value.name.lower()) != parameter_type:
                raise ValueError(
                    f"Argument '{argument.parameter.name}' for function '{call.function_name}' "
                    f"must reference a declared {parameter_type} input, got '{argument.value.name}'"
                )
            if parameter_type == "POOL":
                _validate_pool_kind_compatibility(call, resolved, argument.parameter.name, argument.value.name, declared_pool_kinds)
            continue
        if not is_function_type(argument.parameter.type_sql):
            continue
        if not isinstance(argument.value, ColumnRefExpr):
            raise ValueError(
                f"Argument '{argument.parameter.name}' for function '{call.function_name}' "
                "must be a function reference"
            )
        referenced = registry.resolve(argument.value.name)
        if referenced is None:
            raise ValueError(
                f"Unknown function reference '{argument.value.name}' for argument "
                f"'{argument.parameter.name}' in function '{call.function_name}'"
            )
        _validate_simulator_callback_reference(call, argument.parameter.name, argument.value.name, referenced)
        if not _function_definition_matches(referenced, parse_type_spec(argument.parameter.type_sql)):
            expected = parse_type_spec(argument.parameter.type_sql).normalized()
            actual = _function_definition_type_text(referenced)
            raise ValueError(
                f"Function reference '{argument.value.name}' does not match argument "
                f"'{argument.parameter.name}' for function '{call.function_name}': "
                f"expected {expected}, got {actual}"
            )


def _validate_simulator_observer_schedule(call: CallExpr, argument_name: str, value: object) -> None:
    if call.function_name.lower() != "simulators.observer" or argument_name.lower() != "schedule":
        return
    literal_values = _literal_schedule_values(value)
    if literal_values is None:
        return
    for raw in literal_values:
        normalized = str(raw).strip().lower()
        if normalized not in _SIMULATOR_OBSERVER_SCHEDULES:
            raise ValueError(
                f"simulators.observer schedule contains unsupported value '{raw}'; "
                f"expected one of {sorted(_SIMULATOR_OBSERVER_SCHEDULES)}"
            )


def _literal_schedule_values(value: object) -> list[object] | None:
    if isinstance(value, LiteralExpr):
        if value.value is None:
            return []
        if isinstance(value.value, (list, tuple)):
            return list(value.value)
        return [value.value]
    if not isinstance(value, SqlAstExpr):
        return None
    expression = value.expression
    if isinstance(expression, exp.Array):
        values: list[object] = []
        for item in expression.expressions:
            if not isinstance(item, exp.Literal):
                return None
            values.append(item.this)
        return values
    if isinstance(expression, exp.Literal):
        return [expression.this]
    return None


def _aisystem_parameter_expected_interface(function_name: str, argument_name: str) -> set[str] | None:
    if function_name.lower() == "envs.agent_harness.spec" and argument_name.lower() == "aisystem":
        return _LLM_AISYSTEM_INTERFACES
    return None


def _validate_simulator_callback_reference(
    call: CallExpr,
    argument_name: str,
    referenced_name: str,
    referenced,
) -> None:
    expected_role = _simulator_callback_role(call.function_name, argument_name)
    if expected_role is None:
        return
    normalized_argument = argument_name.lower()
    if referenced.kind == "sql":
        raise ValueError(
            f"Function reference '{referenced_name}' is a local SQL function. "
            f"{call.function_name} requires a registered row-callable runtime function "
            f"for callback argument '{normalized_argument}'."
        )

    metadata = getattr(referenced, "metadata", {}) or {}
    capabilities = _metadata_values(metadata.get("capabilities"))
    simulator_role = str(metadata.get("simulator_role") or "").strip().lower()
    if "row_callable" not in capabilities:
        raise ValueError(
            f"Function reference '{referenced_name}' for argument '{normalized_argument}' "
            "must advertise row_callable capability metadata"
        )
    if expected_role not in capabilities and simulator_role != expected_role:
        raise ValueError(
            f"Function reference '{referenced_name}' for argument '{normalized_argument}' "
            f"must advertise simulator role '{expected_role}'"
        )


def _simulator_callback_role(function_name: str, argument_name: str) -> str | None:
    normalized_function = function_name.lower()
    normalized_argument = argument_name.lower()
    if normalized_function == "simulators.run":
        if normalized_argument in {"user", "agent"}:
            return f"simulator_{normalized_argument}"
        return None
    if normalized_function == "simulators.observer" and normalized_argument == "callback":
        return "simulator_observer"
    return None


def _metadata_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.strip().lower()} if value.strip() else set()
    if isinstance(value, Iterable):
        return {
            str(item).strip().lower()
            for item in value
            if str(item).strip()
        }
    return set()


def _validate_pool_kind_compatibility(
    call: CallExpr,
    resolved,
    argument_name: str,
    pool_input_name: str,
    declared_pool_kinds: dict[str, str],
) -> None:
    metadata = getattr(resolved, "metadata", {}) or {}
    raw_pool = metadata.get("pool")
    pool_metadata = dict(raw_pool) if isinstance(raw_pool, dict) else {}
    expected = str(metadata.get("pool_kind") or pool_metadata.get("kind") or "").strip().lower()
    if not expected:
        return
    actual = declared_pool_kinds.get(pool_input_name.lower())
    if actual and actual != expected:
        raise ValueError(
            f"Argument '{argument_name}' for function '{call.function_name}' requires a {expected} POOL "
            f"but input '{pool_input_name}' declares kind '{actual}'"
        )


def _looks_like_registered_function(function_name: str) -> bool:
    lowered = function_name.strip().lower()
    if lowered in _DIRECTORY_SURFACE_FUNCTIONS:
        return False
    return "." in lowered or any(lowered.startswith(prefix) for prefix in _REGISTERED_FUNCTION_PREFIXES)


_DIRECTORY_SURFACE_FUNCTIONS = {
    "objectstore.exists",
    "objectstore.find",
    "objectstore.glob",
    "objectstore.read_text",
    "objectstore.read_json",
    "objectstore.write_text",
    "objectstore.write_json",
    "objectstore.entry",
}


def _is_null_expr(value: object) -> bool:
    if isinstance(value, LiteralExpr):
        return value.value is None
    if isinstance(value, SqlAstExpr):
        return isinstance(value.expression, exp.Null)
    return False


def _function_definition_matches(definition, expected: TypeSpec) -> bool:
    if not expected.is_function:
        return True
    expected_params = expected.function_parameters
    if len(definition.parameters) != len(expected_params):
        return False
    for actual, (_, expected_type) in zip(definition.parameters, expected_params):
        try:
            actual_type = parse_type_spec(actual.type_sql)
        except ValueError:
            return False
        if actual_type.normalized() != expected_type.normalized():
            return False
    if expected.function_return is None:
        return True
    if definition.return_type_sql is None:
        return False
    try:
        actual_return = parse_type_spec(definition.return_type_sql)
    except ValueError:
        return False
    return actual_return.normalized() == expected.function_return.normalized()


def _function_definition_type_text(definition) -> str:
    parameters = ", ".join(
        f"{parameter.name.lower()} {parse_type_spec(parameter.type_sql).normalized()}"
        for parameter in definition.parameters
    )
    return_type = definition.return_type_sql or "UNKNOWN"
    try:
        return_type = parse_type_spec(return_type).normalized()
    except ValueError:
        pass
    return f"FUNCTION<({parameters}) RETURNS {return_type}>"
