from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import CallExpr, ExprIR, KeywordArgExpr, SqlAstExpr
from agentcicd.sql.ir.functions import FunctionDefinitionIR, RegisteredFunctionSpec
from agentcicd.sql.ir.options import OptionValue, StatementOptions
from agentcicd.sql.ir.statements import BatchTableStmt, DeclareInputStmt, StatementIR, StreamTableStmt
from agentcicd.sql.pool_inputs import pool_kind_from_statement
from agentcicd.sql.surface.sqlglot_bridge import _extract_namespaced_call, expression_to_ir
from agentcicd.sql.surface.top_level_parser import TopLevelParser

if TYPE_CHECKING:
    from agentcicd.sql.semantics.registry import FunctionRegistry

DEFAULT_EXECUTOR_POOL_NAME = "executor_pool"
DEFAULT_EXECUTOR_POOL_CONFIG = {
    "kind": "executor",
    "max_workers": 1,
    "cores_per_worker": 1,
    "memory_per_worker": "1g",
    "task_cpus": 1,
    "max_parallel_stages": 1,
}
DEFAULT_EXECUTOR_POOL_DEFAULT_SQL = (
    "{'kind': 'executor', 'max_workers': 1, 'cores_per_worker': 1, "
    "'memory_per_worker': '1g', 'task_cpus': 1, 'max_parallel_stages': 1}"
)
DEFAULT_FIXTURE_POOL_TIMEOUT_SECONDS = 1200
DEFAULT_FIXTURE_POOL_LEASE_TTL_SECONDS = 1200
DEFAULT_FIXTURE_POOL_IDLE_TTL_SECONDS = 1200
DEFAULT_FIXTURE_POOL_CPU_PER_INSTANCE = "1"
DEFAULT_FIXTURE_POOL_MEMORY_PER_INSTANCE = "1g"
DEFAULT_FIXTURE_POOL_CONFIGS = {
    "service": {
        "kind": "service",
        "min_instances": 0,
        "max_instances": 1,
        "cpu_per_instance": DEFAULT_FIXTURE_POOL_CPU_PER_INSTANCE,
        "memory_per_instance": DEFAULT_FIXTURE_POOL_MEMORY_PER_INSTANCE,
        "timeout_seconds": DEFAULT_FIXTURE_POOL_TIMEOUT_SECONDS,
        "lease_ttl_seconds": DEFAULT_FIXTURE_POOL_LEASE_TTL_SECONDS,
        "idle_ttl_seconds": DEFAULT_FIXTURE_POOL_IDLE_TTL_SECONDS,
    },
    "session": {
        "kind": "session",
        "min_warm": 0,
        "max_instances": 1,
        "cpu_per_instance": DEFAULT_FIXTURE_POOL_CPU_PER_INSTANCE,
        "memory_per_instance": DEFAULT_FIXTURE_POOL_MEMORY_PER_INSTANCE,
        "timeout_seconds": DEFAULT_FIXTURE_POOL_TIMEOUT_SECONDS,
        "lease_ttl_seconds": DEFAULT_FIXTURE_POOL_LEASE_TTL_SECONDS,
        "idle_ttl_seconds": DEFAULT_FIXTURE_POOL_IDLE_TTL_SECONDS,
    },
    "sandbox": {
        "kind": "sandbox",
        "min_warm": 0,
        "max_instances": 1,
        "cpu_per_instance": DEFAULT_FIXTURE_POOL_CPU_PER_INSTANCE,
        "memory_per_instance": DEFAULT_FIXTURE_POOL_MEMORY_PER_INSTANCE,
        "timeout_seconds": DEFAULT_FIXTURE_POOL_TIMEOUT_SECONDS,
        "lease_ttl_seconds": DEFAULT_FIXTURE_POOL_LEASE_TTL_SECONDS,
        "idle_ttl_seconds": DEFAULT_FIXTURE_POOL_IDLE_TTL_SECONDS,
    },
}
DEFAULT_FIXTURE_RATELIMIT_NAME = "fixture_ratelimit"
DEFAULT_FIXTURE_RATELIMIT_DEFAULT_SQL = "1"


