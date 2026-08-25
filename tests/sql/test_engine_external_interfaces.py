from __future__ import annotations

import json
import socketserver
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from agentcicd.sql.engine.annotation_store import HttpAnnotationStore
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.publication_store import HttpPublicationStore
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def _start_file_server(root: Path) -> tuple[_ReusableTCPServer, threading.Thread, str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = _ReusableTCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _start_json_server(response_payload: dict) -> tuple[_ReusableTCPServer, threading.Thread, str]:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            request_payload = json.loads(raw or "{}")
            args = request_payload.get("args") or {}
            text = str(args.get("text") or "")
            model = str(args.get("model") or "")
            payload = dict(response_payload)
            payload["result"] = f"remote:{text}:{model}"
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = _ReusableTCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _start_invalid_json_server(
    *,
    method: str = "POST",
    status_code: int = 200,
    body: str = "not-json",
) -> tuple[_ReusableTCPServer, threading.Thread, str]:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_GET(self):  # noqa: N802
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format, *args):  # noqa: A003
            return

    server = _ReusableTCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _start_service_server() -> tuple[_ReusableTCPServer, threading.Thread, str, list[dict]]:
    requests: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            payload = json.loads(raw or "{}")
            requests.append({"path": self.path, "method": "POST", "payload": payload})
            response_payload = {"request_id": "task-http"} if self.path == "/publish/annotation" else {}
            encoded = json.dumps(response_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):  # noqa: N802
            requests.append({"path": self.path, "method": "GET"})
            if self.path == "/annotations/requests/task-http/results":
                payload = json.dumps(
                    {
                        "rows": [
                            {"id": 1, "label": "approved"},
                            {"id": 2, "label": "review"},
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(404)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = _ReusableTCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}", requests


@pytest.fixture
def local_spark():
    pyspark = pytest.importorskip("pyspark.sql")
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-engine-external-interfaces")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


def test_new_engine_http_loads_json_and_jsonl(local_spark, tmp_path: Path):
    http_root = tmp_path / "http_data"
    http_root.mkdir(parents=True, exist_ok=True)
    (http_root / "array.json").write_text(
        json.dumps([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]),
        encoding="utf-8",
    )
    (http_root / "rows.jsonl").write_text(
        '{"id": 10, "name": "x"}\n{"id": 11, "name": "y"}\n',
        encoding="utf-8",
    )

    server, thread, base_url = _start_file_server(http_root)
    try:
        script = f"""
        LOAD remote_json FROM '{base_url}/array.json';
        LOAD remote_jsonl FROM '{base_url}/rows.jsonl'
        WITH FORMAT='jsonl';
        """

        backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
        EngineEntrypoint(script).execute(backend, include_cells=False)

        json_ids = sorted(_row_value(row.id) for row in local_spark.table("remote_json").select("id").collect())
        jsonl_ids = sorted(_row_value(row.id) for row in local_spark.table("remote_jsonl").select("id").collect())
        assert json_ids == [1, 2]
        assert jsonl_ids == [10, 11]
        assert any(path.name.endswith("array.json") for path in (tmp_path / "http_cache").iterdir())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _row_value(value):
    return getattr(value, "value", value)


def test_new_engine_remote_runtime_function_calls_http_service(local_spark, tmp_path: Path):
    raw_path = tmp_path / "raw_input"
    local_spark.createDataFrame([("alice",), ("bob",)], ["name"]).write.mode("overwrite").parquet(str(raw_path))

    server, thread, base_url = _start_json_server({"result": ""})
    try:
        script = f"""
        LOAD raw FROM '{raw_path.as_posix()}'
        WITH FORMAT='parquet';

        CREATE BATCH TABLE out
        SELECT embed(text=name) AS embedding
        FROM raw
        ORDER BY name;
        """

        backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
        EngineEntrypoint(
            script,
            registered_functions=[
                {
                    "name": "embed",
                    "type": "py",
                    "call_name": "embed",
                    "runtime_alias": "embed",
                    "base_url": base_url,
                    "invoke_path": "/invoke",
                    "signature": {
                        "parameters": [
                            {"name": "text", "type_sql": "STRING", "has_default": False},
                            {"name": "model", "type_sql": "STRING", "has_default": True, "default_value": "bge"},
                        ]
                    },
                }
            ],
        ).execute(backend, include_cells=True)

        rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
        assert [row["embedding"]["value"] for row in rows] == [
            "remote:alice:bge",
            "remote:bob:bge",
        ]
        for row in rows:
            embedding = row["embedding"]
            assert embedding["__agentcicd_cell"] is True
            assert embedding["metadata"]["errors"] == []
            assert isinstance(embedding["metadata"]["latency_ms"], int)
            assert "fixture_trace" in embedding["metadata"].asDict()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_new_engine_publish_reports_writes_run_level_metrics_json(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE score_rows
    SELECT 'helpfulness' AS metric, 0.7 AS value;

    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    metrics_path = tmp_path / "reports" / "metrics.json"
    assert metrics_path.exists()
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["tags"] == {}
    assert payload[0]["metric"]["value"] == "helpfulness"
    assert payload[0]["value"]["value"] == 0.7


def test_new_engine_publish_reports_unwraps_fallback_tag_columns(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE score_rows
    SELECT 'helpfulness' AS metric, 0.7 AS value, 'v12' AS agent_version;

    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    payload = json.loads((tmp_path / "reports" / "metrics.json").read_text(encoding="utf-8"))
    assert payload[0]["tags"] == {"agent_version": "v12"}


def test_new_engine_publish_reports_routes_errored_metric_rows_to_issues(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE source
    SELECT CAST('not-a-number' AS INT) AS bad_value;

    CREATE BATCH TABLE score_rows
    SELECT 'bad_metric' AS metric, bad_value AS value
    FROM source;

    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    metrics_payload = json.loads((tmp_path / "reports" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_payload == []

    issues_payload = json.loads((tmp_path / "reports" / "issues.json").read_text(encoding="utf-8"))
    assert len(issues_payload) == 1
    assert issues_payload[0]["title"] == "Metric row not published"
    assert issues_payload[0]["severity"] == "medium"
    assert issues_payload[0]["row"]["metric"] == "bad_metric"
    assert issues_payload[0]["error"]["code"] == "AGENTCICD_CAST_ERROR"


def test_new_engine_publish_reports_reads_heterogeneous_parquet_parts(tmp_path: Path):
    pyarrow = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    from agentcicd.sql.engine.interfaces import BackendLayout
    from agentcicd.sql.engine.publication_store import LocalManifestPublicationStore

    table_dir = tmp_path / "tables" / "scored"
    table_dir.mkdir(parents=True)
    (tmp_path / "publish").mkdir()
    first = pyarrow.table(
        {
            "case_id": [{"value": "case-1", "metadata": {"error": None}}],
            "required_policy_steps": [
                {"value": {"ask_for_order_id": {"value": True, "typed_value": "bool"}}}
            ],
        }
    )
    second = pyarrow.table(
        {
            "case_id": [{"value": "case-2", "metadata": {"error": None}}],
            "required_policy_steps": [
                {"value": {"ask_for_item_condition_if_not_clear": {"value": True, "typed_value": "bool"}}}
            ],
        }
    )
    pq.write_table(first, table_dir / "part-00000.parquet")
    pq.write_table(second, table_dir / "part-00001.parquet")

    layout = BackendLayout(
        working_dir=str(tmp_path),
        tables_root=str(tmp_path / "tables"),
        sources_root=str(tmp_path / "sources"),
        outputs_root=str(tmp_path / "outputs"),
        publish_root=str(tmp_path / "publish"),
        checkpoints_root=str(tmp_path / "checkpoints"),
        stream_batches_root=str(tmp_path / "stream_batches"),
        http_cache_root=str(tmp_path / "http_cache"),
        annotation_tasks_root=str(tmp_path / "annotation_tasks"),
    )

    LocalManifestPublicationStore().publish_report(layout, "scored", "issue")

    payload = json.loads((tmp_path / "reports" / "issues.json").read_text(encoding="utf-8"))
    assert [row["case_id"] for row in payload] == ["case-1", "case-2"]
    assert "ask_for_order_id" in payload[0]["required_policy_steps"]
    assert "ask_for_item_condition_if_not_clear" in payload[1]["required_policy_steps"]


def test_new_engine_publish_reports_appends_multiple_publish_steps(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE summary_a
    SELECT 'helpfulness' AS metric, 0.7 AS value;

    PUBLISH summary_a TO REPORTS WITH (COMPONENT = METRIC);

    CREATE BATCH TABLE summary_b
    SELECT 'coherence' AS metric, 1.0 AS value;

    PUBLISH summary_b TO REPORTS WITH (COMPONENT = METRIC);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    metrics_path = tmp_path / "reports" / "metrics.json"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert [item["metric"]["value"] for item in payload] == ["helpfulness", "coherence"]
    assert [item["value"]["value"] for item in payload] == [0.7, 1.0]

    summary_a_manifest = tmp_path / "published" / "reports_metric_summary_a.json"
    summary_b_manifest = tmp_path / "published" / "reports_metric_summary_b.json"
    assert summary_a_manifest.exists()
    assert summary_b_manifest.exists()
    assert len(json.loads(summary_a_manifest.read_text(encoding="utf-8"))["rows"]) == 1
    assert len(json.loads(summary_b_manifest.read_text(encoding="utf-8"))["rows"]) == 1


def test_new_engine_publish_reports_writes_chart_definition(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE latency
    SELECT 'provider-a' AS provider, 820.0 AS p95_latency_ms;

    PUBLISH latency TO REPORTS WITH (
      COMPONENT = CHART,
      CHART_TYPE = BAR,
      TITLE = 'Latency by provider',
      X_AXIS = provider,
      Y_AXIS = p95_latency_ms,
      X_AXIS_LABEL = 'Provider',
      Y_AXIS_LABEL = 'P95 latency (ms)',
      AGGREGATION = AVG,
      LIMIT = 20
    );
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    charts_path = tmp_path / "reports" / "charts.json"
    payload = json.loads(charts_path.read_text(encoding="utf-8"))
    assert len(payload) == 1
    chart = payload[0]
    assert chart["title"] == "Latency by provider"
    assert chart["chart_type"] == "bar"
    assert chart["x_axis"] == "provider"
    assert chart["y_axis"] == "p95_latency_ms"
    assert chart["x_axis_label"] == "Provider"
    assert chart["y_axis_label"] == "P95 latency (ms)"
    assert chart["aggregation"] == "avg"
    assert chart["limit"] == 20
    assert chart["data"][0]["provider"] == "provider-a"


def test_new_engine_publish_reports_preserves_chart_rows_with_value_column(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE accuracy_chart_rows
    SELECT 'Relevance' AS task, 'Global large' AS workflow, 1.0 AS value;

    PUBLISH accuracy_chart_rows TO REPORTS WITH (
      COMPONENT = CHART,
      CHART_TYPE = BAR,
      TITLE = 'Accuracy by task',
      X_AXIS = task,
      Y_AXIS = value,
      GROUP_BY = workflow
    );
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    charts_path = tmp_path / "reports" / "charts.json"
    payload = json.loads(charts_path.read_text(encoding="utf-8"))
    assert payload[0]["data"] == [{"task": "Relevance", "workflow": "Global large", "value": 1.0}]


def test_new_engine_publish_reports_rejects_invalid_issue_severity(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE issues
    SELECT 'Missing citation' AS title, 'urgent' AS severity;

    PUBLISH issues TO REPORTS WITH (COMPONENT = ISSUE);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    with pytest.raises(ValueError, match="Issue severity"):
        EngineEntrypoint(script).execute(backend, include_cells=True)


def test_new_engine_publish_reports_accepts_wrapped_issue_severity(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE issues
    SELECT 'Missing citation' AS title, 'high' AS severity;

    PUBLISH issues TO REPORTS WITH (COMPONENT = ISSUE);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    issues_path = tmp_path / "reports" / "issues.json"
    payload = json.loads(issues_path.read_text(encoding="utf-8"))
    assert payload[0]["severity"] == "high"


def test_new_engine_publish_reports_unwraps_errored_issue_cells_as_error(local_spark, tmp_path: Path):
    script = """
    CREATE BATCH TABLE source
    SELECT CAST('not-a-number' AS INT) AS bad_value;

    CREATE BATCH TABLE issues
    SELECT bad_value AS issue
    FROM source;

    PUBLISH issues TO REPORTS WITH (COMPONENT = ISSUE);
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    issues_path = tmp_path / "reports" / "issues.json"
    payload = json.loads(issues_path.read_text(encoding="utf-8"))
    assert payload[0]["issue"]["code"] == "AGENTCICD_CAST_ERROR"
    assert payload[0]["issue"]["message"] == "Could not cast value to INT"


def test_new_engine_http_publication_and_annotation_interfaces(local_spark, tmp_path: Path):
    raw_path = tmp_path / "service_raw"
    local_spark.createDataFrame([(1, "sf"), (2, "la")], ["id", "city"]).write.mode("overwrite").parquet(str(raw_path))

    server, thread, base_url, requests = _start_service_server()
    try:
        script = f"""
        LOAD raw FROM '{raw_path.as_posix()}'
        WITH FORMAT='parquet';

        CREATE BATCH TABLE scored
        SELECT id, city
        FROM raw;

        CREATE BATCH TABLE score_rows
        SELECT 'city_rows' AS metric, CAST(COUNT(*) AS DOUBLE) AS value
        FROM scored;

        PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
        PUBLISH scored TO DATASET 'customer-support-http';
        PUBLISH scored TO ANNOTATION QUEUE 'http review' AS http_review;

        RETRIEVE ANNOTATION RESULTS labeled FROM http_review;

        CREATE BATCH TABLE out
        SELECT scored.id, labeled.label
        FROM scored
        JOIN labeled ON scored.id = labeled.id
        ORDER BY scored.id;
        """

        backend = SparkExecutionBackend(
            local_spark,
            working_dir=str(tmp_path),
            publication_store=HttpPublicationStore(base_url=base_url),
            annotation_store=HttpAnnotationStore(base_url=base_url),
        )
        EngineEntrypoint(script).execute(backend, include_cells=True)

        rows = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()
        assert [(row["id"]["value"], row["label"]["value"]) for row in rows] == [
            (1, "approved"),
            (2, "review"),
        ]

        assert [request["path"] for request in requests] == [
            "/publish/reports",
            "/publish/dataset",
            "/publish/annotation",
            "/annotations/requests/task-http/results",
        ]
        assert requests[0]["payload"]["component"] == "metric"
        assert requests[1]["payload"]["dataset_name"] == "customer-support-http"
        assert requests[2]["payload"]["queue_name"] == "http review"
        assert requests[2]["payload"]["alias"] == "http_review"
        assert [row["id"]["value"] for row in requests[2]["payload"]["rows"]] == [1, 2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_wrapped_aggregate_count_distinct_with_literal_projection(local_spark, tmp_path: Path):
    raw_path = tmp_path / "distinct_raw"
    local_spark.createDataFrame(
        [(1, "first"), (1, "duplicate"), (2, "second")],
        ["sample_id", "label"],
    ).write.mode("overwrite").parquet(str(raw_path))

    script = f"""
    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE scored
    SELECT sample_id, label
    FROM raw;

    CREATE BATCH TABLE summary
    SELECT
      'Question rows' AS summary_metric,
      CAST(COUNT(DISTINCT sample_id) AS DOUBLE) AS value
    FROM scored;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    row = local_spark.read.parquet(str(tmp_path / "tables" / "summary")).first()
    assert row["summary_metric"]["value"] == "Question rows"
    assert row["value"]["value"] == 2.0


def test_wrapped_aggregate_group_key_metadata_is_group_safe(local_spark, tmp_path: Path):
    raw_path = tmp_path / "metric_raw"
    local_spark.createDataFrame(
        [
            ("global_large", "support", 1.0),
            ("global_large", "support", 0.0),
            ("local_small", "support", 1.0),
        ],
        ["workflow", "task", "score"],
    ).write.mode("overwrite").parquet(str(raw_path))

    script = f"""
    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE scored
    SELECT workflow, task, score
    FROM raw;

    CREATE BATCH TABLE summary
    SELECT
      named_struct(
        'metric', 'judge_macro_f1',
        'metric_value', avg(score),
        'tags', map('workflow', workflow, 'task', task)
      ) AS metric_row,
      'judge_macro_f1' AS metric,
      avg(score) AS value,
      map('workflow', workflow, 'task', task) AS tags
    FROM scored
    GROUP BY workflow, task;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "summary")).collect()
    assert sorted(
        (row["metric"]["value"], row["tags"]["value"]["workflow"], row["tags"]["value"]["task"], row["value"]["value"])
        for row in rows
    ) == [
        ("judge_macro_f1", "global_large", "support", 0.5),
        ("judge_macro_f1", "local_small", "support", 1.0),
    ]


def test_wrapped_materialized_table_frontier_aggregates_read_cell_values(local_spark, tmp_path: Path):
    raw_path = tmp_path / "maze_frontier_raw"
    local_spark.createDataFrame(
        [
            ("case-1", 5, "hard", "loop", "full", "condition", 0.4, 3.0, 2.0, 7.0, 1.0, True, True, 0.8, 0.1, 0.2),
            ("case-2", 5, "hard", "loop", "full", "condition", 0.6, 4.0, 4.0, 8.0, 2.0, False, True, 0.5, 0.2, 0.4),
        ],
        [
            "case_id",
            "grid_size",
            "difficulty",
            "complexity_cell",
            "feedback_condition",
            "condition_cell",
            "braid_factor",
            "difficulty_score",
            "cycle_count",
            "loop_decoy_count",
            "path_branch_count",
            "success",
            "meets_difficulty",
            "path_efficiency",
            "invalid_move_rate",
            "revisit_rate",
        ],
    ).write.mode("overwrite").parquet(str(raw_path))

    script = f"""
    DECLARE INPUT braid_factor DOUBLE DEFAULT -1;

    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE scored_episodes
    SELECT *
    FROM raw;

    CREATE BATCH TABLE maze_navigation_frontier
    SELECT
      grid_size,
      difficulty,
      complexity_cell,
      feedback_condition,
      condition_cell,
      count(*) AS case_count,
      avg(difficulty_score) AS avg_difficulty_score,
      avg(braid_factor) AS avg_braid_factor,
      avg(cycle_count) AS avg_cycle_count,
      avg(loop_decoy_count) AS avg_loop_decoy_count,
      avg(path_branch_count) AS avg_path_branch_count,
      avg(CASE WHEN meets_difficulty THEN 1.0 ELSE 0.0 END) AS meets_difficulty_rate,
      avg(CASE WHEN success THEN 1.0 ELSE 0.0 END) AS success_rate,
      avg(path_efficiency) AS avg_path_efficiency,
      avg(invalid_move_rate) AS avg_invalid_move_rate,
      avg(revisit_rate) AS avg_revisit_rate
    FROM scored_episodes
    GROUP BY grid_size, difficulty, complexity_cell, feedback_condition, condition_cell;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    row = local_spark.read.parquet(str(tmp_path / "tables" / "maze_navigation_frontier")).first()
    assert row["case_count"]["value"] == 2
    assert row["avg_braid_factor"]["value"] == 0.5
    assert row["avg_difficulty_score"]["value"] == 3.5
    assert row["avg_cycle_count"]["value"] == 3.0
    assert row["success_rate"]["value"] == 0.5
    assert row["meets_difficulty_rate"]["value"] == 1.0


def test_wrapped_frontier_aggregates_flatten_nested_materialized_cells(local_spark, tmp_path: Path):
    from pyspark.sql import Row
    from pyspark.sql import types as T

    error_type = T.StructType(
        [
            T.StructField("code", T.StringType(), True),
            T.StructField("message", T.StringType(), True),
            T.StructField("source", T.StringType(), True),
            T.StructField("path", T.StringType(), True),
            T.StructField("recoverable", T.BooleanType(), True),
            T.StructField("cause_code", T.StringType(), True),
            T.StructField("cause_message", T.StringType(), True),
            T.StructField("details", T.MapType(T.StringType(), T.StringType()), True),
        ]
    )
    metadata_type = T.StructType(
        [
            T.StructField("errors", T.ArrayType(error_type), True),
            T.StructField("latency_ms", T.LongType(), True),
        ]
    )
    inner_cell_type = T.StructType(
        [
            T.StructField("cell_id", T.StringType(), True),
            T.StructField("value", T.DoubleType(), True),
            T.StructField("metadata", metadata_type, True),
            T.StructField("__agentcicd_cell", T.BooleanType(), True),
        ]
    )
    nested_cell_type = T.StructType(
        [
            T.StructField("cell_id", T.StringType(), True),
            T.StructField("value", inner_cell_type, True),
            T.StructField("metadata", metadata_type, True),
            T.StructField("__agentcicd_cell", T.BooleanType(), True),
        ]
    )
    schema = T.StructType(
        [
            T.StructField("case_id", T.StringType(), True),
            T.StructField("grid_size", T.IntegerType(), True),
            T.StructField("difficulty", T.StringType(), True),
            T.StructField("complexity_cell", T.StringType(), True),
            T.StructField("feedback_condition", T.StringType(), True),
            T.StructField("condition_cell", T.StringType(), True),
            T.StructField("braid_factor", nested_cell_type, True),
            T.StructField("difficulty_score", T.DoubleType(), True),
            T.StructField("success", T.BooleanType(), True),
        ]
    )

    def cell(value: float, cell_id: str) -> Row:
        return Row(
            cell_id=f"outer-{cell_id}",
            value=Row(
                cell_id=f"inner-{cell_id}",
                value=value,
                metadata=Row(errors=[], latency_ms=None),
                __agentcicd_cell=True,
            ),
            metadata=Row(errors=[], latency_ms=None),
            __agentcicd_cell=True,
        )

    raw_path = tmp_path / "nested_frontier_raw"
    local_spark.createDataFrame(
        [
            Row("case-1", 5, "hard", "5x5", "local_only", "5x5 / local_only", cell(0.4, "1"), 3.0, True),
            Row("case-2", 5, "hard", "5x5", "local_only", "5x5 / local_only", cell(0.6, "2"), 5.0, False),
        ],
        schema,
    ).write.mode("overwrite").parquet(str(raw_path))

    script = f"""
    DECLARE INPUT braid_factor DOUBLE DEFAULT -1;

    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE scored_episodes
    SELECT *
    FROM raw;

    CREATE BATCH TABLE maze_navigation_frontier
    SELECT
      grid_size,
      difficulty,
      complexity_cell,
      feedback_condition,
      condition_cell,
      count(*) AS case_count,
      avg(difficulty_score) AS avg_difficulty_score,
      avg(braid_factor) AS avg_braid_factor,
      avg(CASE WHEN success THEN 1.0 ELSE 0.0 END) AS success_rate
    FROM scored_episodes
    GROUP BY grid_size, difficulty, complexity_cell, feedback_condition, condition_cell;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    row = local_spark.read.parquet(str(tmp_path / "tables" / "maze_navigation_frontier")).first()
    assert row["avg_braid_factor"]["value"] == 0.5
    assert row["avg_difficulty_score"]["value"] == 4.0
    assert row["success_rate"]["value"] == 0.5


def test_wrapped_distinct_union_operates_on_cell_values(local_spark, tmp_path: Path):
    raw_path = tmp_path / "label_rows"
    local_spark.createDataFrame(
        [
            ("global_large", "relevance", "relevant", "relevant"),
            ("global_large", "relevance", "not_relevant", "relevant"),
            ("local_small", "citation", "should_cite", "should_not_cite"),
        ],
        ["workflow", "task", "gold_label", "predicted_label"],
    ).write.mode("overwrite").parquet(str(raw_path))

    script = f"""
    LOAD raw FROM '{raw_path.as_posix()}'
    WITH FORMAT='parquet';

    CREATE BATCH TABLE judge_label_results
    SELECT workflow, task, gold_label, predicted_label
    FROM raw;

    CREATE BATCH TABLE label_counts
    SELECT
      labels.workflow,
      labels.task,
      labels.label,
      CAST(count(*) AS DOUBLE) AS label_count
    FROM (
      SELECT DISTINCT
        workflow,
        task,
        gold_label AS label
      FROM judge_label_results
      WHERE gold_label IS NOT NULL
      UNION
      SELECT DISTINCT
        workflow,
        task,
        predicted_label AS label
      FROM judge_label_results
      WHERE predicted_label IS NOT NULL
    ) labels
    GROUP BY labels.workflow, labels.task, labels.label;
    """

    backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
    EngineEntrypoint(script).execute(backend, include_cells=True)

    rows = local_spark.read.parquet(str(tmp_path / "tables" / "label_counts")).collect()
    assert sorted(
        (row["workflow"]["value"], row["task"]["value"], row["label"]["value"], row["label_count"]["value"])
        for row in rows
    ) == [
        ("global_large", "relevance", "not_relevant", 1.0),
        ("global_large", "relevance", "relevant", 1.0),
        ("local_small", "citation", "should_cite", 1.0),
        ("local_small", "citation", "should_not_cite", 1.0),
    ]
    label_cell = rows[0]["label"]
    assert label_cell["metadata"]["errors"] == []
    assert "lineage" not in label_cell["metadata"].asDict()


def test_remote_runtime_function_surfaces_invalid_json_response(local_spark, tmp_path: Path):
    raw_path = tmp_path / "invalid_runtime_raw"
    local_spark.createDataFrame([("alice",)], ["name"]).write.mode("overwrite").parquet(str(raw_path))

    server, thread, base_url = _start_invalid_json_server(body="{invalid")
    try:
        script = f"""
        LOAD raw FROM '{raw_path.as_posix()}'
        WITH FORMAT='parquet';

        CREATE BATCH TABLE out
        SELECT embed(text=name) AS embedding
        FROM raw;
        """

        backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
        EngineEntrypoint(
            script,
            registered_functions=[
                {
                    "name": "embed",
                    "type": "py",
                    "call_name": "embed",
                    "runtime_alias": "embed",
                    "base_url": base_url,
                    "invoke_path": "/invoke",
                    "signature": {
                        "parameters": [
                            {"name": "text", "type_sql": "STRING", "has_default": False},
                        ]
                    },
                }
            ],
        ).execute(backend, include_cells=True)

        row = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()[0]
        error = row["embedding"]["metadata"]["errors"][0]
        assert row["embedding"]["value"] is None
        assert error["code"] == "AGENTCICD_RUNTIME_REMOTE_ERROR"
        assert "invalid JSON" in error["message"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_remote_runtime_function_surfaces_http_error_body(local_spark, tmp_path: Path):
    raw_path = tmp_path / "http_error_runtime_raw"
    local_spark.createDataFrame([("alice",)], ["name"]).write.mode("overwrite").parquet(str(raw_path))

    server, thread, base_url = _start_invalid_json_server(
        status_code=400,
        body='{"error":"missing simulator.user callback"}',
    )
    try:
        script = f"""
        LOAD raw FROM '{raw_path.as_posix()}'
        WITH FORMAT='parquet';

        CREATE BATCH TABLE out
        SELECT embed(text=name) AS embedding
        FROM raw;
        """

        backend = SparkExecutionBackend(local_spark, working_dir=str(tmp_path))
        EngineEntrypoint(
            script,
            registered_functions=[
                {
                    "name": "embed",
                    "type": "py",
                    "call_name": "embed",
                    "runtime_alias": "embed",
                    "base_url": base_url,
                    "invoke_path": "/invoke",
                    "signature": {
                        "parameters": [
                            {"name": "text", "type_sql": "STRING", "has_default": False},
                        ]
                    },
                }
            ],
        ).execute(backend, include_cells=True)

        row = local_spark.read.parquet(str(tmp_path / "tables" / "out")).collect()[0]
        error = row["embedding"]["metadata"]["errors"][0]
        assert row["embedding"]["value"] is None
        assert error["code"] == "AGENTCICD_RUNTIME_HTTP_ERROR"
        assert "HTTP 400" in error["message"]
        assert "missing simulator.user callback" in error["message"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_publication_store_surfaces_service_failure(local_spark, tmp_path: Path):
    raw_path = tmp_path / "publish_failure_raw"
    local_spark.createDataFrame([(1,)], ["id"]).write.mode("overwrite").parquet(str(raw_path))

    server, thread, base_url = _start_invalid_json_server(status_code=500, body='{"error":"boom"}')
    try:
        script = f"""
        LOAD raw FROM '{raw_path.as_posix()}'
        WITH FORMAT='parquet';

        CREATE BATCH TABLE scored
        SELECT id FROM raw;

        PUBLISH scored TO DATASET 'broken-http';
        """

        backend = SparkExecutionBackend(
            local_spark,
            working_dir=str(tmp_path),
            publication_store=HttpPublicationStore(base_url=base_url),
        )
        with pytest.raises(RuntimeError, match="Publication request"):
            EngineEntrypoint(script).execute(backend, include_cells=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_annotation_store_surfaces_invalid_payload(local_spark, tmp_path: Path):
    server, thread, base_url = _start_invalid_json_server(method="GET", body="{invalid")
    try:
        backend = SparkExecutionBackend(
            local_spark,
            working_dir=str(tmp_path),
            annotation_store=HttpAnnotationStore(base_url=base_url),
        )
        with pytest.raises(ValueError, match="invalid JSON|Annotation retrieval"):
            backend.retrieve_annotation("labeled", "task-http", wrap_cells=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
