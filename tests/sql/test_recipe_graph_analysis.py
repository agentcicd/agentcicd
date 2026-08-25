from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agentcicd" / "src"))

from agentcicd.sql.analysis import MappingRecipeGraphCallbacks, build_recipe_dependency_graph  # noqa: E402
from agentcicd.sql.segmentation import segment_sql  # noqa: E402


def test_build_recipe_dependency_graph_connects_declared_inputs_to_referencing_steps() -> None:
    sql = """
    DECLARE INPUT target_model AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.gpt4o';

    DECLARE INPUT num_cases INT DEFAULT 5;

    CREATE STREAM TABLE generated
    SELECT aisystems.llm.chat(aisystem_id = target_model) AS response
    FROM generate_series(1, num_cases);
    """

    segmentation = segment_sql(sql)
    input_segments = [
        {
            "name": item.name,
            "type": item.input_type,
            "options": item.options,
            "default": item.default,
            "required": item.required,
            "source_text": item.sql_text,
        }
        for item in segmentation.inputs
    ]
    table_segments = [
        {
            "table": item.table,
            "phase_type": item.phase_type,
            "batch_size": item.batch_size,
            "source_text": item.sql_text,
            "query_sql": item.query_sql,
            "depends_on": item.depends_on,
        }
        for item in segmentation.tables
    ]

    nodes, edges = build_recipe_dependency_graph(
        segmentation=segmentation,
        input_segments=input_segments,
        function_segments=[],
        load_segments=[],
        table_segments=table_segments,
        save_segments=[],
        publish_segments=[],
        publish_annotation_segments=[],
        retrieve_annotation_segments=[],
        registered_function_names=set(),
    )

    node_types = {node.id: node.type for node in nodes}
    edge_tuples = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

    assert node_types["input:target_model"] == "input"
    assert node_types["input:num_cases"] == "input"
    assert ("input:target_model", "table:0", "uses_input") in edge_tuples
    assert ("input:num_cases", "table:0", "uses_input") in edge_tuples


def test_build_recipe_dependency_graph_connects_table_pool_option_to_table() -> None:
    sql = """
    DECLARE INPUT executor_pool POOL
    WITH kind = 'executor'
    DEFAULT {'kind': 'executor', 'max_workers': 1};

    CREATE BATCH TABLE scored
    OPTIONS (POOL = executor_pool)
    SELECT 1 AS score;
    """

    segmentation = segment_sql(sql)
    input_segments = [
        {
            "name": item.name,
            "type": item.input_type,
            "options": item.options,
            "default": item.default,
            "required": item.required,
            "source_text": item.sql_text,
        }
        for item in segmentation.inputs
    ]
    table_segments = [
        {
            "table": item.table,
            "phase_type": item.phase_type,
            "batch_size": item.batch_size,
            "options": item.options,
            "source_text": item.sql_text,
            "query_sql": item.query_sql,
            "depends_on": item.depends_on,
        }
        for item in segmentation.tables
    ]

    _nodes, edges = build_recipe_dependency_graph(
        segmentation=segmentation,
        input_segments=input_segments,
        function_segments=[],
        load_segments=[],
        table_segments=table_segments,
        save_segments=[],
        publish_segments=[],
        publish_annotation_segments=[],
        retrieve_annotation_segments=[],
        registered_function_names=set(),
    )

    edge_tuples = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

    assert ("input:executor_pool", "table:0", "uses_input") in edge_tuples


def test_build_recipe_dependency_graph_connects_declared_dataset_input_to_load_source() -> None:
    sql = """
    DECLARE INPUT dataset DATASET DEFAULT 'agentcicd://dataset.9bf11ba2f85205ff';

    LOAD raw_cases FROM dataset WITH FORMAT = jsonl;

    CREATE BATCH TABLE prepared_cases
    SELECT case_id
    FROM raw_cases;
    """

    segmentation = segment_sql(sql)
    input_segments = [
        {
            "name": item.name,
            "type": item.input_type,
            "options": item.options,
            "default": item.default,
            "required": item.required,
            "source_text": item.sql_text,
        }
        for item in segmentation.inputs
    ]
    load_segments = [
        {
            "table": item.table,
            "source": item.source,
            "options": item.options,
            "source_text": item.sql_text,
        }
        for item in segmentation.loads
    ]
    table_segments = [
        {
            "table": item.table,
            "phase_type": item.phase_type,
            "batch_size": item.batch_size,
            "source_text": item.sql_text,
            "query_sql": item.query_sql,
            "depends_on": item.depends_on,
        }
        for item in segmentation.tables
    ]

    _nodes, edges = build_recipe_dependency_graph(
        segmentation=segmentation,
        input_segments=input_segments,
        function_segments=[],
        load_segments=load_segments,
        table_segments=table_segments,
        save_segments=[],
        publish_segments=[],
        publish_annotation_segments=[],
        retrieve_annotation_segments=[],
        registered_function_names=set(),
    )

    edge_tuples = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

    assert ("input:dataset", "load:0", "uses_input") in edge_tuples
    assert ("load:0", "table:0", "depends_on") in edge_tuples


