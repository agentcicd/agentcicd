import pytest
from sqlglot.errors import ParseError
from pathlib import Path

from agentcicd.sql.parsing.parser import AgentCICDScriptParser
from agentcicd.sql.parsing.segmentation import segment_sql
from agentcicd.sql.parsing.sql_segments import SqlSegmentType
from agentcicd.sql.engine.entrypoint import EngineEntrypoint

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HELPFULNESS_FIXTURE_PATH = (
    _REPO_ROOT / "benchmarks/tests/fixtures/customer_support_helpfulness.sql"
)


@pytest.fixture
def parse_blocks():
    def _parse(script: str):
        return AgentCICDScriptParser(script).parse()

    return _parse


def _table_blocks(blocks):
    return [block for block in blocks if block.block_type == SqlSegmentType.CREATE_TABLE]


def _function_blocks(blocks):
    return [block for block in blocks if block.block_type == SqlSegmentType.CREATE_FUNCTION]


def test_parse_create_stream_table_without_options(parse_blocks):
    script = "CREATE STREAM TABLE stream_out SELECT * FROM source_table;"
    blocks = parse_blocks(script)

    assert len(blocks) == 1
    block = blocks[0]
    assert block.block_type == SqlSegmentType.CREATE_TABLE
    assert block.table == "stream_out"
    assert block.phase_type == "STREAM"
    assert block.batch_size is None
    assert "FROM source_table" in block.result_expression.sql(dialect="spark")


def test_parse_load_with_mixed_options(parse_blocks):
    script = (
        "LOAD sales FROM 's3://bucket/sales' "
        "WITH SPLITS=('train','test'), MODE=overwrite;"
    )
    blocks = parse_blocks(script)

    assert len(blocks) == 1
    load_block = blocks[0]
    assert load_block.block_type == SqlSegmentType.LOAD_TABLE
    assert load_block.options == {"SPLITS": ["train", "test"], "MODE": "overwrite"}


def test_parse_load_with_wrap_option(parse_blocks):
    script = "LOAD sales FROM 's3://bucket/sales' WITH FORMAT=csv, WRAP=cell;"
    blocks = parse_blocks(script)
    assert len(blocks) == 1
    assert blocks[0].block_type == SqlSegmentType.LOAD_TABLE
    assert blocks[0].options == {"FORMAT": "csv", "WRAP": "cell"}


def test_parse_standalone_row_limit_macro_placeholder(parse_blocks):
    script = """
    CREATE BATCH TABLE out
    SELECT *
    FROM source_table
    $LIMIT_ROWS;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 1
    assert blocks[0].block_type == SqlSegmentType.CREATE_TABLE


def test_parse_load_missing_from_raises():
    script = "LOAD sales 's3://bucket/sales';"
    with pytest.raises(ParseError, match="LOAD statement must include FROM"):
        AgentCICDScriptParser(script).parse()


def test_parse_save_missing_to_raises():
    script = "SAVE sales 's3://bucket/out';"
    with pytest.raises(ParseError, match="SAVE statement must include TO"):
        AgentCICDScriptParser(script).parse()


def test_function_inline_skips_table_qualified_identifiers(parse_blocks):
    script = """
    CREATE FUNCTION add_one(x) AS SELECT t.x + 1;
    CREATE BATCH TABLE out SELECT add_one(value) FROM source_table;
    """
    blocks = parse_blocks(script)

    assert len(_function_blocks(blocks)) == 1
    sql_text = _table_blocks(blocks)[0].result_expression.sql(dialect="spark")
    assert "ADD_ONE(value)" in sql_text


def test_parse_function_with_type_annotations(parse_blocks):
    """Test that functions with Spark SQL type annotations parse correctly."""
    script = """
    CREATE FUNCTION upper_text(text STRING)
    RETURNS STRING
    RETURN upper(text);

    CREATE BATCH TABLE out SELECT upper_text(name) FROM source_table;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    block = _table_blocks(blocks)[0]
    assert block.block_type == SqlSegmentType.CREATE_TABLE
    sql_text = block.result_expression.sql(dialect="spark")
    assert "UPPER_TEXT(name)" in sql_text


