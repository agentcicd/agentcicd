import pytest
import sqlglot

from agentcicd.sql.parsing.parser import AgentCICDScriptParser
from agentcicd.sql.parsing.sql_segments import SqlSegmentType


def test_parse_load_and_save_blocks():
    script = """
    LOAD sales FROM 's3://bucket/sales' WITH FORMAT='csv', SPLITS=('train','test');
    SAVE sales TO 's3://bucket/out' WITH FORMAT='delta';
    """
    blocks = AgentCICDScriptParser(script).parse()

    assert [block.block_type for block in blocks] == [SqlSegmentType.LOAD_TABLE, SqlSegmentType.EXPORT_TABLE]
    assert blocks[0].options["FORMAT"] == "csv"
    assert blocks[0].options["SPLITS"] == ["train", "test"]
    assert blocks[1].path == "s3://bucket/out"


def test_parse_create_table_batch_with_options_and_function_inline():
    script = """
    CREATE FUNCTION add_one(x) AS SELECT x + 1;
    CREATE BATCH TABLE output OPTIONS (BATCH_SIZE=10) SELECT add_one(value) AS v FROM source_table;
    """
    blocks = AgentCICDScriptParser(script).parse()

    assert len(blocks) == 2
    assert blocks[0].block_type == SqlSegmentType.CREATE_FUNCTION
    block = blocks[1]
    assert block.block_type == SqlSegmentType.CREATE_TABLE
    assert block.batch_size == 10
    assert block.table == "output"
    sql_text = block.result_expression.sql(dialect="spark")
    assert "ADD_ONE(value)" in sql_text


def test_parse_no_blocks_raises():
    script = "CREATE FUNCTION noop(x) AS SELECT x;"
    blocks = AgentCICDScriptParser(script).parse()
    assert len(blocks) == 1
    assert blocks[0].block_type == SqlSegmentType.CREATE_FUNCTION


def test_recursive_function_detection():
    script = """
    CREATE FUNCTION loop(x) AS SELECT loop(x);
    CREATE BATCH TABLE out SELECT loop(value) FROM source_table;
    """
    with pytest.raises(ValueError, match="Recursive function call detected"):
        AgentCICDScriptParser(script).parse()


def test_function_call_argument_mismatch():
    script = """
    CREATE FUNCTION add(x, y) AS SELECT x + y;
    CREATE BATCH TABLE out SELECT add(value) FROM source_table;
    """
    with pytest.raises(ValueError, match="missing required arguments: y"):
        AgentCICDScriptParser(script).parse()


def test_option_value_to_python_array_roundtrip():
    expr = sqlglot.parse_one("SELECT 1", read="spark")
    option_expr = sqlglot.parse_one("ARRAY('a','b')", read="spark")
    assert option_expr is not None
    values = AgentCICDScriptParser._option_value_to_python(option_expr)
    assert values == ["a", "b"]


def test_parse_publish_reports_metric():
    """Test parsing PUBLISH <table> TO REPORTS metric statement."""
    script = """
    LOAD data FROM 's3://bucket/data';
    CREATE BATCH TABLE results SELECT metric, value, tags FROM data;
    PUBLISH results TO REPORTS WITH (COMPONENT = METRIC);
    """
    blocks = AgentCICDScriptParser(script).parse()

    assert len(blocks) == 3
    assert blocks[0].block_type == SqlSegmentType.LOAD_TABLE
    assert blocks[1].block_type == SqlSegmentType.CREATE_TABLE
    assert blocks[2].block_type == SqlSegmentType.PUBLISH_REPORTS
    assert blocks[2].table == "results"
    assert blocks[2].report_component == "metric"


def test_parse_publish_reports_chart_case_insensitive():
    """Test that PUBLISH TO REPORTS options are case insensitive."""
    script = """
    CREATE BATCH TABLE scores_table SELECT metric, value FROM data;
    PUBLISH scores_table TO reports WITH (component = chart, chart_type = bar, x_axis = metric, y_axis = value);
    """
    blocks = AgentCICDScriptParser(script).parse()

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_REPORTS
    assert blocks[1].table == "scores_table"
    assert blocks[1].report_component == "chart"
    assert blocks[1].chart_type == "bar"
    assert blocks[1].report_options["x_axis"] == "metric"
    assert blocks[1].report_options["y_axis"] == "value"


def test_parse_publish_dataset():
    script = """
    CREATE BATCH TABLE output SELECT * FROM data;
    PUBLISH output TO DATASET 'counterfactual-output';
    """
    blocks = AgentCICDScriptParser(script).parse()

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_DATASET
    assert blocks[1].table == "output"
    assert blocks[1].publish_name == "counterfactual-output"


def test_parse_publish_invalid_destination():
    """Test that PUBLISH with invalid destination raises error."""
    script = """
    CREATE BATCH TABLE output SELECT * FROM data;
    PUBLISH output AS INVALID;
    """
    with pytest.raises(Exception, match="REPORTS, DATASET, or ANNOTATION"):
        AgentCICDScriptParser(script).parse()


def test_parse_publish_missing_to_or_as():
    """Test that PUBLISH without TO or AS raises error."""
    script = """
    CREATE BATCH TABLE output SELECT * FROM data;
    PUBLISH output SCORES;
    """
    with pytest.raises(Exception, match="TO"):
        AgentCICDScriptParser(script).parse()
