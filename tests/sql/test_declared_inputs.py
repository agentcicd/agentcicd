from pathlib import Path

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend
from agentcicd.sql.injections import normalize_recipe_source
from agentcicd.sql.ir.functions import RegisteredFunctionParameterSpec, RegisteredFunctionSpec
from agentcicd.sql.ir.statements import DeclareInputStmt, LoadStmt
from agentcicd.sql.segmentation import segment_sql
from agentcicd.sql.surface.top_level_parser import TopLevelParser


def test_declared_inputs_parse_and_segment() -> None:
    script = """
    DECLARE INPUT gpt AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.gpt';

    DECLARE INPUT source DATASET DEFAULT 'gpt';
    DECLARE INPUT start_date DATE DEFAULT DATE '2026-05-01';
    LOAD raw FROM source;
    """

    statements = TopLevelParser(script).parse()
    assert [type(item) for item in statements] == [
        DeclareInputStmt,
        DeclareInputStmt,
        DeclareInputStmt,
        LoadStmt,
    ]

    segmentation = segment_sql(script)
    assert len(segmentation.inputs) == 3
    assert segmentation.inputs[0].name == "gpt"
    assert segmentation.inputs[0].input_type == "AISYSTEM"
    assert segmentation.inputs[0].options == {"interface": "llm.chat"}
    assert segmentation.inputs[0].default == "aisystem.gpt"
    assert segmentation.inputs[0].required is False
    assert segmentation.inputs[2].default == "DATE '2026-05-01'"


def test_declared_aisystem_input_parses_environment_default() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'agent_a2a'
    DEFAULT 'aisystem.support_agent' ON ENVIRONMENT 'staging';
    """

    statements = TopLevelParser(script).parse()
    statement = statements[0]
    assert isinstance(statement, DeclareInputStmt)
    assert statement.default_sql == "'aisystem.support_agent'"
    assert statement.environment == "staging"

    segmentation = segment_sql(script)
    assert segmentation.inputs[0].default == "aisystem.support_agent"
    assert segmentation.inputs[0].environment == "staging"

    normalized = normalize_recipe_source(script)
    assert "DEFAULT 'aisystem.support_agent' ON ENVIRONMENT 'staging';" in normalized


def test_declared_aisystem_input_parses_system_under_test_option() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'agent_a2a', system_under_test = true
    DEFAULT 'aisystem.support_agent_v13';
    """

    statement = TopLevelParser(script).parse()[0]
    assert isinstance(statement, DeclareInputStmt)
    assert statement.options.to_dict() == {"interface": "agent_a2a", "system_under_test": True}

    segmentation = segment_sql(script)
    assert segmentation.inputs[0].options == {"interface": "agent_a2a", "system_under_test": True}

    normalized = normalize_recipe_source(script)
    assert "system_under_test = true" in normalized


def test_declared_aisystem_input_parses_non_target_option() -> None:
    script = """
    DECLARE INPUT judge AISYSTEM
    WITH interface = 'llm.responses', system_under_test = false
    DEFAULT 'aisystem.judge';
    """

    segmentation = segment_sql(script)
    assert segmentation.inputs[0].options == {"interface": "llm.responses", "system_under_test": False}


def test_declared_inputs_lower_to_spark_variables_and_dataset_load_default() -> None:
    script = """
    DECLARE INPUT source DATASET DEFAULT 'dataset.path';
    DECLARE INPUT start_date DATE DEFAULT DATE '2026-05-01';
    LOAD raw FROM source;
    CREATE BATCH TABLE out SELECT start_date AS d FROM raw;
    """

    entrypoint = EngineEntrypoint(script)

    assert entrypoint.lower_script() == [
        "DECLARE OR REPLACE VARIABLE source STRING DEFAULT 'dataset.path'",
        "DECLARE OR REPLACE VARIABLE start_date DATE DEFAULT DATE '2026-05-01'",
        "SELECT start_date AS d FROM raw",
    ]
    plan = entrypoint.compile_plan()
    assert [(step.kind, step.name) for step in plan] == [
        ("declare_variable", "source"),
        ("declare_variable", "start_date"),
        ("load_table", "raw"),
        ("create_batch_table", "out"),
    ]
    assert plan[2].payload.path == "dataset.path"