def test_parse_function_with_multiple_typed_parameters(parse_blocks):
    """Test function with multiple parameters having type annotations."""
    script = """
    CREATE FUNCTION concat_with_sep(text1 STRING, text2 STRING, sep STRING)
    RETURNS STRING
    RETURN concat(text1, sep, text2);

    CREATE BATCH TABLE out SELECT concat_with_sep(first_name, last_name, ' ') AS full_name FROM users;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    sql_text = _table_blocks(blocks)[0].result_expression.sql(dialect="spark")
    assert "CONCAT_WITH_SEP(first_name, last_name, ' ')" in sql_text


def test_parse_runtime_function_prefixes_to_single_part_aliases(parse_blocks):
    script = """
    CREATE BATCH TABLE out
    SELECT
      echo(value) AS a,
      embed_with_deps(value) AS b,
      container.ranker(value) AS c
    FROM source_table;
    """

    blocks = AgentCICDScriptParser(
        script,
        registered_functions=[
            {
                "name": "echo",
                "type": "py",
                "call_name": "echo",
                "runtime_alias": "echo",
                "signature": {"parameters": [{"name": "value", "has_default": False}]},
            },
            {
                "name": "embed_with_deps",
                "type": "pydeps",
                "call_name": "embed_with_deps",
                "runtime_alias": "embed_with_deps",
                "signature": {"parameters": [{"name": "value", "has_default": False}]},
            },
            {
                "name": "container.ranker",
                "type": "container",
                "call_name": "container.ranker",
                "runtime_alias": "container_ranker",
                "signature": {"parameters": [{"name": "value", "has_default": False}]},
            },
        ],
    ).parse()
    sql_text = blocks[0].result_expression.sql(dialect="spark")

    assert "ECHO(value)" in sql_text
    assert "EMBED_WITH_DEPS(NAMED_STRUCT('value', value))" in sql_text
    assert "CONTAINER_RANKER(NAMED_STRUCT('value', value))" in sql_text


def test_parse_local_sql_function_python_style_keyword_binding(parse_blocks):
    script = """
    CREATE FUNCTION concat_with_sep(text1 STRING, text2 STRING, sep STRING)
    RETURNS STRING
    RETURN concat(text1, sep, text2);

    CREATE BATCH TABLE out
    SELECT concat_with_sep(text2=last_name, sep=' ', text1=first_name) AS full_name
    FROM users;
    """

    blocks = parse_blocks(script)
    sql_text = _table_blocks(blocks)[0].result_expression.sql(dialect="spark")

    assert "CONCAT_WITH_SEP(first_name, last_name, ' ')" in sql_text


def test_parse_registered_runtime_function_lowers_to_named_struct():
    script = """
    CREATE BATCH TABLE out
    SELECT embed_with_deps(model='bge', text=value) AS embedding
    FROM source_table;
    """

    blocks = AgentCICDScriptParser(
        script,
        registered_functions=[
            {
                "name": "embed_with_deps",
                "type": "pydeps",
                "call_name": "embed_with_deps",
                "runtime_alias": "embed_with_deps",
                "signature": {
                    "parameters": [
                        {"name": "text", "has_default": False},
                        {"name": "model", "has_default": True},
                    ]
                },
            }
        ],
    ).parse()
    sql_text = blocks[0].result_expression.sql(dialect="spark")

    assert "EMBED_WITH_DEPS(NAMED_STRUCT('text', value, 'model', 'bge'))" in sql_text


def test_parse_registered_python_runtime_function_preserves_ordered_arguments():
    script = """
    CREATE BATCH TABLE out
    SELECT aisystems.llm.chat(
      messages=[{'role': 'user', 'content': value}],
      aisystem_id='aisystem.test',
      response_format={'type': 'json_object'}
    ) AS response_raw
    FROM source_table;
    """

    blocks = AgentCICDScriptParser(
        script,
        registered_functions=[
            {
                "name": "aisystems.llm.chat",
                "type": "py",
                "call_name": "aisystems.llm.chat",
                "runtime_alias": "aisystems_llm_chat",
                "signature": {
                    "parameters": [
                        {"name": "aisystem_id", "has_default": False},
                        {"name": "messages", "has_default": False},
                        {"name": "response_format", "has_default": True},
                    ]
                },
            }
        ],
    ).parse()
    sql_text = blocks[0].result_expression.sql(dialect="spark")

    assert "AISYSTEMS_LLM_CHAT(" in sql_text
    assert "AISYSTEMS_LLM_CHAT(NAMED_STRUCT(" not in sql_text
    assert "'aisystem.test', TO_VARIANT_OBJECT(ARRAY(" in sql_text


def test_engine_lowering_registered_python_runtime_function_preserves_declared_argument_order():
    script = """
    CREATE BATCH TABLE out
    SELECT aisystems.llm.chat(
      messages=[{'role': 'user', 'content': value}],
      aisystem_id='aisystem.test',
      response_format={'type': 'json_object'}
    ) AS response_raw
    FROM source_table;
    """

    lowered_sql = EngineEntrypoint(
        script,
        registered_functions=[
            {
                "name": "aisystems.llm.chat",
                "type": "py",
                "call_name": "aisystems.llm.chat",
                "runtime_alias": "aisystems_llm_chat",
                "signature": {
                    "parameters": [
                        {"name": "aisystem_id", "has_default": False},
                        {"name": "messages", "has_default": False},
                        {"name": "request_timeout", "has_default": True},
                        {"name": "response_format", "has_default": True},
                    ]
                },
            }
        ],
    ).lower_script()[0]

    assert "AISYSTEMS_LLM_CHAT('aisystem.test', TO_VARIANT_OBJECT(ARRAY(" in lowered_sql
    assert "TO_VARIANT_OBJECT(" in lowered_sql
    assert "'json_object'" in lowered_sql


def test_segment_sql_does_not_require_variant_semantics_to_succeed():
    script = """
    CREATE BATCH TABLE evaluated
    SELECT helpfulness:score AS helpfulness_score
    FROM source_table;
    """

    segmentation = segment_sql(script)

    assert len(segmentation.tables) == 1
    assert segmentation.tables[0].table == "evaluated"
    assert "helpfulness:score" in segmentation.tables[0].sql_text


def test_parse_registered_runtime_function_rejects_positional_after_keyword():
    script = """
    CREATE BATCH TABLE out
    SELECT embed_with_deps(model='bge', value)
    FROM source_table;
    """

    with pytest.raises(ValueError, match="cannot use positional arguments after keyword arguments"):
        AgentCICDScriptParser(
            script,
            registered_functions=[
                {
                    "name": "embed_with_deps",
                    "type": "pydeps",
                    "call_name": "embed_with_deps",
                    "runtime_alias": "embed_with_deps",
                    "signature": {
                        "parameters": [
                            {"name": "text", "has_default": False},
                            {"name": "model", "has_default": True},
                        ]
                    },
                }
            ],
        ).parse()


def test_parse_variant_colon_operator_lowers_to_try_variant_get(parse_blocks):
    script = """
    CREATE BATCH TABLE out
    SELECT response_raw:choices[0]:message:content:score AS score
    FROM dataset;
    """

    blocks = parse_blocks(script)
    sql_text = _table_blocks(blocks)[0].result_expression.sql(dialect="spark")

    assert "__AGENTCICD_COLON_PATH(response_raw, '$.choices[0].message.content.score')" in sql_text


def test_discover_external_function_references_finds_registered_udfs_and_ignores_builtins():
    script = """
    CREATE FUNCTION local.wrap_score(question STRING, candidate_answer STRING) RETURNS DOUBLE
    RETURN customer_support.helpfulness_judge(
      question => question,
      candidate_answer => candidate_answer
    );

    CREATE BATCH TABLE evaluated
    SELECT
      aisystems.llm.chat(
        messages => ARRAY(MAP('role', 'user', 'content', question)),
        aisystem_id => 'aisystem.123'
      ) AS reply,
      local.wrap_score(question, candidate_answer) AS score,
      AVG(1) AS avg_value
    FROM prepared;
    """

    references = AgentCICDScriptParser.discover_external_function_references(
        script,
        registered_functions=[
            {
                "id": "fixture.helpfulness",
                "name": "customer_support.helpfulness_judge",
                "type": "sql",
                "call_name": "customer_support.helpfulness_judge",
                "runtime_alias": "customer_support_helpfulness_judge",
                "source_text": "CREATE FUNCTION customer_support.helpfulness_judge(question STRING, candidate_answer STRING) RETURNS DOUBLE RETURN 1.0;",
                "signature": {
                    "parameters": [
                        {"name": "question", "has_default": False},
                        {"name": "candidate_answer", "has_default": False},
                    ]
                },
            },
            {
                "id": "fixture.llm.chat",
                "name": "aisystems.llm.chat",
                "type": "py",
                "call_name": "aisystems.llm.chat",
                "runtime_alias": "aisystems_llm_chat",
                "signature": {
                    "parameters": [
                        {"name": "messages", "has_default": False},
                        {"name": "aisystem_id", "has_default": False},
                    ]
                },
            },
        ],
    )

    assert references == [
        "aisystems.llm.chat",
        "customer_support.helpfulness_judge",
    ]


def test_parse_unknown_runtime_function_alias_is_preserved():
    script = """
    CREATE BATCH TABLE out
    SELECT http.myfunction.returns_1(ticker) AS metric
    FROM dataset;
    """

    segments = AgentCICDScriptParser(script, registered_functions=[]).parse()
    assert len(segments) == 1
    assert any("HTTP_MYFUNCTION_RETURNS_1" in statement for statement in segments[0].statements_sql)


def test_parse_publish_to_reports(parse_blocks):
    """Test PUBLISH <table> TO REPORTS syntax."""
    script = """
    CREATE BATCH TABLE results SELECT metric, value FROM data;
    PUBLISH results TO REPORTS WITH (COMPONENT = METRIC);
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_REPORTS
    assert blocks[1].table == "results"
    assert blocks[1].report_component == "metric"


