from __future__ import annotations

from typing import Any

from agentcicd.sql.contracts import RegisteredRuntimeFunction, RuntimeFunctionParameter
from agentcicd.sql.injections import apply_recipe_injections, normalize_recipe_source, render_recipe_statements
from agentcicd.sql.integration import validate_script_text
from agentcicd.sql.ir.functions import RegisteredFunctionParameterSpec, RegisteredFunctionSpec
from agentcicd.sql.ir.statements import BatchTableStmt, DeclareInputStmt, StatementIR, StreamTableStmt
from agentcicd.sql.lowering.sql_lowering import lower_expr
from agentcicd.sql.pool_inputs import parse_pool_default
from agentcicd.sql.semantics.registry import build_function_registry
from agentcicd.sql.surface.spark_sql_parser import parse_sql
from agentcicd.sql.surface.top_level_parser import TopLevelParser


def test_recipe_render_preserves_canonical_source_when_no_injection_is_needed() -> None:
    source = """DECLARE INPUT executor_pool POOL
WITH kind = 'executor'
DEFAULT {'kind': 'executor', 'max_workers': 1};

CREATE BATCH TABLE evaluated
OPTIONS (POOL = executor_pool)
SELECT id, answer FROM prepared;"""

    assert render_recipe_statements(TopLevelParser(source).parse()) == source


def test_recipe_injections_round_trip_ir_through_rendered_sql() -> None:
    source = """
    DECLARE INPUT agent_pool POOL
    WITH kind = 'session'
    DEFAULT {'max_instances': 2};

    CREATE BATCH TABLE first
    SELECT * FROM prepared;

    CREATE STREAM TABLE second
    OPTIONS (BATCH_SIZE = 5)
    SELECT * FROM first;
    """

    injected = apply_recipe_injections(TopLevelParser(source).parse())
    rendered = render_recipe_statements(injected)
    reparsed = TopLevelParser(rendered).parse()

    assert [_statement_signature(item) for item in reparsed] == [_statement_signature(item) for item in injected]


def test_normalize_recipe_source_injects_executor_pool_once() -> None:
    source = """
    CREATE BATCH TABLE first SELECT * FROM prepared;

    CREATE BATCH TABLE second
    OPTIONS (POOL = executor_pool)
    SELECT * FROM first;
    """

    normalized = normalize_recipe_source(source)
    renormalized = normalize_recipe_source(normalized)

    assert normalized == renormalized
    assert normalized.count("DECLARE INPUT executor_pool POOL") == 1
    assert normalized.count("POOL = executor_pool") == 2


def test_recipe_injections_add_missing_fixture_call_pool_argument() -> None:
    source = """
    CREATE BATCH TABLE out
    SELECT browser.check(task = task) AS result FROM prepared;
    """
    statements = TopLevelParser(source).parse()
    registry = build_function_registry(statements, [_browser_fixture("session")])

    injected = apply_recipe_injections(statements, registry=registry)
    rendered = render_recipe_statements(injected)
    reparsed = TopLevelParser(rendered).parse()

    assert "DECLARE INPUT session_pool POOL" in rendered
    assert "DECLARE INPUT fixture_ratelimit RATELIMIT" in rendered
    assert "'min_warm': 0" in rendered
    assert "'max_instances': 1" in rendered
    assert "'cpu_per_instance': '1'" in rendered
    assert "'memory_per_instance': '1g'" in rendered
    assert "'timeout_seconds': 1200" in rendered
    assert "'lease_ttl_seconds': 1200" in rendered
    assert "'idle_ttl_seconds': 1200" in rendered
    assert "pool = session_pool" in rendered
    assert "limiter = fixture_ratelimit" in rendered
    assert [_statement_signature(item) for item in reparsed] == [_statement_signature(item) for item in injected]


def test_normalize_recipe_source_injects_fixture_call_pool_from_registry() -> None:
    source = """
    CREATE BATCH TABLE out
    SELECT browser.check(task = task) AS result FROM prepared;
    """

    normalized = normalize_recipe_source(source, registered_functions=[_browser_fixture("session")])
    renormalized = normalize_recipe_source(normalized, registered_functions=[_browser_fixture("session")])

    assert normalized == renormalized
    assert normalized.count("DECLARE INPUT session_pool POOL") == 1
    assert normalized.count("DECLARE INPUT fixture_ratelimit RATELIMIT") == 1
    assert normalized.count("pool = session_pool") == 1
    assert normalized.count("limiter = fixture_ratelimit") == 1


def test_normalize_recipe_source_injects_builtin_llm_chat_service_pool() -> None:
    source = """
    CREATE BATCH TABLE out
    SELECT aisystems.llm.chat(
        aisystem_id = 'aisystem.fake',
        messages = parse_json('[]')
    ) AS response
    FROM prepared;
    """

    normalized = normalize_recipe_source(source, registered_functions=[])

    assert "DECLARE INPUT service_pool POOL" in normalized
    assert "DECLARE INPUT fixture_ratelimit RATELIMIT" in normalized
    assert "pool = service_pool" in normalized
    assert "limiter = fixture_ratelimit" in normalized