def test_declared_inputs_lower_to_cells_in_wrapped_mode() -> None:
    script = """
    DECLARE INPUT threshold INT DEFAULT 2;
    CREATE BATCH TABLE out SELECT threshold + 1 AS value;
    """

    lowered = EngineEntrypoint(script).lower_script(include_cells=True)

    assert lowered[0].startswith("DECLARE OR REPLACE VARIABLE threshold STRUCT<")
    assert "'value', CAST(2 AS INT)" in lowered[0]
    assert "__agentcicd_cell" in lowered[0]
    assert "threshold.value + 1" in lowered[1]


def test_declared_secret_input_lowers_to_scalar_string() -> None:
    script = """
    DECLARE INPUT browser_key SECRET DEFAULT 'secret.browser';
    CREATE BATCH TABLE out
    SELECT browser_key AS secret_id
    FROM prepared;
    """

    entrypoint = EngineEntrypoint(script, external_tables=["prepared"])
    segmentation = segment_sql(script)

    assert segmentation.inputs[0].default == "secret.browser"
    assert entrypoint.lower_script() == [
        "DECLARE OR REPLACE VARIABLE browser_key STRING DEFAULT 'secret.browser'",
        "SELECT browser_key AS secret_id FROM prepared",
    ]
    lowered_cells = entrypoint.lower_script(include_cells=True)
    assert lowered_cells[1] == "SELECT browser_key AS secret_id FROM prepared"
    assert "browser_key.value" not in lowered_cells[1]


def test_declared_ratelimit_input_lowers_to_integer_variable() -> None:
    script = """
    DECLARE INPUT openai_ratelimit RATELIMIT DEFAULT 10;
    CREATE BATCH TABLE out SELECT openai_ratelimit AS max_in_flight FROM prepared;
    """

    entrypoint = EngineEntrypoint(script, external_tables=["prepared"])

    assert segment_sql(script).inputs[0].input_type == "RATELIMIT"
    assert entrypoint.lower_script() == [
        "DECLARE OR REPLACE VARIABLE openai_ratelimit INT DEFAULT 10",
        "SELECT openai_ratelimit AS max_in_flight FROM prepared",
    ]


