from agentcicd.sql.engine.annotation_store import AnnotationResultsPending
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.runtime import execute_plan_dag
from agentcicd.sql.engine.plan import ExecutionPlanStep, SqlStepPayload
import threading


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def register_sql_function(self, name, definition) -> None:
        self.calls.append(("register_sql_function", name, definition.canonical_name))

    def register_runtime_function(self, name, definition) -> None:
        self.calls.append(("register_runtime_function", name, definition.runtime_alias))

    def create_batch_table(self, name, sql, *, options=None) -> None:
        self.calls.append(("create_batch_table", name, sql))

    def create_stream_table(self, name, sql, *, source_tables=None, batch_size=None, options=None) -> None:
        self.calls.append(("create_stream_table", name, sql, source_tables or [], batch_size))

    def load_table(self, name, path, options, *, wrap_cells=False, limit=None) -> None:
        self.calls.append(("load_table", name, path, options, wrap_cells, limit))

    def save_table(self, name, path, options) -> None:
        self.calls.append(("save_table", name, path, options))

    def publish_report(self, name, component, chart_type=None) -> None:
        self.calls.append(("publish_report", name, component, chart_type))

    def publish_dataset(self, name, dataset_name) -> None:
        self.calls.append(("publish_dataset", name, dataset_name))

    def publish_annotation(self, name, queue_name, *, alias=None, options=None) -> None:
        self.calls.append(("publish_annotation", name, queue_name, alias, options))

    def retrieve_annotation(self, name, source_ref, *, wrap_cells=False) -> None:
        self.calls.append(("retrieve_annotation", name, source_ref, wrap_cells))


class FailingBackend(RecordingBackend):
    def create_batch_table(self, name, sql, *, options=None) -> None:
        super().create_batch_table(name, sql, options=options)
        raise RuntimeError("boom")


class PendingAnnotationBackend(RecordingBackend):
    def retrieve_annotation(self, name, source_ref, *, wrap_cells=False) -> None:
        super().retrieve_annotation(name, source_ref, wrap_cells=wrap_cells)
        raise AnnotationResultsPending("annreq.ready_later", status_code=404)


def test_engine_execute_runs_dependency_ordered_plan():
    script = """
    CREATE FUNCTION customer_support.helpfulness_judge(question STRING, candidate_answer STRING, aisystem_id STRING)
    RETURNS STRING
    RETURN concat(question, candidate_answer);

    LOAD raw FROM 's3://bucket/raw' WITH FORMAT='csv';

    CREATE BATCH TABLE out
    SELECT customer_support.helpfulness_judge(question=q, candidate_answer=a, aisystem_id=id) AS helpfulness
    FROM raw;

    SAVE out TO 's3://bucket/out' WITH FORMAT='delta';
    PUBLISH out TO DATASET 'customer-support';
    """

    backend = RecordingBackend()
    report = EngineEntrypoint(script).execute(backend, include_cells=True)

    assert [call[0] for call in backend.calls] == [
        "register_sql_function",
        "load_table",
        "create_batch_table",
        "save_table",
        "publish_dataset",
    ]
    assert backend.calls[1] == ("load_table", "raw", "s3://bucket/raw", {"format": "csv"}, True, None)
    assert "NAMED_STRUCT" in backend.calls[2][2]
    assert [event.status for event in report.events] == [
        "started",
        "completed",
        "started",
        "completed",
        "started",
        "completed",
        "started",
        "completed",
        "started",
        "completed",
    ]


def test_engine_execute_handles_annotation_and_publish_steps():
    script = """
    RETRIEVE ANNOTATION RESULTS labeled FROM ANNOTATION REQUEST 'task-123';
    CREATE BATCH TABLE score_rows
    SELECT 'annotation_rows' AS metric, CAST(COUNT(*) AS DOUBLE) AS value
    FROM labeled;
    PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
    """

    backend = RecordingBackend()
    EngineEntrypoint(script).execute(backend)

    assert backend.calls == [
        ("retrieve_annotation", "labeled", "task-123", False),
        ("create_batch_table", "score_rows", backend.calls[1][2]),
        ("publish_report", "score_rows", "metric", None),
    ]