def test_publish_to_reports_metric_requires_metric_value_columns(parse_blocks):
    script = """
    CREATE BATCH TABLE summary
    SELECT
      count(*) AS row_count,
      count(DISTINCT channel) AS channel_count,
      count(DISTINCT region) AS region_count
    FROM evaluated;
    PUBLISH summary TO REPORTS WITH (COMPONENT = METRIC);
    """
    with pytest.raises(Exception, match="must project 'metric' and 'value' columns"):
        parse_blocks(script)


def test_parse_publish_to_dataset_with_string_name(parse_blocks):
    script = """
    CREATE BATCH TABLE results SELECT * FROM data;
    PUBLISH results TO DATASET 'counterfactual-results';
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_DATASET
    assert blocks[1].table == "results"
    assert blocks[1].publish_name == "counterfactual-results"


def test_parse_publish_to_dataset_without_name(parse_blocks):
    script = """
    CREATE BATCH TABLE results SELECT * FROM data;
    PUBLISH results TO DATASET;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_DATASET
    assert blocks[1].publish_name is None


def test_parse_publish_to_annotation_queue_with_alias_and_options(parse_blocks):
    """Test PUBLISH <table> TO ANNOTATION QUEUE '<name>' AS <alias> WITH (...) syntax."""
    script = """
    CREATE BATCH TABLE samples SELECT text, label FROM data;
    PUBLISH samples TO ANNOTATION QUEUE 'intent review' AS intent_review
    WITH (
      INSTRUCTIONS = 'Label intent',
      REVIEWERS_PER_TASK = 3,
      RESERVATION_MINUTES = 30,
      CONSENSUS = 'majority',
      TEMPLATE = '<View />'
    );
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_ANNOTATION
    assert blocks[1].table == "samples"
    assert blocks[1].queue_name == "intent review"
    assert blocks[1].publish_alias == "intent_review"
    assert blocks[1].options == {
        "INSTRUCTIONS": "Label intent",
        "REVIEWERS_PER_TASK": "3",
        "RESERVATION_MINUTES": "30",
        "CONSENSUS": "majority",
        "TEMPLATE": "<View />",
    }


def test_parse_publish_to_annotation_queue_without_alias(parse_blocks):
    """Test PUBLISH <table> TO ANNOTATION QUEUE <name> syntax without an alias."""
    script = """
    CREATE BATCH TABLE samples SELECT text, label FROM data;
    PUBLISH samples TO ANNOTATION QUEUE intent_review;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    assert blocks[1].block_type == SqlSegmentType.PUBLISH_ANNOTATION
    assert blocks[1].table == "samples"
    assert blocks[1].queue_name == "intent_review"
    assert blocks[1].publish_alias is None