def test_declared_ratelimit_input_lowers_to_runtime_control_argument() -> None:
    script = """
    DECLARE INPUT openai_ratelimit RATELIMIT DEFAULT 10;
    CREATE BATCH TABLE out
    SELECT judge.score(text, openai_ratelimit) AS score FROM prepared;
    """
    judge = RegisteredFunctionSpec(
        name="judge.score",
        kind="remote",
        runtime_alias="judge_score",
        signature=(
            RegisteredFunctionParameterSpec(name="text", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="limiter", type_sql="RATELIMIT"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/score",
            "return_type_sql": "STRING",
        },
    )

    lowered = EngineEntrypoint(
        script,
        external_tables=["prepared"],
        registered_functions=[judge],
    ).lower_script()

    assert lowered[0] == "DECLARE OR REPLACE VARIABLE openai_ratelimit INT DEFAULT 10"
    assert "NAMED_STRUCT('key', 'openai_ratelimit', 'max_in_flight', openai_ratelimit)" in lowered[1]


def test_declared_ratelimit_keyword_argument_compiles_cell_plan() -> None:
    script = """
    DECLARE INPUT openai_ratelimit RATELIMIT DEFAULT 10;
    CREATE BATCH TABLE out
    SELECT judge.score(text = text, limiter = openai_ratelimit) AS score FROM prepared;
    """
    judge = RegisteredFunctionSpec(
        name="judge.score",
        kind="remote",
        runtime_alias="judge_score",
        signature=(
            RegisteredFunctionParameterSpec(name="text", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="limiter", type_sql="RATELIMIT"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/score",
            "return_type_sql": "STRING",
        },
    )

    plan = EngineEntrypoint(
        script,
        external_tables=["prepared"],
        registered_functions=[judge],
    ).compile_plan(include_cells=True)

    create_step = next(step for step in plan if step.kind == "create_batch_table")
    assert create_step.payload is not None
    assert "NAMED_STRUCT('key', 'openai_ratelimit', 'max_in_flight', openai_ratelimit.value)" in create_step.payload.sql
    assert "openai_ratelimit.metadata.errors" in create_step.payload.sql


def test_builtin_llm_chat_ratelimit_keyword_argument_compiles_cell_plan() -> None:
    script = """
    DECLARE INPUT small_judge_ratelimit RATELIMIT DEFAULT 4;
    CREATE BATCH TABLE out
    SELECT aisystems.llm.chat(
        aisystem_id = 'aisystem.fake',
        messages = parse_json('[]'),
        limiter = small_judge_ratelimit
    ) AS response
    FROM prepared;
    """

    plan = EngineEntrypoint(
        script,
        external_tables=["prepared"],
    ).compile_plan(include_cells=True)

    create_step = next(step for step in plan if step.kind == "create_batch_table")
    assert create_step.payload is not None
    lowered_sql = create_step.payload.sql.lower()
    assert "agentcicd_wrapped_aisystems_llm_chat" in lowered_sql
    assert "NAMED_STRUCT('key', 'small_judge_ratelimit', 'max_in_flight', small_judge_ratelimit.value)" in (
        create_step.payload.sql
    )
    assert "small_judge_ratelimit.metadata.errors" in create_step.payload.sql


def test_builtin_llm_chat_pool_keyword_argument_compiles_cell_plan() -> None:
    script = """
    DECLARE INPUT service_pool POOL
    WITH kind = 'service'
    DEFAULT {'kind': 'service', 'max_instances': 1};

    CREATE BATCH TABLE out
    SELECT aisystems.llm.chat(
        aisystem_id = 'aisystem.fake',
        messages = parse_json('[]'),
        pool = service_pool
    ) AS response
    FROM prepared;
    """

    plan = EngineEntrypoint(
        script,
        external_tables=["prepared"],
    ).compile_plan(include_cells=True)

    create_step = next(step for step in plan if step.kind == "create_batch_table")
    assert create_step.payload is not None
    lowered_sql = create_step.payload.sql.lower()
    assert "agentcicd_wrapped_aisystems_llm_chat" in lowered_sql
    assert (
        "NAMED_STRUCT('key', 'service_pool', 'config_json', service_pool.value)"
        in create_step.payload.sql
    )
    assert "service_pool.metadata.errors" in create_step.payload.sql


@pytest.mark.parametrize(
    ("pool_kind", "default_sql", "expected_default"),
    [
        ("service", "{'min_instances': 1, 'max_instances': 3}", '{"kind":"service","max_instances":3,"min_instances":1}'),
        ("session", "{'min_warm': 2, 'max_instances': 6}", '{"kind":"session","max_instances":6,"min_warm":2}'),
        ("sandbox", "{'min_warm': 1, 'max_instances': 4}", '{"kind":"sandbox","max_instances":4,"min_warm":1}'),
    ],
)
def test_declared_fixture_pool_kinds_lower_to_canonical_json_string(
    pool_kind: str,
    default_sql: str,
    expected_default: str,
) -> None:
    script = f"""
    DECLARE INPUT fixture_pool POOL
    WITH kind = '{pool_kind}'
    DEFAULT {default_sql};

    CREATE BATCH TABLE out SELECT fixture_pool AS pool_config FROM prepared;
    """

    lowered = EngineEntrypoint(script, external_tables=["prepared"]).lower_script()

    assert lowered[0] == f"DECLARE OR REPLACE VARIABLE fixture_pool STRING DEFAULT '{expected_default}'"


def test_declared_pool_input_lowers_to_canonical_json_string() -> None:
    script = """
    DECLARE INPUT session_pool POOL
    WITH kind = 'session'
    DEFAULT {'min_warm': 2, 'max_instances': 6, 'memory_per_instance': '2Gi'};

    CREATE BATCH TABLE out SELECT session_pool AS pool_config FROM prepared;
    """

    lowered = EngineEntrypoint(script, external_tables=["prepared"]).lower_script()

    assert lowered[0] == (
        "DECLARE OR REPLACE VARIABLE session_pool STRING "
        "DEFAULT '{\"kind\":\"session\",\"max_instances\":6,\"memory_per_instance\":\"2g\",\"min_warm\":2}'"
    )


def test_declared_fixture_pool_input_accepts_timeout_seconds() -> None:
    script = """
    DECLARE INPUT session_pool POOL
    WITH kind = 'session'
    DEFAULT {'max_instances': 1, 'timeout_seconds': 1800};

    CREATE BATCH TABLE out SELECT session_pool AS pool_config FROM prepared;
    """

    lowered = EngineEntrypoint(script, external_tables=["prepared"]).lower_script()

    assert lowered[0] == (
        "DECLARE OR REPLACE VARIABLE session_pool STRING "
        "DEFAULT '{\"kind\":\"session\",\"max_instances\":1,\"timeout_seconds\":1800}'"
    )


def test_declared_pool_input_override_compiles_to_canonical_json_string() -> None:
    script = """
    DECLARE INPUT session_pool POOL
    WITH kind = 'session'
    DEFAULT {'max_instances': 6};

    CREATE BATCH TABLE out SELECT session_pool AS pool_config FROM prepared;
    """

    plan = EngineEntrypoint(
        script,
        external_tables=["prepared"],
        input_values={"session_pool": '{"kind":"session","max_instances":1}'},
    ).compile_plan()

    declare_step = next(step for step in plan if step.kind == "declare_variable")
    assert declare_step.payload.sql == (
        "DECLARE OR REPLACE VARIABLE session_pool STRING DEFAULT "
        "'{\"kind\":\"session\",\"max_instances\":1}'"
    )


def test_declared_pool_keyword_argument_compiles_cell_plan() -> None:
    script = """
    DECLARE INPUT session_pool POOL
    WITH kind = 'session'
    DEFAULT {'max_instances': 6};

    CREATE BATCH TABLE out
    SELECT browser.check(task = task, pool = session_pool) AS result FROM prepared;
    """
    browser = RegisteredFunctionSpec(
        name="browser.check",
        kind="remote",
        runtime_alias="browser_check",
        signature=(
            RegisteredFunctionParameterSpec(name="task", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="pool", type_sql="POOL"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/check",
            "return_type_sql": "STRING",
        },
    )

    plan = EngineEntrypoint(
        script,
        external_tables=["prepared"],
        registered_functions=[browser],
    ).compile_plan(include_cells=True)

    create_step = next(step for step in plan if step.kind == "create_batch_table")
    assert create_step.payload is not None
    assert "NAMED_STRUCT('key', 'session_pool', 'config_json', session_pool.value)" in create_step.payload.sql
    assert "session_pool.metadata.errors" in create_step.payload.sql


def test_executor_pool_is_injected_for_materialized_tables() -> None:
    script = """
    CREATE BATCH TABLE first SELECT * FROM prepared;

    CREATE STREAM TABLE second
    OPTIONS (BATCH_SIZE = 5)
    SELECT * FROM first;
    """

    normalized = normalize_recipe_source(script)

    assert "DECLARE INPUT executor_pool POOL" in normalized
    assert "CREATE BATCH TABLE first\nOPTIONS (POOL = executor_pool)" in normalized
    assert "OPTIONS (BATCH_SIZE = 5, POOL = executor_pool)" in normalized


def test_table_pool_option_must_reference_executor_pool() -> None:
    script = """
    DECLARE INPUT session_pool POOL
    WITH kind = 'session'
    DEFAULT {'max_instances': 2};

    CREATE BATCH TABLE out
    OPTIONS (POOL = session_pool)
    SELECT * FROM prepared;
    """

    with pytest.raises(ValueError, match="must reference an executor POOL input"):
        EngineEntrypoint(script, external_tables=["prepared"]).resolve()


def test_pool_function_argument_must_reference_declared_pool_input() -> None:
    script = """
    DECLARE INPUT not_pool STRING DEFAULT 'x';
    CREATE BATCH TABLE out
    SELECT browser.check(task = task, pool = not_pool) AS result FROM prepared;
    """
    browser = RegisteredFunctionSpec(
        name="browser.check",
        kind="remote",
        runtime_alias="browser_check",
        signature=(
            RegisteredFunctionParameterSpec(name="task", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="pool", type_sql="POOL"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/check",
            "return_type_sql": "STRING",
        },
    )

    with pytest.raises(ValueError, match="must reference a declared POOL input"):
        EngineEntrypoint(
            script,
            external_tables=["prepared"],
            registered_functions=[browser],
        ).resolve()


def test_pool_function_argument_must_match_fixture_pool_kind() -> None:
    script = """
    DECLARE INPUT service_pool POOL
    WITH kind = 'service'
    DEFAULT {'max_instances': 2};

    CREATE BATCH TABLE out
    SELECT browser.check(task = task, pool = service_pool) AS result FROM prepared;
    """
    browser = RegisteredFunctionSpec(
        name="browser.check",
        kind="remote",
        runtime_alias="browser_check",
        signature=(
            RegisteredFunctionParameterSpec(name="task", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="pool", type_sql="POOL"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/check",
            "pool_kind": "session",
            "return_type_sql": "STRING",
        },
    )

    with pytest.raises(ValueError, match="requires a session POOL"):
        EngineEntrypoint(
            script,
            external_tables=["prepared"],
            registered_functions=[browser],
        ).resolve()


@pytest.mark.parametrize("pool_kind", ["service", "session", "sandbox"])
def test_pool_function_argument_accepts_matching_fixture_pool_kind(pool_kind: str) -> None:
    script = f"""
    DECLARE INPUT fixture_pool POOL
    WITH kind = '{pool_kind}'
    DEFAULT {{'max_instances': 2}};

    CREATE BATCH TABLE out
    SELECT browser.check(task = task, pool = fixture_pool) AS result FROM prepared;
    """
    browser = RegisteredFunctionSpec(
        name="browser.check",
        kind="remote",
        runtime_alias="browser_check",
        signature=(
            RegisteredFunctionParameterSpec(name="task", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="pool", type_sql="POOL"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/check",
            "pool_kind": pool_kind,
            "return_type_sql": "STRING",
        },
    )

    EngineEntrypoint(
        script,
        external_tables=["prepared"],
        registered_functions=[browser],
    ).resolve()


def test_declared_pool_input_rejects_mismatched_default_kind() -> None:
    script = """
    DECLARE INPUT session_pool POOL
    WITH kind = 'session'
    DEFAULT {'kind': 'service', 'max_instances': 6};
    """

    with pytest.raises(ValueError, match="DEFAULT kind must match WITH kind"):
        EngineEntrypoint(script).resolve()


def test_ratelimit_function_argument_must_reference_declared_ratelimit_input() -> None:
    script = """
    DECLARE INPUT not_limiter STRING DEFAULT 'x';
    CREATE BATCH TABLE out
    SELECT judge.score(text, not_limiter) AS score FROM prepared;
    """
    judge = RegisteredFunctionSpec(
        name="judge.score",
        kind="remote",
        runtime_alias="judge_score",
        signature=(
            RegisteredFunctionParameterSpec(name="text", type_sql="STRING"),
            RegisteredFunctionParameterSpec(name="limiter", type_sql="RATELIMIT"),
        ),
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/score",
            "return_type_sql": "STRING",
        },
    )

    with pytest.raises(ValueError, match="must reference a declared RATELIMIT input"):
        EngineEntrypoint(
            script,
            external_tables=["prepared"],
            registered_functions=[judge],
        ).resolve()


def test_declared_ratelimit_input_rejects_non_positive_default() -> None:
    script = "DECLARE INPUT openai_ratelimit RATELIMIT DEFAULT 0;"

    with pytest.raises(ValueError, match="RATELIMIT DEFAULT must be a positive integer"):
        EngineEntrypoint(script).resolve()


def test_declared_input_values_override_defaults_in_execution_plan() -> None:
    script = """
    DECLARE INPUT source DATASET DEFAULT 'default.path';
    DECLARE INPUT start_date DATE DEFAULT DATE '2026-05-01';
    LOAD raw FROM source;
    CREATE BATCH TABLE out SELECT start_date AS d FROM raw;
    """

    plan = EngineEntrypoint(
        script,
        input_values={"source": "runtime.path", "start_date": "2026-05-02"},
    ).compile_plan()

    assert plan[0].payload.sql == "DECLARE OR REPLACE VARIABLE source STRING DEFAULT 'runtime.path'"
    assert plan[1].payload.sql == "DECLARE OR REPLACE VARIABLE start_date DATE DEFAULT DATE '2026-05-02'"
    assert plan[2].payload.path == "runtime.path"


def test_declared_secret_input_value_override_is_quoted_in_execution_plan() -> None:
    script = """
    DECLARE INPUT judge_secret SECRET DEFAULT 'secret.default';
    CREATE BATCH TABLE out SELECT judge_secret AS secret_id FROM prepared;
    """

    plan = EngineEntrypoint(
        script,
        external_tables=["prepared"],
        input_values={"judge_secret": "secret.runtime"},
    ).compile_plan()

    assert plan[0].payload.sql == "DECLARE OR REPLACE VARIABLE judge_secret STRING DEFAULT 'secret.runtime'"


def test_declared_input_validation_rejects_invalid_interface() -> None:
    script = """
    DECLARE INPUT gpt AISYSTEM
    WITH interface = 'llm.unknown'
    DEFAULT 'aisystem.gpt';
    """

    with pytest.raises(ValueError, match="Unsupported AISYSTEM interface"):
        EngineEntrypoint(script).resolve()


def test_declared_input_validation_rejects_direct_model_as_aisystem_default() -> None:
    script = """
    DECLARE INPUT user_llm AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'anthropic/claude-sonnet-4-5-20250929';
    """

    with pytest.raises(ValueError, match="DECLARE INPUT AISYSTEM DEFAULT must be an AI system id"):
        EngineEntrypoint(script).resolve()


def test_declared_input_validation_rejects_non_boolean_system_under_test() -> None:
    script = """
    DECLARE INPUT gpt AISYSTEM
    WITH interface = 'llm.chat', system_under_test = 'true'
    DEFAULT 'aisystem.gpt';
    """

    with pytest.raises(ValueError, match="system_under_test must be boolean"):
        EngineEntrypoint(script).resolve()


def test_declared_input_validation_rejects_non_secret_default() -> None:
    script = """
    DECLARE INPUT browser_key SECRET DEFAULT 'api-key-value';
    """

    with pytest.raises(ValueError, match="DECLARE INPUT SECRET DEFAULT must be a secret id"):
        EngineEntrypoint(script).resolve()


def test_declared_input_validation_rejects_duplicate_names() -> None:
    script = """
    DECLARE INPUT source DATASET DEFAULT 'first';
    DECLARE INPUT source DATASET DEFAULT 'second';
    """

    with pytest.raises(ValueError, match="Duplicate DECLARE INPUT name"):
        EngineEntrypoint(script).resolve()


def test_compile_plan_rejects_dataset_input_as_query_relation() -> None:
    script = """
    DECLARE INPUT source DATASET DEFAULT 'dataset.path';

    CREATE BATCH TABLE out
    SELECT *
    FROM source;
    """

    with pytest.raises(ValueError, match="DATASET input 'source' is not a SQL table"):
        EngineEntrypoint(script).compile_plan(include_cells=True)


def test_compile_plan_rejects_unknown_query_relation() -> None:
    script = """
    CREATE BATCH TABLE out
    SELECT *
    FROM missing_table;
    """

    with pytest.raises(ValueError, match="Table 'missing_table' is referenced but is not produced"):
        EngineEntrypoint(script).compile_plan(include_cells=True)


def test_declared_aisystem_interface_must_match_runtime_function_usage() -> None:
    script = """
    DECLARE INPUT gpt AISYSTEM
    WITH interface = 'llm.responses'
    DEFAULT 'aisystem.gpt';

    CREATE BATCH TABLE out
    SELECT aisystems.llm.chat(
      aisystem_id = gpt,
      messages = ARRAY()
    ) AS response_raw;
    """

    with pytest.raises(ValueError, match="requires 'llm.chat'"):
        EngineEntrypoint(script).resolve()


def test_declared_aisystem_input_lowers_as_cell_in_cell_lowering() -> None:
    script = """
    DECLARE INPUT support_agent AISYSTEM
    WITH interface = 'agent_a2a'
    DEFAULT 'aisystem.support';

    CREATE BATCH TABLE out
    SELECT aisystems.a2a.send_message(
      aisystem_id = support_agent,
      message = customer_message,
      metadata = {'case_id': case_id}
    ) AS response_raw
    FROM prepared;
    """

    lowered = EngineEntrypoint(script).lower_script(include_cells=True)

    assert "AGENTCICD_WRAPPED_AISYSTEMS_A2A_SEND_MESSAGE(support_agent" in lowered[1]
    assert "support_agent.value" not in lowered[1]
    assert "support_agent.metadata" not in lowered[1]
    assert "ELSE TO_VARIANT_OBJECT(NAMED_STRUCT('case_id', case_id.value)) END" in lowered[1]
    assert lowered[1].count("ELSE NULL END") >= 3
    assert "<object object at" not in lowered[1]


def test_declared_inputs_execute_with_spark_variables(tmp_path: Path) -> None:
    pyspark = pytest.importorskip("pyspark.sql")
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("declared-inputs-test")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        source_path = tmp_path / "source"
        spark.createDataFrame([(1,)], ["id"]).write.mode("overwrite").parquet(str(source_path))

        script = f"""
        DECLARE INPUT source DATASET DEFAULT '{source_path.as_posix()}';
        DECLARE INPUT start_date DATE DEFAULT DATE '2026-05-01';
        LOAD raw FROM source;
        CREATE BATCH TABLE out SELECT id, start_date AS d FROM raw;
        """

        backend = SparkExecutionBackend(spark, working_dir=str(tmp_path / "run"))
        EngineEntrypoint(script).execute(backend)

        rows = spark.read.parquet(str(tmp_path / "run" / "tables" / "out")).collect()
        assert len(rows) == 1
        id_value = getattr(rows[0].id, "value", rows[0].id)
        date_value = getattr(rows[0].d, "value", rows[0].d)
        assert id_value == 1
        assert str(date_value) == "2026-05-01"

        wrapped_script = """
        DECLARE INPUT threshold INT DEFAULT 2;
        CREATE BATCH TABLE wrapped_out SELECT threshold + 1 AS value;
        """
        wrapped_backend = SparkExecutionBackend(spark, working_dir=str(tmp_path / "wrapped-run"))
        EngineEntrypoint(wrapped_script).execute(wrapped_backend, include_cells=True)
        wrapped_row = spark.read.parquet(str(tmp_path / "wrapped-run" / "tables" / "wrapped_out")).first()
        assert wrapped_row["value"]["value"] == 3
    finally:
        spark.stop()
