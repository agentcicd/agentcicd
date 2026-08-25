from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.integration import validate_script_text
from agentcicd.sql.ir.functions import RegisteredFunctionSpec
from agentcicd.sql.ir.options import StatementOptions
from agentcicd.sql.ir.statements import BatchTableStmt, LoadStmt, RetrieveAnnotationStmt, StreamTableStmt


def _registered_functions() -> list[RegisteredFunctionSpec]:
    return [
        RegisteredFunctionSpec.from_mapping(
            {
                "name": "customer_support.helpfulness_judge",
                "type": "sql",
                "call_name": "customer_support.helpfulness_judge",
                "runtime_alias": "customer_support_helpfulness_judge",
                "signature": {
                    "parameters": [
                        {"name": "question", "has_default": False},
                        {"name": "candidate_answer", "has_default": False},
                        {"name": "aisystem_id", "has_default": False},
                    ]
                },
                "source_text": """
                CREATE FUNCTION customer_support.helpfulness_judge(
                  question STRING,
                  candidate_answer STRING,
                  aisystem_id STRING
                )
                RETURNS STRING
                RETURN '{"score": 1.0}';
                """,
            }
        ),
        RegisteredFunctionSpec.from_mapping(
            {
                "name": "text.normalize",
                "type": "py",
                "call_name": "text.normalize",
                "runtime_alias": "PY_TEXT_NORMALIZE",
                "signature": {
                    "parameters": [
                        {"name": "text", "has_default": False},
                        {"name": "strip_html", "has_default": True, "default_value": False},
                    ]
                },
            }
        ),
    ]


@dataclass(frozen=True)
class SyntaxAcceptanceCase:
    name: str
    script: str


@dataclass(frozen=True)
class SyntaxRejectionCase:
    name: str
    script: str
    expected_message: str


ACCEPTANCE_CASES = [
    SyntaxAcceptanceCase(
        name="load_batch_publish_scores_canonical",
        script="""
        LOAD input_data FROM '$INPUT_PATH' WITH FORMAT=parquet;

        CREATE BATCH TABLE evaluated
        SELECT *
        FROM input_data;

        CREATE BATCH TABLE score_rows
        SELECT
          'helpfulness' AS metric,
          avg(CAST(helpfulness:score AS DOUBLE)) AS value,
          {'bundle': 'customer_support_language'} AS tags
        FROM (
          SELECT customer_support.helpfulness_judge(
            question = question,
            candidate_answer = candidate_answer,
            aisystem_id = '$MODEL_ID'
          ) AS helpfulness
          FROM evaluated
          $LIMIT_ROWS
        ) scored;

        PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
        """,
    ),
    SyntaxAcceptanceCase(
        name="variant_literals_in_query",
        script="""
        CREATE BATCH TABLE prepared
        SELECT
          ['quality', 'safety', 'tone'] AS labels,
          {'bundle': 'customer_support_language', 'source': 'seed'} AS tags
        FROM input_data;
        """,
    ),
    SyntaxAcceptanceCase(
        name="stream_and_retrieve_annotation_forms",
        script="""
        CREATE STREAM TABLE events OPTIONS (BATCH_SIZE=25)
        SELECT * FROM input_data;

        RETRIEVE ANNOTATION RESULTS reviewed FROM ANNOTATION REQUEST 'annreq.123';
        """,
    ),
    SyntaxAcceptanceCase(
        name="runtime_fixture_call_with_exact_keyword_args",
        script="""
        CREATE BATCH TABLE prepared
        SELECT
          text.normalize(text = question, strip_html = true) AS normalized
        FROM input_data;
        """,
    ),
]


