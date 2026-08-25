import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agentcicd" / "src"))

from agentcicd.sql.analysis import collect_recipe_artifact_references, validate_recipe_artifact_references


def test_collect_recipe_artifact_references_merges_metadata_and_sql_dependencies() -> None:
    sql = """
    LOAD cases FROM 'agentcicd://dataset.abc123';

    CREATE BATCH TABLE generated
    SELECT
      aisystems.llm.chat(aisystem_id = 'aisystem.target123', messages = messages) AS response,
      agent.tool.schema_match(fixture_id = '$FIXTURE_ID', secret_id = 'secret.runtime123') AS tool_result
    FROM cases;
    """

    refs = collect_recipe_artifact_references(
        sql,
        default_macros={"FIXTURE_ID": "fixture.runtime123"},
        fixture_ids=["fixture.attached123"],
    )

    assert refs.fixture_ids == {"fixture.attached123", "fixture.runtime123"}
    assert refs.aisystem_ids == {"aisystem.target123"}
    assert refs.secret_ids == {"secret.runtime123"}
    assert refs.dataset_ids == {"dataset.abc123"}


def test_validate_recipe_artifact_references_reports_missing_refs() -> None:
    sql = """
    LOAD cases FROM 'dataset.missing';

    CREATE BATCH TABLE generated
    SELECT aisystems.llm.chat(aisystem_id = 'aisystem.missing', messages = messages) AS response
    FROM cases;
    """

    result = validate_recipe_artifact_references(
        sql,
        fixture_ids=["fixture.missing"],
        available_fixture_ids=set(),
        available_aisystem_ids=set(),
        available_secret_ids=set(),
        available_dataset_ids=set(),
    )

    assert not result.valid
    assert {(issue.code, issue.reference_id) for issue in result.errors} == {
        ("missing_fixture", "fixture.missing"),
        ("missing_aisystem", "aisystem.missing"),
        ("missing_dataset", "dataset.missing"),
    }


def test_validate_recipe_artifact_references_accepts_available_refs() -> None:
    sql = """
    LOAD cases FROM 'dataset.ok';

    CREATE BATCH TABLE generated
    SELECT aisystems.llm.chat(aisystem_id = 'aisystem.ok', messages = messages) AS response
    FROM cases;
    """

    result = validate_recipe_artifact_references(
        sql,
        fixture_ids=["fixture.ok"],
        available_fixture_ids={"fixture.ok"},
        available_aisystem_ids={"aisystem.ok"},
        available_secret_ids=set(),
        available_dataset_ids={"dataset.ok"},
    )

    assert result.valid
    assert result.errors == []