def test_build_recipe_dependency_graph_includes_aisystem_and_secret_nodes() -> None:
    sql = """
    LOAD prepared FROM 'dataset.test';

    CREATE BATCH TABLE evaluated
    SELECT customer_support.helpfulness_judge(
      question=question,
      candidate_answer=candidate_answer,
      aisystem_id='aisystem.alpha123',
      secret_id='secret.alpha999'
    ) AS helpfulness
    FROM prepared;

    CREATE BATCH TABLE score_rows
    SELECT helpfulness AS score
    FROM evaluated;
    """

    segmentation = segment_sql(
        sql,
        registered_functions=[
            {
                "name": "customer_support.helpfulness_judge",
                "call_name": "customer_support.helpfulness_judge",
                "runtime_alias": "customer_support_helpfulness_judge",
                "signature": {
                    "parameters": [
                        {"name": "question", "has_default": False},
                        {"name": "candidate_answer", "has_default": False},
                        {"name": "aisystem_id", "has_default": False},
                        {"name": "secret_id", "has_default": False},
                    ]
                },
            }
        ],
    )

    function_segments: list[dict] = []
    load_segments = [
        {
            "table": item.table,
            "source": item.source,
            "options": item.options,
            "source_text": item.sql_text,
        }
        for item in segmentation.loads
    ]
    table_segments = [
        {
            "table": item.table,
            "phase_type": item.phase_type,
            "batch_size": item.batch_size,
            "source_text": item.sql_text,
            "query_sql": item.query_sql,
            "depends_on": item.depends_on,
        }
        for item in segmentation.tables
    ]

    nodes, edges = build_recipe_dependency_graph(
        segmentation=segmentation,
        function_segments=function_segments,
        load_segments=load_segments,
        table_segments=table_segments,
        save_segments=[],
        publish_segments=[],
        publish_annotation_segments=[],
        retrieve_annotation_segments=[],
        registered_function_names={"customer_support.helpfulness_judge"},
        callbacks=MappingRecipeGraphCallbacks(
            fixtures_by_id={},
            aisystems_by_id={
                "aisystem.alpha123": {
                    "id": "aisystem.alpha123",
                    "name": "Claude Haiku",
                }
            },
            secrets_by_id={
                "secret.alpha999": {
                    "id": "secret.alpha999",
                    "key": "anthropic_key",
                }
            },
        ),
    )

    node_index = {node.id: node for node in nodes}
    edge_tuples = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

    assert "aisystem:aisystem_alpha123" in node_index
    assert node_index["aisystem:aisystem_alpha123"].label == "Claude Haiku"
    assert "secret:secret_alpha999" in node_index
    assert node_index["secret:secret_alpha999"].label == "anthropic_key"
    assert "function_reference:customer_support_helpfulness_judge" in node_index
    assert ("load:0", "table:0", "depends_on") in edge_tuples
    assert ("function_reference:customer_support_helpfulness_judge", "table:0", "function_used") in edge_tuples
    assert ("aisystem:aisystem_alpha123", "table:0", "uses_aisystem") in edge_tuples
    assert ("secret:secret_alpha999", "table:0", "uses_secret") in edge_tuples