def normalize_recipe_source(
    script: str,
    *,
    registered_functions: Iterable[RegisteredFunctionSpec] | None = None,
) -> str:
    statements = TopLevelParser(script).parse()
    registry = None
    if registered_functions is not None:
        from agentcicd.sql.semantics.registry import build_function_registry

        registry = build_function_registry(statements, registered_functions)
    normalized = apply_recipe_injections(statements, registry=registry)
    if normalized == statements:
        return script
    return render_recipe_statements(normalized)


def apply_recipe_injections(
    statements: list[StatementIR],
    *,
    registry: "FunctionRegistry | None" = None,
) -> list[StatementIR]:
    statements = _apply_fixture_call_pool_defaults(statements, registry=registry)
    return _apply_executor_pool_defaults(statements)


def _apply_fixture_call_pool_defaults(
    statements: list[StatementIR],
    *,
    registry: "FunctionRegistry | None",
) -> list[StatementIR]:
    if registry is None:
        return statements
    declared_pools = _declared_pool_kinds(statements)
    injected_pool_kinds: set[str] = set()
    requires_default_ratelimit = False
    normalized: list[StatementIR] = []
    changed = False

    for statement in statements:
        if isinstance(statement, (BatchTableStmt, StreamTableStmt)) and isinstance(statement.query, SqlAstExpr):
            query, required_kinds, requires_ratelimit = _inject_call_control_arguments(statement.query, registry=registry)
            injected_pool_kinds.update(required_kinds)
            if requires_ratelimit:
                requires_default_ratelimit = True
            if query != statement.query:
                changed = True
                normalized.append(replace(statement, query=query, query_source_text="", source_text=""))
                continue
        normalized.append(statement)

    for kind in sorted(injected_pool_kinds):
        if _pool_name_for_kind(kind).lower() not in declared_pools:
            normalized.insert(_first_non_input_index(normalized), _default_fixture_pool_statement(kind))
            declared_pools[_pool_name_for_kind(kind).lower()] = kind
            changed = True
    if requires_default_ratelimit and not _has_declared_input(normalized, DEFAULT_FIXTURE_RATELIMIT_NAME, "RATELIMIT"):
        normalized.insert(_first_non_input_index(normalized), _default_fixture_ratelimit_statement())
        changed = True

    return normalized if changed else statements


def _apply_executor_pool_defaults(statements: list[StatementIR]) -> list[StatementIR]:
    if not any(isinstance(statement, (BatchTableStmt, StreamTableStmt)) for statement in statements):
        return statements

    normalized: list[StatementIR] = []
    for statement in statements:
        if isinstance(statement, (BatchTableStmt, StreamTableStmt)) and statement.options.get("pool") is None:
            normalized.append(_with_executor_pool_option(statement))
            continue
        normalized.append(statement)

    if _has_executor_pool_declaration(normalized):
        return normalized
    insert_at = _first_non_input_index(normalized)
    return [
        *normalized[:insert_at],
        _default_executor_pool_statement(),
        *normalized[insert_at:],
    ]


def render_recipe_statements(statements: Iterable[StatementIR]) -> str:
    return "\n\n".join(_render_statement(statement) for statement in statements).strip()


def validate_table_executor_pools(statements: Iterable[StatementIR]) -> None:
    declared_pool_kinds = _declared_pool_kinds(statements)
    for statement in statements:
        if not isinstance(statement, (BatchTableStmt, StreamTableStmt)):
            continue
        pool_name = statement.options.get("pool")
        if pool_name is None:
            continue
        normalized_name = str(pool_name).strip().lower()
        if not normalized_name:
            raise ValueError(f"CREATE TABLE {statement.name} OPTIONS POOL must reference an executor POOL input")
        kind = declared_pool_kinds.get(normalized_name)
        if kind is None:
            raise ValueError(
                f"CREATE TABLE {statement.name} OPTIONS POOL references undeclared POOL input '{pool_name}'"
            )
        if kind != "executor":
            raise ValueError(
                f"CREATE TABLE {statement.name} OPTIONS POOL must reference an executor POOL input, "
                f"got kind '{kind}'"
            )


def _with_executor_pool_option(statement: BatchTableStmt | StreamTableStmt) -> BatchTableStmt | StreamTableStmt:
    options = statement.options.to_dict()
    options["pool"] = DEFAULT_EXECUTOR_POOL_NAME
    return replace(statement, options=StatementOptions.from_mapping(options), source_text="")


