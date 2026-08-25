import pytest

from agentcicd.sql.engine.rerun_analysis import RerunFeasibilityError, analyze_rerun_feasibility


def test_user_selected_rerun_stages_clean_upstreams_and_dirty_descendants() -> None:
    analysis = analyze_rerun_feasibility(
        """
        CREATE BATCH TABLE source_data SELECT 1 AS id;

        CREATE BATCH TABLE fixture_results SELECT id FROM source_data;

        CREATE BATCH TABLE successful_rows SELECT id FROM fixture_results;

        CREATE BATCH TABLE error_summary SELECT count(*) AS n FROM successful_rows;
        """,
        completed_tables={"source_data", "fixture_results", "successful_rows", "error_summary"},
        from_node="table:successful_rows",
        from_node_mode="user",
    )

    assert analysis.dirty_tables == {"successful_rows", "error_summary"}
    assert analysis.staged_tables == {"source_data", "fixture_results"}


def test_user_selected_rerun_expands_unfinished_prerequisites_for_dirty_descendants() -> None:
    analysis = analyze_rerun_feasibility(
        """
        CREATE BATCH TABLE judge_label_results SELECT 'support' AS task, 'ok' AS label;

        CREATE BATCH TABLE judge_label_f1_by_label SELECT task, label, 1.0 AS value FROM judge_label_results;

        CREATE BATCH TABLE score_rate_observations SELECT 'accuracy' AS metric, 1.0 AS value;

        CREATE BATCH TABLE score_rate_uncertainty_rows
        SELECT metric || '_ci95' AS metric, value FROM score_rate_observations;

        CREATE BATCH TABLE score_row_structs SELECT task AS metric, value FROM judge_label_f1_by_label;

        CREATE BATCH TABLE score_rows
        SELECT metric, value FROM score_row_structs
        UNION ALL
        SELECT metric, value FROM score_rate_uncertainty_rows;
        """,
        completed_tables={"judge_label_results", "score_rate_observations"},
        from_node="table:judge_label_f1_by_label",
        from_node_mode="user",
    )

    assert analysis.dirty_tables == {
        "judge_label_f1_by_label",
        "score_rate_uncertainty_rows",
        "score_row_structs",
        "score_rows",
    }
    assert analysis.staged_tables == {"judge_label_results", "score_rate_observations"}


def test_auto_rerun_dirties_incomplete_tables_and_stages_clean_upstreams() -> None:
    analysis = analyze_rerun_feasibility(
        """
        CREATE BATCH TABLE source_data SELECT 1 AS id;

        CREATE BATCH TABLE fixture_results SELECT id FROM source_data;

        CREATE BATCH TABLE successful_rows SELECT id FROM fixture_results;

        CREATE BATCH TABLE error_summary SELECT count(*) AS n FROM successful_rows;
        """,
        completed_tables={"source_data"},
    )

    assert analysis.dirty_tables == {"fixture_results", "successful_rows", "error_summary"}
    assert analysis.staged_tables == {"source_data"}


def test_selected_rerun_rejects_missing_clean_upstream() -> None:
    with pytest.raises(RerunFeasibilityError) as exc_info:
        analyze_rerun_feasibility(
            """
            CREATE BATCH TABLE source_data SELECT 1 AS id;

            CREATE BATCH TABLE fixture_results SELECT id FROM source_data;

            CREATE BATCH TABLE successful_rows SELECT id FROM fixture_results;
            """,
            completed_tables={"successful_rows"},
            from_node="table:successful_rows",
            from_node_mode="user",
        )

    assert "successful_rows requires fixture_results" in str(exc_info.value)