def test_build_recipe_dependency_graph_links_annotation_roundtrip_and_alias_dependencies() -> None:
    sql = """
    CREATE BATCH TABLE evaluated
    SELECT 1 AS case_id, 0.0 AS correctness_score;

    CREATE BATCH TABLE annotation_sample
    SELECT case_id, correctness_score
    FROM evaluated;

    PUBLISH annotation_sample TO ANNOTATION QUEUE 'math_eval_review' AS math_annotations
    WITH (TEMPLATE = '<View />');

    RETRIEVE ANNOTATION RESULTS annotations
    FROM math_annotations;

    CREATE BATCH TABLE annotation_metrics
    SELECT count(*) AS value
    FROM annotations;

    CREATE BATCH TABLE annotation_disagreements
    SELECT a.case_id
    FROM annotations a
    WHERE a.case_id IS NOT NULL;
    """

    segmentation = segment_sql(sql)
    table_segments = [
        {
            "table": item.table,
            "phase_type": item.phase_type,
            "batch_size": item.batch_size,
            "source_text": item.sql_text,
            "query_sql": item.query_sql,
            "depends_on": item.depends_on,
        }
        for item in segmentation.tables
    ]
    publish_annotation_segments = [
        {
            "table": item.table,
            "queue_name": item.queue_name,
            "alias": item.alias,
            "options": item.options,
            "source_text": item.sql_text,
        }
        for item in segmentation.publish_annotations
    ]
    retrieve_annotation_segments = [
        {
            "table": item.table,
            "source_ref": item.source_ref,
            "annotation_request_id": item.annotation_request_id,
            "source_text": item.sql_text,
        }
        for item in segmentation.retrieve_annotations
    ]

    _nodes, edges = build_recipe_dependency_graph(
        segmentation=segmentation,
        function_segments=[],
        load_segments=[],
        table_segments=table_segments,
        save_segments=[],
        publish_segments=[],
        publish_annotation_segments=publish_annotation_segments,
        retrieve_annotation_segments=retrieve_annotation_segments,
        registered_function_names=set(),
    )

    edge_tuples = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

    assert ("table:0", "table:1", "depends_on") in edge_tuples
    assert ("table:1", "publish_annotation:0", "publish_annotation") in edge_tuples
    assert ("publish_annotation:0", "retrieve_annotation:0", "annotation_roundtrip") in edge_tuples
    assert ("retrieve_annotation:0", "table:2", "depends_on") in edge_tuples
    assert ("retrieve_annotation:0", "table:3", "depends_on") in edge_tuples


def test_build_recipe_dependency_graph_types_report_publish_components() -> None:
    sql = """
    CREATE BATCH TABLE metrics
    SELECT 'accuracy' AS metric, 1.0 AS value;

    CREATE BATCH TABLE chart_rows
    SELECT 'a' AS label, 1.0 AS value;

    CREATE BATCH TABLE issues
    SELECT 'issue' AS title, 'low' AS severity, 'details' AS description;

    PUBLISH metrics TO REPORTS WITH (COMPONENT = METRIC);
    PUBLISH chart_rows TO REPORTS WITH (COMPONENT = CHART, CHART_TYPE = BAR, X_AXIS = label, Y_AXIS = value);
    PUBLISH issues TO REPORTS WITH (COMPONENT = ISSUE);
    """

    segmentation = segment_sql(sql)
    table_segments = [
        {
            "table": item.table,
            "phase_type": item.phase_type,
            "batch_size": item.batch_size,
            "source_text": item.sql_text,
            "query_sql": item.query_sql,
            "depends_on": item.depends_on,
        }
        for item in segmentation.tables
    ]
    publish_segments = [
        {
            "table": item.table,
            "destination": item.destination,
            "published_name": item.published_name,
            "component": item.component,
            "chart_type": item.chart_type,
            "report_options": item.report_options,
            "source_text": item.sql_text,
        }
        for item in segmentation.publishes
    ]

    nodes, edges = build_recipe_dependency_graph(
        segmentation=segmentation,
        function_segments=[],
        load_segments=[],
        table_segments=table_segments,
        save_segments=[],
        publish_segments=publish_segments,
        publish_annotation_segments=[],
        retrieve_annotation_segments=[],
        registered_function_names=set(),
    )

    node_types = {node.id: node.type for node in nodes}
    edge_tuples = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

    assert node_types["publish:0"] == "publish_report_metric"
    assert node_types["publish:1"] == "publish_report_chart"
    assert node_types["publish:2"] == "publish_report_issue"
    assert ("table:0", "publish:0", "publish_report_metric") in edge_tuples
    assert ("table:1", "publish:1", "publish_report_chart") in edge_tuples
    assert ("table:2", "publish:2", "publish_report_issue") in edge_tuples
