from agentcicd.sql.integration import (
    discover_registered_function_references,
    validate_label_studio_template_xml,
    validate_script_text,
)


def _registered_sql_fixture() -> dict[str, object]:
    return {
        "name": "customer_support.helpfulness_judge",
        "type": "sql",
        "call_name": "customer_support.helpfulness_judge",
        "runtime_alias": "customer_support_helpfulness_judge",
        "signature": {
            "parameters": [
                {"name": "question", "has_default": False},
                {"name": "candidate_answer", "has_default": False},
            ]
        },
        "source_text": """
        CREATE FUNCTION customer_support.helpfulness_judge(question STRING, candidate_answer STRING)
        RETURNS DOUBLE
        RETURN 1.0;
        """,
    }


def _registered_python_fixture() -> dict[str, object]:
    return {
        "id": "fixture.supportsim",
        "name": "support_multi_turn_simulator_run_support_simulation",
        "type": "py",
        "call_name": "support_multi_turn_simulator.run_support_simulation",
        "runtime_alias": "support_multi_turn_simulator_run_support_simulation",
        "signature": {
            "parameters": [
                {"name": "intent", "has_default": False},
                {"name": "target_agent", "has_default": False},
                {"name": "user_model", "has_default": False},
            ]
        },
    }


def test_validate_script_text_accepts_registered_sql_fixture() -> None:
    validate_script_text(
        """
        CREATE BATCH TABLE evaluated
        SELECT customer_support.helpfulness_judge(
          question=question,
          candidate_answer=candidate_answer
        ) AS score
        FROM prepared;
        """,
        registered_functions=[_registered_sql_fixture()],
    )


def test_validate_script_text_rejects_trailing_projection_comma() -> None:
    try:
        validate_script_text(
            """
            CREATE BATCH TABLE generated_outputs
            SELECT
              case_id,
              customer_support.helpfulness_judge(
                question = prompt,
                candidate_answer = generated_text
              ) AS score,
            FROM test_cases;
            """,
            registered_functions=[_registered_sql_fixture()],
        )
    except Exception as exc:
        assert "Unexpected token" in str(exc) or "Invalid expression" in str(exc)
    else:
        raise AssertionError("Expected trailing projection comma to be rejected")


def test_validate_script_text_rejects_invalid_annotation_template_xml() -> None:
    try:
        validate_script_text(
            """
            CREATE BATCH TABLE evaluated SELECT 1 AS id;

            PUBLISH evaluated TO ANNOTATION QUEUE 'queue' AS review
            WITH (
              TEMPLATE =   TEMPLATE = '<View><Text name="body" value="$body"/></View>'
            );
            """
        )
    except ValueError as exc:
        assert "Invalid annotation template XML" in str(exc)
    else:
        raise AssertionError("Expected invalid annotation template XML to be rejected")


def test_validate_script_text_rejects_annotation_template_submit_control() -> None:
    try:
        validate_script_text(
            """
            CREATE BATCH TABLE evaluated SELECT 1 AS id;

            PUBLISH evaluated TO ANNOTATION QUEUE 'queue' AS review
            WITH (
              TEMPLATE = '<View><Text name="body" value="$body"/><SubmitButton /></View>'
            );
            """
        )
    except ValueError as exc:
        assert "submit controls are owned by the annotation UI" in str(exc)
    else:
        raise AssertionError("Expected annotation template submit control to be rejected")


def test_validate_label_studio_template_rejects_button_submit_labels() -> None:
    try:
        validate_label_studio_template_xml("<View><Button value='Submit review' /></View>", context="TEMPLATE")
    except ValueError as exc:
        assert "submit controls are owned by the annotation UI" in str(exc)
    else:
        raise AssertionError("Expected template submit button to be rejected")


def test_discover_registered_function_references_excludes_local_wrappers() -> None:
    references = discover_registered_function_references(
        """
        CREATE FUNCTION local.wrap_score(question STRING, candidate_answer STRING)
        RETURNS DOUBLE
        RETURN customer_support.helpfulness_judge(question=question, candidate_answer=candidate_answer);

        CREATE BATCH TABLE evaluated
        SELECT local.wrap_score(question, candidate_answer) AS score
        FROM prepared;
        """,
        registered_functions=[_registered_sql_fixture()],
    )

    assert references == ["customer_support.helpfulness_judge"]


def test_discover_registered_function_references_accepts_standalone_select() -> None:
    references = discover_registered_function_references(
        """
        SELECT customer_support.helpfulness_judge(
          question => question,
          candidate_answer => candidate_answer
        )
        FROM prepared
        """,
        registered_functions=[_registered_sql_fixture()],
    )

    assert references == ["customer_support.helpfulness_judge"]


def test_discover_registered_function_references_accepts_call_name_distinct_from_name() -> None:
    references = discover_registered_function_references(
        """
        SELECT support_multi_turn_simulator.run_support_simulation(
          intent = user_message,
          target_agent = target_agent,
          user_model = user_model
        ) AS simulation_result
        FROM prepared_cases
        """,
        registered_functions=[_registered_python_fixture()],
    )

    assert references == ["support_multi_turn_simulator.run_support_simulation"]


def test_discover_registered_function_references_accepts_inline_returns_header() -> None:
    references = discover_registered_function_references(
        """
        CREATE FUNCTION local.wrap_score(question STRING, candidate_answer STRING) RETURNS DOUBLE
        RETURN customer_support.helpfulness_judge(question => question, candidate_answer => candidate_answer);

        SELECT local.wrap_score(question, candidate_answer)
        FROM prepared
        """,
        registered_functions=[_registered_sql_fixture()],
    )

    assert references == ["customer_support.helpfulness_judge"]


def test_discover_registered_function_references_accepts_multiline_function_header() -> None:
    references = discover_registered_function_references(
        """
        CREATE FUNCTION local.wrap_score(
          question STRING,
          candidate_answer STRING
        )
        RETURNS DOUBLE
        RETURN customer_support.helpfulness_judge(
          question => question,
          candidate_answer => candidate_answer
        );

        SELECT local.wrap_score(question, candidate_answer)
        FROM prepared
        """,
        registered_functions=[_registered_sql_fixture()],
    )

    assert references == ["customer_support.helpfulness_judge"]


def test_validate_script_text_accepts_python_style_variant_literal_tags() -> None:
    validate_script_text(
        """
        CREATE BATCH TABLE summary
        SELECT
          'helpfulness' AS metric,
          avg(CAST(helpfulness:score AS DOUBLE)) AS value,
          {'bundle': 'customer_support_language', 'judge_model': 'claude-3-5-haiku-latest'} AS tags
        FROM evaluated;

        PUBLISH summary TO REPORTS WITH (COMPONENT = METRIC);
        """,
        registered_functions=[_registered_sql_fixture()],
    )
