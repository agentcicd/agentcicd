from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.statements import BatchTableStmt, LoadStmt, PublishAnnotationStmt, RetrieveAnnotationStmt, SaveStmt, StreamTableStmt
from agentcicd.sql.surface.top_level_parser import TopLevelParser


def test_custom_statement_parser_supports_load_save_with_options():
    script = """
    LOAD sales FROM 's3://bucket/sales'
    WITH FORMAT='csv', SPLITS=('train', 'test'), WRAP_CELLS='true';

    SAVE sales TO 's3://bucket/out'
    WITH FORMAT='delta';
    """

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], LoadStmt)
    assert statements[0].options == {
        "format": "csv",
        "splits": ["train", "test"],
        "wrap_cells": "true",
    }
    assert isinstance(statements[1], SaveStmt)
    assert statements[1].options == {"format": "delta"}


def test_custom_statement_parser_supports_load_limit():
    script = "LOAD sales FROM 's3://bucket/sales' WITH FORMAT='jsonl' LIMIT 25;"

    statements = TopLevelParser(script).parse()
    plan = EngineEntrypoint(script).compile_plan()

    assert isinstance(statements[0], LoadStmt)
    assert statements[0].limit == 25
    assert plan[0].payload["limit"] == 25


def test_custom_statement_parser_supports_load_options_without_with():
    script = "LOAD raw_cases FROM dataset FORMAT = jsonl;"

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], LoadStmt)
    assert statements[0].path == "dataset"
    assert statements[0].options == {"format": "jsonl"}


def test_custom_statement_parser_supports_publish_annotation_queue_form():
    script = """
    PUBLISH examples TO ANNOTATION QUEUE 'intent review'
    AS intent_review
    WITH REVIEWERS_PER_TASK = 3, RESERVATION_MINUTES = 30, CONSENSUS = 'majority', TEMPLATE = '<View />';
    """

    statements = TopLevelParser(script).parse()

    assert len(statements) == 1
    assert isinstance(statements[0], PublishAnnotationStmt)
    assert statements[0].table == "examples"
    assert statements[0].queue_name == "intent review"
    assert statements[0].alias == "intent_review"
    assert statements[0].options == {
        "reviewers_per_task": "3",
        "reservation_minutes": "30",
        "consensus": "majority",
        "template": "<View />",
    }


def test_custom_statement_parser_supports_retrieve_annotation_results_form():
    script = "RETRIEVE ANNOTATION RESULTS labeled_data FROM intent_review;"

    statements = TopLevelParser(script).parse()

    assert len(statements) == 1
    assert isinstance(statements[0], RetrieveAnnotationStmt)
    assert statements[0].table == "labeled_data"
    assert statements[0].source_ref == "intent_review"


def test_top_level_parser_supports_stream_options_batch_size():
    script = """
    CREATE STREAM TABLE live_scores
    OPTIONS (BATCH_SIZE=50)
    SELECT score
    FROM prepared;
    """

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], StreamTableStmt)
    assert statements[0].batch_size == 50


def test_engine_compile_plan_includes_custom_steps_and_sql_steps():
    script = """
    LOAD sales FROM 's3://bucket/sales'
    WITH FORMAT='csv', SPLITS=('train', 'test');

    CREATE BATCH TABLE out
    SELECT q FROM sales;

    SAVE out TO 's3://bucket/out'
    WITH FORMAT='delta';

    PUBLISH out TO DATASET 'customer-support';
    """

    entrypoint = EngineEntrypoint(script)

    plan = entrypoint.compile_plan(include_cells=True)

    assert [step.kind for step in plan] == [
        "load_table",
        "create_batch_table",
        "save_table",
        "publish_dataset",
    ]
    assert plan[0].payload["options"]["splits"] == ["train", "test"]
    assert plan[1].payload["sql"] == "SELECT q AS q FROM sales"
    assert plan[2].payload["options"]["format"] == "delta"
    assert plan[3].payload["dataset_name"] == "customer-support"


def test_engine_compile_plan_links_annotation_publish_alias_to_retrieve():
    script = """
    LOAD prepared FROM 's3://bucket/prepared';

    CREATE BATCH TABLE examples
    SELECT q FROM prepared;

    PUBLISH examples TO ANNOTATION QUEUE 'intent review'
    AS intent_review
    WITH TEMPLATE = '<View />';

    RETRIEVE ANNOTATION RESULTS labeled FROM intent_review;
    """

    plan = EngineEntrypoint(script).compile_plan()

    assert [step.kind for step in plan] == [
        "load_table",
        "create_batch_table",
        "publish_annotation",
        "retrieve_annotation",
    ]
    assert plan[2].payload["queue_name"] == "intent review"
    assert plan[2].payload["alias"] == "intent_review"
    assert plan[3].payload["source_ref"] == "intent_review"
    assert plan[3].dependencies == ["publish:annotation:intent_review"]


def test_engine_compile_plan_orders_steps_by_dependencies():
    script = """
    SAVE out TO 's3://bucket/out' WITH FORMAT='delta';

    LOAD prepared FROM 's3://bucket/prepared';

    CREATE BATCH TABLE out
    SELECT q FROM prepared;
    """

    plan = EngineEntrypoint(script).compile_plan()

    assert [step.kind for step in plan] == ["load_table", "create_batch_table", "save_table"]
    assert plan[2].dependencies == ["table:out"]


def test_top_level_parser_handles_comments_and_embedded_keywords_in_paths():
    script = """
    -- load input
    LOAD sales FROM 's3://bucket/path/with/PUBLISH/in/name.csv'
    WITH FORMAT='csv';

    /* persist result */
    SAVE sales TO 's3://bucket/out'
    WITH FORMAT='delta';
    """

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], LoadStmt)
    assert statements[0].path == "s3://bucket/path/with/PUBLISH/in/name.csv"
    assert isinstance(statements[1], SaveStmt)
    assert statements[1].path == "s3://bucket/out"


def test_top_level_parser_handles_stream_options_with_comments():
    script = """
    CREATE STREAM TABLE live_scores
    OPTIONS (
      BATCH_SIZE=25 /* available now chunking */
    )
    SELECT score
    FROM prepared;
    """

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], StreamTableStmt)
    assert statements[0].batch_size == 25


def test_top_level_parser_preserves_table_column_semantics_options():
    script = """
    CREATE STREAM TABLE generated
    OPTIONS (
      BATCH_SIZE = 25,
      COLUMN_SEMANTICS = {
        'assistant_answer': {'type': 'markdown'},
        'trajectory': {'type': 'trace', 'format': 'otel'},
        'judge_payload': {'type': 'code', 'language': 'json'}
      }
    )
    SELECT assistant_answer, trajectory, judge_payload
    FROM prepared;
    """

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], StreamTableStmt)
    assert statements[0].batch_size == 25
    assert statements[0].options.to_dict()["column_semantics"] == {
        "assistant_answer": {"type": "markdown"},
        "trajectory": {"type": "trace", "format": "otel"},
        "judge_payload": {"type": "code", "language": "json"},
    }


def test_top_level_parser_preserves_table_description_option():
    script = """
    CREATE BATCH TABLE evaluated
    OPTIONS (
      DESCRIPTION = 'Scores target answers and keeps judge evidence for review'
    )
    SELECT score
    FROM judged;
    """

    statements = TopLevelParser(script).parse()

    assert isinstance(statements[0], BatchTableStmt)
    assert statements[0].options.to_dict()["description"] == "Scores target answers and keeps judge evidence for review"