def _inject_call_control_arguments(
    expression: SqlAstExpr,
    *,
    registry: "FunctionRegistry",
) -> tuple[SqlAstExpr, set[str], bool]:
    required_kinds: set[str] = set()
    requires_ratelimit = False
    changed = False

    def transform(node: exp.Expression) -> exp.Expression:
        nonlocal changed, requires_ratelimit
        call = expression_to_ir(node)
        if not isinstance(call, CallExpr):
            return node
        definition = registry.resolve(call.function_name)
        if definition is None:
            return node
        call_expression = _call_expression_node(node)
        if call_expression is None:
            return node
        for parameter_name, parameter_type in _missing_control_parameters(call, definition):
            if parameter_type == "POOL":
                pool_kind = _definition_pool_kind(definition)
                if pool_kind is None:
                    continue
                call_expression.append("expressions", _control_keyword_argument(parameter_name, _pool_name_for_kind(pool_kind)))
                required_kinds.add(pool_kind)
                changed = True
            elif parameter_type == "RATELIMIT":
                call_expression.append("expressions", _control_keyword_argument(parameter_name, DEFAULT_FIXTURE_RATELIMIT_NAME))
                requires_ratelimit = True
                changed = True
        return node

    transformed = expression.expression.copy().transform(transform, copy=False)
    return (SqlAstExpr(transformed), required_kinds, requires_ratelimit) if changed else (expression, required_kinds, requires_ratelimit)


def _missing_control_parameters(call: CallExpr, definition: FunctionDefinitionIR) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for index, parameter in enumerate(definition.parameters):
        parameter_type = parameter.type_sql.strip().upper()
        if parameter_type not in {"POOL", "RATELIMIT"}:
            continue
        if _argument_is_supplied(call, parameter.name, index):
            continue
        missing.append((parameter.name, parameter_type))
    return missing


def _argument_is_supplied(call: CallExpr, parameter_name: str, parameter_index: int) -> bool:
    positional_index = 0
    for argument in call.args:
        if isinstance(argument, KeywordArgExpr):
            if argument.name.lower() == parameter_name.lower():
                return True
            continue
        if positional_index == parameter_index:
            return True
        positional_index += 1
    return False


def _definition_pool_kind(definition: FunctionDefinitionIR) -> str | None:
    metadata = dict(definition.metadata or {})
    raw_kind = str(metadata.get("pool_kind") or "").strip().lower()
    if not raw_kind:
        raw_pool = metadata.get("pool")
        if isinstance(raw_pool, dict):
            raw_kind = str(raw_pool.get("kind") or "").strip().lower()
    return raw_kind if raw_kind in DEFAULT_FIXTURE_POOL_CONFIGS else None


def _call_expression_node(node: exp.Expression) -> exp.Anonymous | None:
    namespaced = _extract_namespaced_call(node)
    if namespaced is not None:
        return namespaced[1]
    return node if isinstance(node, exp.Anonymous) else None


def _control_keyword_argument(parameter_name: str, input_name: str) -> exp.EQ:
    return exp.EQ(
        this=exp.column(parameter_name),
        expression=exp.column(input_name),
    )


def _default_fixture_pool_statement(kind: str) -> DeclareInputStmt:
    return DeclareInputStmt(
        name=_pool_name_for_kind(kind),
        input_type="POOL",
        options=StatementOptions.from_mapping({"kind": kind}),
        default_sql=_pool_default_sql(DEFAULT_FIXTURE_POOL_CONFIGS[kind]),
    )


def _default_fixture_ratelimit_statement() -> DeclareInputStmt:
    return DeclareInputStmt(
        name=DEFAULT_FIXTURE_RATELIMIT_NAME,
        input_type="RATELIMIT",
        default_sql=DEFAULT_FIXTURE_RATELIMIT_DEFAULT_SQL,
    )


def _default_executor_pool_statement() -> DeclareInputStmt:
    return DeclareInputStmt(
        name=DEFAULT_EXECUTOR_POOL_NAME,
        input_type="POOL",
        options=StatementOptions.from_mapping({"kind": "executor"}),
        default_sql=DEFAULT_EXECUTOR_POOL_DEFAULT_SQL,
    )


def _pool_name_for_kind(kind: str) -> str:
    return f"{kind}_pool"


