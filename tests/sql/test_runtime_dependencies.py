from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agentcicd" / "src"))

from agentcicd.sql.analysis import extract_runtime_dependencies_from_sql  # noqa: E402


def test_extract_runtime_dependencies_from_sql_resolves_fixture_and_secret_ids() -> None:
    sql = """
    CREATE BATCH TABLE out
    SELECT agent.tool.schema_match(
      image_id = 'image.abc123',
      fixture_ids = ARRAY('fixture.dep001', '$FIXTURE_ID'),
      secret_id = 'secret.secret001'
    ) AS result
    FROM prepared;
    """

    dependencies = extract_runtime_dependencies_from_sql(
        sql,
        macros={"FIXTURE_ID": "fixture.dep002"},
    )

    assert dependencies.image_ids == {"image.abc123"}
    assert dependencies.fixture_ids == {"fixture.dep001", "fixture.dep002"}
    assert dependencies.secret_ids == {"secret.secret001"}


def test_extract_runtime_dependencies_from_sql_resolves_declared_secret_default() -> None:
    sql = """
    DECLARE INPUT browser_key SECRET DEFAULT 'secret.browser';

    CREATE BATCH TABLE out
    SELECT http.request(secret_id = browser_key) AS result
    FROM prepared;
    """

    dependencies = extract_runtime_dependencies_from_sql(sql)

    assert dependencies.secret_ids == {"secret.browser"}


def test_extract_runtime_dependencies_from_sql_handles_fixture_and_secret_maps() -> None:
    sql = """
    SELECT agent.tool.schema_match(
      MAP(
        'fixture_id', 'fixture.runtime123',
        'secret_ids', ARRAY('secret.alpha', 'secret.beta')
      )
    ) AS tool_result
    """

    dependencies = extract_runtime_dependencies_from_sql(sql)

    assert dependencies.fixture_ids == {"fixture.runtime123"}
    assert dependencies.secret_ids == {"secret.alpha", "secret.beta"}


def test_extract_runtime_dependencies_from_sql_resolves_aisystem_ids_from_named_args_and_maps() -> None:
    sql = """
    CREATE BATCH TABLE evaluated AS
    SELECT
      customer_support.helpfulness_judge(
        question = question,
        candidate_answer = candidate_answer,
        aisystem_id = 'aisystem.alpha123'
      ) AS helpfulness,
      agent.tool.schema_match(
        image_id = 'image.dep123',
        aisystem_ids = ARRAY('aisystem.beta456', '$AISYSTEM_ID')
      ) AS fixture_result
    FROM prepared;
    """

    dependencies = extract_runtime_dependencies_from_sql(
        sql,
        macros={"AISYSTEM_ID": "aisystem.gamma789"},
    )

    assert dependencies.aisystem_ids == {
        "aisystem.alpha123",
        "aisystem.beta456",
        "aisystem.gamma789",
    }


def test_extract_runtime_dependencies_from_full_recipe_sql_keeps_aisystem_ids() -> None:
    sql = """
    LOAD input_data FROM 'dataset.4b82f3253683c8e8';

    CREATE BATCH TABLE prepared
    SELECT ticket_id, question, ground_truth_answer AS candidate_answer
    FROM input_data;

    CREATE BATCH TABLE evaluated
    SELECT
      customer_support.helpfulness_judge(
        question = question,
        candidate_answer = candidate_answer,
        aisystem_id = 'aisystem.6f887bf07768eb5a'
      ) AS helpfulness
    FROM prepared;

    CREATE BATCH TABLE summary
    SELECT avg(CAST(helpfulness:score AS DOUBLE)) AS value
    FROM evaluated;

    PUBLISH summary TO REPORTS WITH (COMPONENT = METRIC);
    """

    dependencies = extract_runtime_dependencies_from_sql(sql)

    assert dependencies.aisystem_ids == {"aisystem.6f887bf07768eb5a"}