def test_engine_execute_waits_when_annotation_results_are_pending():
    script = """
    RETRIEVE ANNOTATION RESULTS labeled FROM annotation_review;
    CREATE BATCH TABLE after_retrieve
    SELECT COUNT(*) AS row_count
    FROM labeled;
    """
    events = []

    backend = PendingAnnotationBackend()
    report = EngineEntrypoint(script).execute(backend, progress_callback=events.append)

    assert backend.calls == [("retrieve_annotation", "labeled", "annotation_review", False)]
    assert report.failed_step_kind is None
    assert report.events[-1].status == "waiting"
    assert report.events[-1].payload == {
        "action": "wait_for_annotation",
        "annotation_request_id": "annreq.ready_later",
        "source_ref": "annotation_review",
        "target_table": "labeled",
    }
    assert events[-1].status == "waiting"
    assert events[-1].metadata["annotation_request_id"] == "annreq.ready_later"


def test_engine_execute_registers_runtime_functions_before_table_steps():
    script = """
    LOAD raw FROM 's3://bucket/raw' WITH FORMAT='csv';

    CREATE BATCH TABLE out
    SELECT embed(text=q, model='bge') AS embedding
    FROM raw;
    """

    backend = RecordingBackend()
    EngineEntrypoint(
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
    ).execute(backend, include_cells=True)

    assert [call[0] for call in backend.calls[:3]] == [
        "register_runtime_function",
        "load_table",
        "create_batch_table",
    ]


def test_engine_execute_reports_failed_step():
    script = """
    LOAD prepared FROM 's3://bucket/prepared';

    CREATE BATCH TABLE out
    SELECT q FROM prepared;
    """

    backend = FailingBackend()
    try:
        EngineEntrypoint(script).execute(backend)
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("Expected RuntimeError")

    from agentcicd.sql.engine.runtime import execute_plan

    plan = EngineEntrypoint(script).compile_plan()
    report = execute_plan(plan, FailingBackend(), raise_on_error=False)

    assert report.failed_step_kind == "create_batch_table"
    assert report.failed_step_name == "out"
    assert report.error == "boom"
    assert report.events[-1].status == "failed"


def test_execute_plan_dag_runs_independent_tables_concurrently():
    started = []
    release = threading.Event()

    class BlockingBackend(RecordingBackend):
        def create_batch_table(self, name, sql, *, options=None) -> None:
            started.append(name)
            if name == "left":
                release.wait(timeout=2)
            self.calls.append(("create_batch_table", name, sql))

    plan = [
        ExecutionPlanStep(kind="create_batch_table", name="left", payload=SqlStepPayload(sql="SELECT 1")),
        ExecutionPlanStep(kind="create_batch_table", name="right", payload=SqlStepPayload(sql="SELECT 2")),
    ]
    backend = BlockingBackend()

    def run():
        execute_plan_dag(plan, backend, max_parallel_stages=2)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert release.wait(timeout=0.05) is False
        assert "left" in started
        for _ in range(20):
            if "right" in started:
                break
            threading.Event().wait(0.01)
        assert "right" in started
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        release.set()


def test_execute_plan_dag_blocks_dependents_after_failed_dependency():
    class SelectiveFailingBackend(RecordingBackend):
        def create_batch_table(self, name, sql, *, options=None) -> None:
            super().create_batch_table(name, sql, options=options)
            if name == "upstream":
                raise RuntimeError("upstream failed")

    plan = [
        ExecutionPlanStep(kind="create_batch_table", name="upstream", payload=SqlStepPayload(sql="SELECT 1")),
        ExecutionPlanStep(
            kind="create_batch_table",
            name="downstream",
            payload=SqlStepPayload(sql="SELECT * FROM upstream"),
            dependencies=("table:upstream",),
        ),
    ]

    report = execute_plan_dag(plan, SelectiveFailingBackend(), max_parallel_stages=2, raise_on_error=False)

    assert report.failed_step_name == "upstream"
    assert report.error == "upstream failed"
    blocked_events = [event for event in report.events if event.status == "blocked"]
    assert len(blocked_events) == 1
    assert blocked_events[0].step_name == "downstream"
    assert blocked_events[0].payload["blocked_by"] == "table:upstream"