def test_parse_retrieve_annotation_results(parse_blocks):
    """Test RETRIEVE ANNOTATION RESULTS <table> FROM <publish alias> syntax."""
    script = """
    RETRIEVE ANNOTATION RESULTS labeled_data FROM intent_review;
    CREATE BATCH TABLE processed SELECT * FROM labeled_data;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    assert blocks[0].block_type == SqlSegmentType.RETRIEVE_ANNOTATION
    assert blocks[0].table == "labeled_data"
    assert blocks[0].source_ref == "intent_review"
    assert blocks[0].annotation_request_id is None
    assert blocks[1].block_type == SqlSegmentType.CREATE_TABLE


def test_parse_retrieve_annotation_results_from_request_id(parse_blocks):
    """Test RETRIEVE ANNOTATION RESULTS with an explicit annotation request ID."""
    script = """
    RETRIEVE ANNOTATION RESULTS annotations FROM ANNOTATION REQUEST 'annreq.abc123';
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 1
    assert blocks[0].block_type == SqlSegmentType.RETRIEVE_ANNOTATION
    assert blocks[0].table == "annotations"
    assert blocks[0].source_ref == "annreq.abc123"
    assert blocks[0].annotation_request_id == "annreq.abc123"


def test_parse_publish_invalid_destination_raises():
    """Test PUBLISH with invalid destination raises error."""
    script = """
    CREATE BATCH TABLE output SELECT * FROM data;
    PUBLISH output TO INVALID_DEST;
    """
    with pytest.raises(Exception, match="REPORTS, DATASET, or ANNOTATION"):
        AgentCICDScriptParser(script).parse()


