from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.statements import StreamTableStmt


class RecordingBackend:
    def __init__(self) -> None:
        self.calls = []

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


def test_stream_table_parses_and_compiles():
    script = """
    LOAD prepared FROM 's3://bucket/prepared'
    WITH FORMAT='parquet';

    CREATE STREAM TABLE live_scores
    OPTIONS (BATCH_SIZE=25)
    SELECT score
    FROM prepared;
    """

    statements = EngineEntrypoint(script).resolve()
    assert any(isinstance(statement, StreamTableStmt) for statement in statements)

    plan = EngineEntrypoint(script).compile_plan(include_cells=True)
    assert [step.kind for step in plan] == ["load_table", "create_stream_table"]
    assert plan[1].payload["sql"] == "SELECT score AS score FROM prepared"
    assert plan[1].payload["batch_size"] == 25
    assert plan[1].payload["source_tables"] == ["prepared"]


def test_stream_table_executes_through_backend_hook():
    script = """
    LOAD prepared FROM 's3://bucket/prepared'
    WITH FORMAT='parquet';

    CREATE STREAM TABLE live_scores
    OPTIONS (BATCH_SIZE=10)
    SELECT score
    FROM prepared;
    """

    backend = RecordingBackend()
    EngineEntrypoint(script).execute(backend, include_cells=True)

    assert backend.calls[1][0] == "create_stream_table"
    assert backend.calls[1][2] == "SELECT score AS score FROM prepared"
    assert backend.calls[1][3] == ["prepared"]
    assert backend.calls[1][4] == 10
