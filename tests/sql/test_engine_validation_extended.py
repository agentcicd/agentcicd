import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.validator import validate_lowered_sql


def test_validate_lowered_sql_accepts_join_window_and_subquery_shapes():
    sql = """
    SELECT *
    FROM (
      SELECT
        customer_id,
        score,
        ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY score DESC) AS rn
      FROM scores
    ) ranked
    JOIN segments ON ranked.customer_id = segments.customer_id
    WHERE rn = 1
    """

    result = validate_lowered_sql(sql)

    assert result.ok is True


def test_entrypoint_validate_lowered_script_for_stream_query():
    script = """
    CREATE STREAM TABLE ranked
    SELECT customer_id, AVG(score) AS avg_score
    FROM scores
    GROUP BY customer_id;
    """

    validations = EngineEntrypoint(script).validate_lowered_script(include_cells=True)

    assert len(validations) == 1
    assert validations[0].ok is True


@pytest.mark.parametrize(
    ("script", "message"),
    [
        (
            """
            CREATE BATCH TABLE out
            SELECT *
            FROM left_scores
            JOIN right_scores USING (id);
            """,
            "JOIN ... USING",
        ),
        (
            """
            CREATE BATCH TABLE out
            SELECT *
            FROM scores
            PIVOT (AVG(score) FOR segment IN ('a'));
            """,
            "PIVOT",
        ),
        (
            """
            LOAD raw FROM '/tmp/raw.parquet'
            WITH FORMAT='parquet', WRAP=false;
            """,
            "always wraps",
        ),
    ],
)
def test_wrapped_mode_rejects_unsupported_constructs_before_lowering(script: str, message: str):
    with pytest.raises(ValueError, match=message):
        EngineEntrypoint(script).compile_plan(include_cells=True)


def test_wrapped_mode_allows_regular_struct_field_named_value_before_lowering():
    script = """
    CREATE BATCH TABLE out
    SELECT payload.value AS extracted_value
    FROM prepared;
    """

    plan = EngineEntrypoint(script, external_tables=["prepared"]).compile_plan(include_cells=True)

    create_step = next(step for step in plan if step.kind == "create_batch_table")
    assert create_step.payload is not None


@pytest.mark.parametrize(
    "field_access",
    [
        "payload.metadata.errors",
        "payload.metadata.error",
        "payload.__agentcicd_cell",
    ],
)
def test_wrapped_mode_rejects_unambiguous_physical_cell_fields(field_access: str):
    script = f"""
    CREATE BATCH TABLE out
    SELECT {field_access} AS leaked_metadata
    FROM prepared;
    """

    with pytest.raises(ValueError, match="physical cell field access"):
        EngineEntrypoint(script, external_tables=["prepared"]).compile_plan(include_cells=True)


def test_compile_plan_can_validate_wrapped_lowering_without_rendering_sql():
    script = """
    CREATE BATCH TABLE prepared
    SELECT id, parse_json(raw_json) AS payload
    FROM raw_rows;

    CREATE BATCH TABLE scored
    SELECT
      id,
      CAST(payload['score'] AS DOUBLE) AS score,
      CASE
        WHEN CAST(payload['confidence'] AS DOUBLE) < 0.5 THEN 'review'
        ELSE 'accept'
      END AS route
    FROM prepared;
    """

    rendered_plan = EngineEntrypoint(script, external_tables=["raw_rows"]).compile_plan(
        include_cells=True,
        render_sql=True,
    )
    validation_plan = EngineEntrypoint(script, external_tables=["raw_rows"]).compile_plan(
        include_cells=True,
        render_sql=False,
    )

    rendered_sql = [
        step.payload.sql
        for step in rendered_plan
        if step.kind in {"create_batch_table", "create_stream_table"}
    ]
    validation_sql = [
        step.payload.sql
        for step in validation_plan
        if step.kind in {"create_batch_table", "create_stream_table"}
    ]

    assert all(sql for sql in rendered_sql)
    assert validation_sql == ["", ""]