def test_parse_function_with_triple_quoted_strings_in_body(parse_blocks):
    script = '''
    CREATE FUNCTION judge_prompt(question STRING, answer STRING)
    RETURNS STRING
    RETURN concat(
      """
You are a judge.
Question:
""",
      question,
      """

Answer:
""",
      answer
    );

    CREATE BATCH TABLE out SELECT judge_prompt(question, answer) AS prompt FROM source_table;
    '''
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    function_sql = _function_blocks(blocks)[0].statements_sql[0]
    sql_text = _table_blocks(blocks)[0].result_expression.sql(dialect="spark")
    assert "You are a judge." in function_sql
    assert "Question:" in function_sql
    assert "Answer:" in function_sql
    assert "CONCAT(" in function_sql
    assert "JUDGE_PROMPT(question, answer)" in sql_text


def test_parse_native_sql_function_with_characteristics_and_query_body(parse_blocks):
    script = """
    CREATE FUNCTION area(width DOUBLE, height DOUBLE)
    RETURNS DOUBLE
    DETERMINISTIC
    CONTAINS SQL
    RETURN SELECT width * height;

    CREATE BATCH TABLE out
    SELECT area(w, h) AS area
    FROM source_table;
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 2
    function_sql = _function_blocks(blocks)[0].statements_sql[0]
    sql_text = _table_blocks(blocks)[0].result_expression.sql(dialect="spark")
    assert "RETURN (SELECT width * height)" in function_sql
    assert "AREA(w, h)" in sql_text


def test_parse_retrieve_missing_annotation_keyword_raises():
    """Test RETRIEVE without ANNOTATION keyword raises error."""
    script = "RETRIEVE RESULTS table FROM something;"
    with pytest.raises(ParseError, match="ANNOTATION"):
        AgentCICDScriptParser(script).parse()


def test_parse_retrieve_missing_results_keyword_raises():
    """Test RETRIEVE ANNOTATION without RESULTS keyword raises error."""
    script = "RETRIEVE ANNOTATION table FROM ANNOTATION id;"
    with pytest.raises(ParseError, match="RESULTS"):
        AgentCICDScriptParser(script).parse()


def test_parser_normalizes_python_collection_literals():
    script = """
    CREATE BATCH TABLE out
    SELECT {'name': name, 'scores': [1, 2, 3]} AS payload
    FROM source_table;
    """
    blocks = AgentCICDScriptParser(script).parse()

    assert len(blocks) == 1
    sql_text = blocks[0].result_expression.sql(dialect="spark")
    assert "TO_VARIANT_OBJECT(NAMED_STRUCT(" in sql_text
    assert "ARRAY(1, 2, 3)" in sql_text


@pytest.mark.parametrize(
    "sql_path",
    sorted(Path("benchmarks/recipes").glob("*.sql")),
    ids=lambda path: path.name,
)
def test_all_benchmark_sql_scripts_parse_raw(sql_path: Path):
    sql_text = sql_path.read_text(encoding="utf-8")
    AgentCICDScriptParser(sql_text).parse()


def test_parser_normalizes_triple_quoted_strings():
    script = '''
    CREATE BATCH TABLE out
    SELECT """hello