REJECTION_CASES = [
    SyntaxRejectionCase(
        name="rich_body_rejects_equals_assignment",
        script="""
        CREATE FUNCTION normalize_context(text STRING)
        RETURNS STRING
        cleaned = trim(text)
        RETURN cleaned;
        """,
        expected_message="Unsupported function body line",
    ),
    SyntaxRejectionCase(
        name="rejects_arrow_keyword_operator",
        script="""
        CREATE BATCH TABLE evaluated
        SELECT customer_support.helpfulness_judge(
          question => question,
          candidate_answer => candidate_answer,
          aisystem_id => '$MODEL_ID'
        ) AS score
        FROM input_data;
        """,
        expected_message="'=>' is not supported",
    ),
    SyntaxRejectionCase(
        name="rejects_invalid_fixture_keyword_name",
        script="""
        CREATE BATCH TABLE evaluated
        SELECT customer_support.helpfulness_judge(
          prompt = question,
          candidate_answer = candidate_answer,
          aisystem_id = '$MODEL_ID'
        ) AS score
        FROM input_data;
        """,
        expected_message="Invalid keyword argument 'prompt'",
    ),
    SyntaxRejectionCase(
        name="rejects_missing_required_fixture_argument",
        script="""
        CREATE BATCH TABLE evaluated
        SELECT customer_support.helpfulness_judge(
          question = question,
          candidate_answer = candidate_answer
        ) AS score
        FROM input_data;
        """,
        expected_message="Missing required argument 'aisystem_id'",
    ),
    SyntaxRejectionCase(
        name="rejects_unknown_registered_function_namespace",
        script="""
        CREATE BATCH TABLE evaluated
        SELECT customer_support.unknown_judge(
          question = question,
          candidate_answer = candidate_answer,
          aisystem_id = '$MODEL_ID'
        ) AS score
        FROM input_data;
        """,
        expected_message="Unknown registered function 'customer_support.unknown_judge'",
    ),
    SyntaxRejectionCase(
        name="publish_scores_requires_metric_and_value",
        script="""
        CREATE BATCH TABLE summary
        SELECT
          avg(CAST(helpfulness:score AS DOUBLE)) AS avg_helpfulness,
          {'bundle': 'customer_support_language'} AS tags
        FROM evaluated;

        PUBLISH summary TO REPORTS WITH (COMPONENT = METRIC);
        """,
        expected_message="requires source table 'summary' to expose 'metric' and 'value' columns",
    ),
]


@pytest.mark.parametrize("case", ACCEPTANCE_CASES, ids=lambda case: case.name)
def test_syntax_guide_acceptance_matrix(case: SyntaxAcceptanceCase) -> None:
    validate_script_text(case.script, registered_functions=_registered_functions())


@pytest.mark.parametrize("case", REJECTION_CASES, ids=lambda case: case.name)
def test_syntax_guide_rejection_matrix(case: SyntaxRejectionCase) -> None:
    with pytest.raises(ValueError, match=case.expected_message):
        validate_script_text(case.script, registered_functions=_registered_functions())


def test_syntax_guide_load_options_are_parsed_exactly() -> None:
    statements = EngineEntrypoint(
        """
        LOAD input_data
        FROM '$INPUT_PATH'
        WITH FORMAT=parquet, SPLITS=('train', 'test'), WRAP=cell, WRAP_CELLS='false';
        """,
        registered_functions=_registered_functions(),
    ).parse()

    assert len(statements) == 1
    statement = statements[0]
    assert isinstance(statement, LoadStmt)
    assert statement.options == StatementOptions.from_mapping(
        {
            "format": "parquet",
            "splits": ("train", "test"),
            "wrap": "cell",
            "wrap_cells": "false",
        }
    )


def test_syntax_guide_create_table_options_and_retrieve_forms_are_parsed() -> None:
    statements = EngineEntrypoint(
        """
        CREATE BATCH TABLE prepared OPTIONS (BATCH_SIZE=100)
        SELECT * FROM input_data;

        CREATE STREAM TABLE streamed OPTIONS (BATCH_SIZE=25)
        SELECT * FROM input_data;

        RETRIEVE ANNOTATION RESULTS reviewed FROM ANNOTATION REQUEST 'annreq.123';
        """,
        registered_functions=_registered_functions(),
    ).parse()

    assert isinstance(statements[0], BatchTableStmt)
    assert statements[0].batch_size == 100

    assert isinstance(statements[1], StreamTableStmt)
    assert statements[1].batch_size == 25

    assert isinstance(statements[2], RetrieveAnnotationStmt)
    assert statements[2].table == "reviewed"
    assert statements[2].source_ref == "annreq.123"
    assert statements[2].annotation_request_id == "annreq.123"


def test_syntax_guide_positional_argument_cannot_follow_keyword_binding() -> None:
    with pytest.raises(ValueError, match="Positional argument cannot follow keyword binding"):
        validate_script_text(
            """
            CREATE BATCH TABLE evaluated
            SELECT text.normalize(text = question, true) AS normalized
            FROM input_data;
            """,
            registered_functions=_registered_functions(),
        )
