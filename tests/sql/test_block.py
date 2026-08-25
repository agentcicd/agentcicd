import sqlglot

from agentcicd.sql.parsing.sql_segments import SqlSegment, SqlSegmentType


def test_block_properties_empty():
    block = SqlSegment(block_type=SqlSegmentType.CREATE_TABLE, table="demo")
    assert block.statements_sql == []
    assert block.result_expression is None
    assert block.source_tables == []


def test_block_source_tables_filters_target_cte_and_duplicates():
    expr_one = sqlglot.parse_one(
        "CREATE TABLE derived AS SELECT * FROM raw_table",
        read="spark",
    )
    expr_two = sqlglot.parse_one(
        "WITH cte AS (SELECT * FROM cte_source) "
        "SELECT * FROM cte JOIN other_table ON cte.id = other_table.id",
        read="spark",
    )
    block = SqlSegment(
        block_type=SqlSegmentType.CREATE_TABLE,
        table="final_table",
        statement_exprs=[expr_one, expr_two],
    )

    assert set(block.source_tables) == {"raw_table", "cte_source", "other_table"}


def test_publish_reports_block():
    """Test PUBLISH_REPORTS block type."""
    block = SqlSegment(
        block_type=SqlSegmentType.PUBLISH_REPORTS,
        table="reports_table",
        report_component="metric",
    )
    assert block.block_type == SqlSegmentType.PUBLISH_REPORTS
    assert block.table == "reports_table"
    assert block.report_component == "metric"
    assert block.statements_sql == []
    assert block.source_tables == []