def _pool_default_sql(config: dict[str, object]) -> str:
    return "{" + ", ".join(
        f"{_quote_string(str(key))}: {_render_structured_literal_value(value)}" for key, value in config.items()
    ) + "}"


def _declared_pool_kinds(statements: Iterable[StatementIR]) -> dict[str, str]:
    return {
        statement.name.lower(): pool_kind_from_statement(statement)
        for statement in statements
        if isinstance(statement, DeclareInputStmt) and statement.input_type.upper() == "POOL"
    }


def _has_executor_pool_declaration(statements: Iterable[StatementIR]) -> bool:
    for statement in statements:
        if not isinstance(statement, DeclareInputStmt) or statement.input_type.upper() != "POOL":
            continue
        if statement.name.lower() == DEFAULT_EXECUTOR_POOL_NAME and pool_kind_from_statement(statement) == "executor":
            return True
    return False


def _has_declared_input(statements: Iterable[StatementIR], name: str, input_type: str) -> bool:
    normalized_name = name.strip().lower()
    normalized_type = input_type.strip().upper()
    return any(
        isinstance(statement, DeclareInputStmt)
        and statement.name.lower() == normalized_name
        and statement.input_type.upper() == normalized_type
        for statement in statements
    )


def _first_non_input_index(statements: list[StatementIR]) -> int:
    for index, statement in enumerate(statements):
        if not isinstance(statement, DeclareInputStmt):
            return index
    return len(statements)


def _render_statement(statement: StatementIR) -> str:
    if statement.source_text.strip():
        return _ensure_semicolon(statement.source_text.strip())
    if isinstance(statement, DeclareInputStmt):
        return _render_declare_input(statement)
    if isinstance(statement, BatchTableStmt):
        return _render_table_statement("BATCH", statement)
    if isinstance(statement, StreamTableStmt):
        return _render_table_statement("STREAM", statement)
    raise ValueError(f"Cannot render statement type '{type(statement).__name__}' without source_text")


def _render_declare_input(statement: DeclareInputStmt) -> str:
    sql = f"DECLARE INPUT {statement.name} {statement.input_type.upper()}"
    if statement.options:
        sql += "\nWITH " + _render_options_assignments(statement.options)
    if statement.default_sql is not None:
        sql += f"\nDEFAULT {statement.default_sql}"
    if statement.environment is not None:
        sql += f" ON ENVIRONMENT '{statement.environment}'"
    return _ensure_semicolon(sql)


def _render_table_statement(mode: str, statement: BatchTableStmt | StreamTableStmt) -> str:
    query_sql = statement.query_source_text.strip()
    if not query_sql and statement.query is not None:
        query_sql = _render_expr(statement.query)
    if not query_sql:
        raise ValueError(f"Cannot render {mode} table '{statement.name}' without a query")
    sql = f"CREATE {mode} TABLE {statement.name}"
    if statement.options:
        sql += "\nOPTIONS (" + _render_options_assignments(statement.options) + ")"
    sql += "\n" + query_sql
    return _ensure_semicolon(sql)


def _render_expr(expression: ExprIR) -> str:
    from agentcicd.sql.lowering.sql_lowering import lower_expr

    return lower_expr(expression, registry=None).sql(dialect="spark")


def _render_options_assignments(options: StatementOptions) -> str:
    return ", ".join(f"{key.upper()} = {_render_option_value(value)}" for key, value in options.items())


def _render_option_value(value: OptionValue) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, tuple):
        return "(" + ", ".join(_render_option_value(item) for item in value) + ")"
    if isinstance(value, dict):
        rendered = ", ".join(f"{_quote_string(key)}: {_render_structured_literal_value(item)}" for key, item in value.items())
        return "{" + rendered + "}"
    text = str(value)
    if _is_number_literal(text):
        return text
    return text if _is_identifier(text) else _quote_string(text)


def _is_number_literal(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    try:
        int(text)
        return True
    except ValueError:
        pass
    try:
        float(text)
    except ValueError:
        return False
    return any(char.isdigit() for char in text)


def _render_structured_literal_value(value: OptionValue) -> str:
    if isinstance(value, str):
        return _quote_string(value)
    return _render_option_value(value)


def _is_identifier(value: str) -> bool:
    return value.replace("_", "").isalnum() and value[:1].isalpha()


def _quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ensure_semicolon(sql: str) -> str:
    return sql.rstrip().rstrip(";") + ";"