def test_normalize_recipe_source_accepts_registered_runtime_function_contract() -> None:
    source = """
    CREATE BATCH TABLE out
    SELECT browser.check(task = task) AS result FROM prepared;
    """
    fixture = RegisteredRuntimeFunction(
        id="fixture.browser",
        name="browser.check",
        kind="py",
        call_name="browser.check",
        runtime_alias="browser_check",
        signature=(
            RuntimeFunctionParameter(name="task", type_sql="STRING"),
            RuntimeFunctionParameter(name="pool", type_sql="POOL"),
            RuntimeFunctionParameter(name="limiter", type_sql="RATELIMIT"),
        ),
        pool_kind="session",
    )

    normalized = normalize_recipe_source(source, registered_functions=[fixture])

    assert "DECLARE INPUT session_pool POOL" in normalized
    assert "pool = session_pool" in normalized
    assert "limiter = fixture_ratelimit" in normalized


def test_recipe_injections_preserve_explicit_fixture_call_pool_argument() -> None:
    source = """
    DECLARE INPUT custom_pool POOL
    WITH kind = 'sandbox'
    DEFAULT {'kind': 'sandbox', 'max_instances': 2};

    CREATE BATCH TABLE out
    SELECT browser.check(task = task, pool = custom_pool) AS result FROM prepared;
    """
    statements = TopLevelParser(source).parse()
    registry = build_function_registry(statements, [_browser_fixture("sandbox")])

    rendered = render_recipe_statements(apply_recipe_injections(statements, registry=registry))

    assert "DECLARE INPUT sandbox_pool POOL" not in rendered
    assert "DECLARE INPUT fixture_ratelimit RATELIMIT" in rendered
    assert "pool = custom_pool" in rendered
    assert "limiter = fixture_ratelimit" in rendered


def test_recipe_injections_preserve_explicit_fixture_call_ratelimit_argument() -> None:
    source = """
    DECLARE INPUT custom_limiter RATELIMIT DEFAULT 3;

    CREATE BATCH TABLE out
    SELECT browser.check(task = task, limiter = custom_limiter) AS result FROM prepared;
    """
    statements = TopLevelParser(source).parse()
    registry = build_function_registry(statements, [_browser_fixture("session")])

    rendered = render_recipe_statements(apply_recipe_injections(statements, registry=registry))

    assert "DECLARE INPUT fixture_ratelimit RATELIMIT" not in rendered
    assert "DECLARE INPUT session_pool POOL" in rendered
    assert "pool = session_pool" in rendered
    assert "limiter = custom_limiter" in rendered


def test_public_validation_applies_fixture_call_pool_injections() -> None:
    source = """
    CREATE BATCH TABLE out
    SELECT browser.check(task = task) AS result FROM prepared;
    """

    validate_script_text(source, registered_functions=[_browser_fixture("session")])


def _statement_signature(statement: StatementIR) -> tuple[Any, ...]:
    if isinstance(statement, DeclareInputStmt):
        return (
            "input",
            statement.name,
            statement.input_type.upper(),
            _options_signature(statement.options.to_dict()),
            _default_signature(statement),
        )
    if isinstance(statement, (BatchTableStmt, StreamTableStmt)):
        return (
            "table",
            "batch" if isinstance(statement, BatchTableStmt) else "stream",
            statement.name,
            _options_signature(statement.options.to_dict()),
            _query_signature(statement),
        )
    return (type(statement).__name__, statement.source_text.strip())


def _options_signature(options: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((str(key).lower(), _normalize_option_value(value)) for key, value in options.items()))


def _normalize_option_value(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_normalize_option_value(item) for item in value)
    if isinstance(value, dict):
        return _options_signature(value)
    return value


def _default_signature(statement: DeclareInputStmt) -> Any:
    if statement.default_sql is None:
        return None
    if statement.input_type.upper() == "POOL":
        return _options_signature(parse_pool_default(statement.default_sql))
    return statement.default_sql


def _query_signature(statement: BatchTableStmt | StreamTableStmt) -> str:
    if statement.query_source_text.strip():
        return _normalize_query_text(statement.query_source_text)
    if statement.query is None:
        return ""
    return _normalize_query_text(lower_expr(statement.query, registry=None).sql(dialect="spark"))


def _normalize_query_text(value: str) -> str:
    raw = value.strip().rstrip(";").strip()
    return parse_sql(raw).sql(dialect="spark")


def _browser_fixture(pool_kind: str) -> RegisteredFunctionSpec:
    return RegisteredFunctionSpec(
        name="browser.check",
        kind="remote",
        runtime_alias="browser_check",
        signature=(
            RegisteredFunctionParameterSpec(name="task", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="pool", type_sql="POOL"),
            RegisteredFunctionParameterSpec(name="limiter", type_sql="RATELIMIT"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/check",
            "pool_kind": pool_kind,
            "return_type_sql": "STRING",
        },
    )
