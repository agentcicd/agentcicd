from agentcicd.sql.engine.entrypoint import EngineEntrypoint


def test_dependency_graph_tracks_tables_and_functions():
    script = """
    CREATE FUNCTION customer_support.helpfulness_judge(question STRING, candidate_answer STRING, aisystem_id STRING)
    RETURNS STRING
    RETURN embed(text=concat(question, candidate_answer), model='bge');

    CREATE BATCH TABLE out
    SELECT customer_support.helpfulness_judge(question=q, candidate_answer=a, aisystem_id=id) AS helpfulness
    FROM prepared;
    """

    entrypoint = EngineEntrypoint(
        script,
        registered_functions=[
            {
                "name": "embed",
                "type": "py",
                "call_name": "embed",
                "runtime_alias": "embed",
                "signature": {
                    "parameters": [
                        {"name": "text", "type_sql": "STRING", "has_default": False},
                        {"name": "model", "type_sql": "STRING", "has_default": True},
                    ]
                },
            }
        ],
    )

    graph = entrypoint.dependency_graph()

    assert "table:out" in graph.edges
    assert "function:customer_support.helpfulness_judge" in graph.edges["table:out"]
    assert "table:prepared" in graph.edges["table:out"]
    assert "function:embed" in graph.edges["function:customer_support.helpfulness_judge"]


def test_dependency_graph_tracks_load_retrieve_and_publish_edges():
    script = """
    LOAD raw FROM 's3://bucket/raw';
    RETRIEVE ANNOTATION RESULTS labeled FROM ANNOTATION REQUEST 'task-123';
    CREATE BATCH TABLE joined
    SELECT 'quality' AS metric, CAST(q AS DOUBLE) AS value FROM raw;
    SAVE joined TO 's3://bucket/out';
    PUBLISH joined TO REPORTS WITH (COMPONENT = METRIC);
    """

    graph = EngineEntrypoint(script).dependency_graph()

    assert "table:joined" in graph.edges
    assert "table:raw" in graph.edges["table:joined"]
    assert "publish:annotation:task-123" in graph.edges["table:labeled"]
    assert "table:joined" in graph.edges["save:joined->s3://bucket/out"]
    assert "table:joined" in graph.edges["publish:reports:metric:joined"]


def test_dependency_graph_types_dataset_input_load_edges():
    script = """
    DECLARE INPUT source DATASET DEFAULT 'dataset.path';
    LOAD raw FROM source;
    CREATE BATCH TABLE out SELECT * FROM raw;
    """

    graph = EngineEntrypoint(script).dependency_graph()

    assert graph.node_kinds["input:source"] == "input:dataset"
    assert graph.node_kinds["table:raw"] == "table:load"
    assert graph.edge_kinds["table:raw"]["input:source"] == "loads_dataset_input"
    assert graph.edge_kinds["table:out"]["table:raw"] == "reads_table"
