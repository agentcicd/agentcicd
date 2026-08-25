import sqlglot

from agentcicd.sql.parsing.sql_segments import SqlSegment, SqlSegmentType


def test_source_tables_include_qualified_names():
    statement = sqlglot.parse_one(
        "SELECT * FROM catalog.schema.table_a JOIN table_b ON table_a.id = table_b.id",
        read="spark",
    )
    block = SqlSegment(
        block_type=SqlSegmentType.CREATE_TABLE,
        table="final_table",
        statement_exprs=[statement],
    )

    assert "table_a" in block.source_tables
    assert "table_b" in block.source_tables