world""" AS message;
    '''

    blocks = AgentCICDScriptParser(script).parse()

    assert len(blocks) == 1
    sql_text = blocks[0].result_expression.sql(dialect="spark")
    assert "'hello\\nworld'" in sql_text


def test_parser_rejects_python_f_strings():
    script = '''
    CREATE BATCH TABLE out
    SELECT f"value: {name}" AS message
    FROM source_table;
    '''

    with pytest.raises(ValueError, match="f-strings are not supported"):
        AgentCICDScriptParser(script).parse()


def test_parse_full_annotation_workflow(parse_blocks):
    """Test complete annotation workflow with PUBLISH and RETRIEVE."""
    script = """
    LOAD raw_data FROM 's3://bucket/input';
    CREATE BATCH TABLE samples SELECT id, text FROM raw_data LIMIT 100;
    PUBLISH samples TO ANNOTATION QUEUE 'labeling' AS labeling_request;
    RETRIEVE ANNOTATION RESULTS labeled FROM labeling_request;
    CREATE BATCH TABLE results SELECT id, text, _annotation FROM labeled;
    CREATE BATCH TABLE score_rows SELECT 'annotation_rows' AS metric, count(*) AS value FROM results;
    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    """
    blocks = parse_blocks(script)

    assert len(blocks) == 7
    assert blocks[0].block_type == SqlSegmentType.LOAD_TABLE
    assert blocks[1].block_type == SqlSegmentType.CREATE_TABLE
    assert blocks[2].block_type == SqlSegmentType.PUBLISH_ANNOTATION
    assert blocks[3].block_type == SqlSegmentType.RETRIEVE_ANNOTATION
    assert blocks[4].block_type == SqlSegmentType.CREATE_TABLE
    assert blocks[5].block_type == SqlSegmentType.CREATE_TABLE
    assert blocks[6].block_type == SqlSegmentType.PUBLISH_REPORTS


def test_parse_assigns_segment_dependencies(parse_blocks):
    script = """
    LOAD raw_data FROM 's3://bucket/input';
    CREATE BATCH TABLE samples SELECT id, text FROM raw_data;
    PUBLISH samples TO ANNOTATION QUEUE 'labeling' AS labeling_request;
    RETRIEVE ANNOTATION RESULTS labeled FROM labeling_request;
    CREATE BATCH TABLE results SELECT id, text, _annotation FROM labeled;
    CREATE BATCH TABLE score_rows SELECT 'annotation_rows' AS metric, count(*) AS value FROM results;
    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    """
    blocks = parse_blocks(script)

    assert blocks[1].dependencies == [blocks[0].segment_id]
    assert blocks[2].dependencies == [blocks[1].segment_id]
    assert blocks[3].dependencies == [blocks[2].segment_id]
    assert blocks[5].dependencies == [blocks[4].segment_id]
    assert blocks[6].dependencies == [blocks[5].segment_id]


def test_parse_publish_dataset_assigns_dependencies(parse_blocks):
    script = """
    LOAD raw_data FROM 's3://bucket/input';
    CREATE BATCH TABLE results SELECT * FROM raw_data;
    PUBLISH results TO DATASET 'published-results';
    """
    blocks = parse_blocks(script)

    assert blocks[1].dependencies == [blocks[0].segment_id]
    assert blocks[2].block_type == SqlSegmentType.PUBLISH_DATASET
    assert blocks[2].dependencies == [blocks[1].segment_id]
