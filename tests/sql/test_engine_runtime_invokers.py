from __future__ import annotations

import json
import threading
import time
import zipfile
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

from pyspark.sql.types import ArrayType, DoubleType, StringType as SparkStringType
from pyspark.sql.types import StructField, StructType

from agentcicd.sql.runtime.udf_compat.function import BatchFunction
from agentcicd.sql.runtime.udf_compat.runtime_control import PoolLease
from agentcicd.sql.runtime.udf_compat.types import FType, StringType
from agentcicd.sql.runtime.udf_compat.udf import Param, Udf
from agentcicd.sql.engine import runtime_functions
from agentcicd.sql.runtime import package_distribution as runtime_package_distribution
from agentcicd.sql.runtime.invokers import http as runtime_http_invoker
from agentcicd.sql.runtime.invokers import local_fixture as runtime_local_fixture
from agentcicd.sql.runtime.spark_types import _coerce_remote_result
from agentcicd.sql.engine.runtime_functions import (
    CompositeRuntimeFunctionInvoker,
    HttpRuntimeFunctionInvoker,
    LocalFixtureRuntimeInvoker,
    SparkUdfRuntimeInvoker,
    SparkWorkerPackageDistributor,
    StubRuntimeFunctionInvoker,
)
from agentcicd.sql.ir.functions import FunctionParameterIR
from agentcicd.sql.udf_registry import clear_registered_udfs, register_udf


class _LocalOnlyUdf(Udf, name="mock.local"):
    _udf_name = "mock.local"

    def input_schema(self):
        return ()

    def input_args(self):
        return ()

    def signature(self):
        return ()

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return self._create_function

    def _create_function(self):
        class _Fn:
            def __call__(self, *args, **kwargs):
                return None

        return _Fn()


class _ControlledLocalFunction(BatchFunction):
    def transform(self, values):
        return [str(value).upper() for value in values]


class _ControlledLocalUdf(Udf, name="mock.controlled"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("value",)

    def signature(self):
        return (
            Param("value", required=True),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return _ControlledLocalFunction


class _FakeSparkContext:
    def __init__(self) -> None:
        self.pyfiles: list[str] = []

    def addPyFile(self, path: str) -> None:
        self.pyfiles.append(path)


class _FakeSparkSession:
    def __init__(self) -> None:
        self.sparkContext = _FakeSparkContext()


class _FakeSparkUdfRegistry:
    def __init__(self) -> None:
        self.registered: dict[str, tuple[object, object]] = {}

    def register(self, name: str, func, returnType=None):
        self.registered[name] = (func, returnType)


class _FakeHttpSparkSession(_FakeSparkSession):
    def __init__(self) -> None:
        super().__init__()
        self.udf = _FakeSparkUdfRegistry()


class _MemoryDiagnosticSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def emit(self, payload: dict[str, object]) -> None:
        self.events.append(payload)


def test_spark_udf_runtime_invoker_handles_builtin_aisystems_chat_without_remote_endpoint():
    invoker = SparkUdfRuntimeInvoker()
    definition = SimpleNamespace(
        kind="remote",
        canonical_name="aisystems.llm.chat",
        metadata={},
    )

    assert invoker.can_handle(definition) is True


def test_spark_udf_runtime_invoker_defers_when_builtin_aisystems_chat_has_remote_endpoint():
    invoker = SparkUdfRuntimeInvoker()
    definition = SimpleNamespace(
        kind="remote",
        canonical_name="aisystems.llm.chat",
        metadata={"base_url": "http://fixture-runtime", "invoke_path": "/invoke/chat"},
    )

    assert invoker.can_handle(definition) is False


def test_stub_runtime_invoker_refuses_concrete_function_runner_definition():
    definition = SimpleNamespace(
        kind="python",
        canonical_name="aisystems.llm.chat",
        runtime_alias="aisystems_llm_chat",
        metadata={"execution_runtime": "function_runner", "pool_kind": "service"},
    )

    assert StubRuntimeFunctionInvoker().can_handle(definition) is False


def test_spark_udf_runtime_invoker_does_not_claim_other_remote_functions():
    invoker = SparkUdfRuntimeInvoker()
    definition = SimpleNamespace(
        kind="remote",
        canonical_name="aisystems.http.get",
        metadata={},
    )

    assert invoker.can_handle(definition) is False


def test_spark_udf_runtime_invoker_defers_registered_builtin_when_declared_signature_differs():
    invoker = SparkUdfRuntimeInvoker()
    definition = SimpleNamespace(
        kind="python",
        canonical_name="aisystems.http.get",
        metadata={},
        parameters=[
            FunctionParameterIR(name="text", type_sql="STRING"),
            FunctionParameterIR(name="model", type_sql="STRING", has_default=True),
        ],
    )

    assert invoker.can_handle(definition) is False


def test_spark_udf_runtime_invoker_handles_explicitly_registered_local_python_udf_without_signature() -> None:
    register_udf(_LocalOnlyUdf)
    try:
        invoker = SparkUdfRuntimeInvoker()
        definition = SimpleNamespace(
            kind="python",
            canonical_name="mock.local",
            metadata={},
            parameters=[],
        )

        assert invoker.can_handle(definition) is True
    finally:
        clear_registered_udfs()


def test_wrapped_spark_udf_runtime_invoker_infers_control_argument_from_udf_signature() -> None:
    register_udf(_ControlledLocalUdf)
    try:
        spark = _FakeHttpSparkSession()
        definition = SimpleNamespace(
            kind="python",
            canonical_name="mock.controlled",
            runtime_alias="mock_controlled",
            metadata={},
            parameters=[
                FunctionParameterIR(name="value", type_sql="STRING"),
                FunctionParameterIR(name="limiter", type_sql="ANY"),
            ],
        )

        SparkUdfRuntimeInvoker(package_distributor=SparkWorkerPackageDistributor()).register_wrapped(spark, definition)

        func, _return_type = spark.udf.registered["agentcicd_wrapped_mock_controlled"]
        result = func(
            {"value": "hello", "metadata": {"errors": []}, "__agentcicd_cell": True},
            {
                "value": {"key": "small_judge_ratelimit", "max_in_flight": 4},
                "metadata": {"errors": []},
                "__agentcicd_cell": True,
            },
        )

        assert result["value"] == "HELLO"
        assert result["metadata"]["errors"] == []
        assert isinstance(result["metadata"]["latency_ms"], int)
    finally:
        clear_registered_udfs()


def test_spark_udf_runtime_invoker_matches_when_external_control_parameter_type_is_stale() -> None:
    register_udf(_ControlledLocalUdf)
    try:
        invoker = SparkUdfRuntimeInvoker()
        definition = SimpleNamespace(
            kind="python",
            canonical_name="mock.controlled",
            metadata={},
            parameters=[
                FunctionParameterIR(name="value", type_sql="STRING"),
                FunctionParameterIR(name="limiter", type_sql="ANY"),
            ],
        )

        assert invoker.can_handle(definition) is True
    finally:
        clear_registered_udfs()


def test_worker_package_distributor_uses_stable_archive_once_per_spark_context(tmp_path, monkeypatch) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime_package_distribution, "_local_package_roots", lambda: [("demo", package_root)])
    spark = _FakeSparkSession()
    distributor = SparkWorkerPackageDistributor()

    distributor.ensure_distributed(spark)
    distributor.ensure_distributed(spark)

    assert len(spark.sparkContext.pyfiles) == 1
    archive_path = spark.sparkContext.pyfiles[0]
    assert archive_path.endswith(f"/agentcicd_ir_pyfiles_{id(spark.sparkContext)}/demo_src.zip")


def test_worker_package_distributor_archives_only_the_runtime_package(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "site-packages"
    package_root = site_packages / "demo"
    dependency_root = site_packages / "compiled_dependency"
    package_root.mkdir(parents=True)
    dependency_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (dependency_root / "extension.so").write_bytes(b"not-a-real-extension")
    monkeypatch.setattr(runtime_package_distribution, "_local_package_roots", lambda: [("demo", package_root)])
    spark = _FakeSparkSession()

    SparkWorkerPackageDistributor().ensure_distributed(spark)

    with zipfile.ZipFile(spark.sparkContext.pyfiles[0]) as archive:
        archived_paths = set(archive.namelist())
    assert "demo/__init__.py" in archived_paths
    assert all(not path.startswith("compiled_dependency/") for path in archived_paths)


def test_http_runtime_payload_normalizes_decimal_values() -> None:
    payload = runtime_functions._json_payload_value(
        {
            "whole": Decimal("5.0"),
            "fractional": Decimal("5.25"),
            "nested": [Decimal("2"), {"value": Decimal("3.5")}],
        }
    )

    assert payload == {
        "whole": 5,
        "fractional": 5.25,
        "nested": [2, {"value": 3.5}],
    }


def test_http_runtime_payload_preserves_row_field_names() -> None:
    pyspark_row = __import__("pyspark.sql", fromlist=["Row"]).Row

    payload = runtime_functions._json_payload_value(
        pyspark_row(id=1, value=Decimal("10"), nested=pyspark_row(status="ok"))
    )

    assert payload == {"id": 1, "value": 10, "nested": {"status": "ok"}}


def test_http_runtime_payload_normalizes_numpy_arrays() -> None:
    np = __import__("numpy")

    payload = runtime_functions._json_payload_value(
        {
            "items": np.array([{"path": "a.txt"}, {"path": "b.txt"}], dtype=object),
            "count": np.int64(2),
        }
    )

    assert payload == {"items": [{"path": "a.txt"}, {"path": "b.txt"}], "count": 2}
    json.dumps(payload)


def test_http_runtime_timeout_uses_metadata_with_safe_default() -> None:
    assert runtime_functions._http_timeout_seconds({}, default=900) == 900
    assert runtime_functions._http_timeout_seconds({"timeout_seconds": "120"}, default=900) == 120
    assert runtime_functions._http_timeout_seconds({"http_timeout_seconds": 45.5}, default=900) == 45
    assert runtime_functions._http_timeout_seconds({"timeout_seconds": 0}, default=900) == 900
    assert runtime_functions._http_timeout_seconds({"timeout_seconds": "bad"}, default=900) == 900


def test_http_runtime_registers_outputs_from_registered_schema(monkeypatch) -> None:
    pyspark_types = __import__("pyspark.sql.types", fromlist=["MapType", "StringType", "VariantType"])
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "object", "additionalProperties": {"type": "json"}},
        },
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":{"history":[{"message":"hi"}],"terminated_by":"done"}}'

    monkeypatch.setattr(runtime_http_invoker, "urlopen", lambda request, timeout: _Response())

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, return_type = spark.udf.registered["support_sim"]
    result = func("case-1")
    assert isinstance(return_type, pyspark_types.MapType)
    assert isinstance(return_type.keyType, pyspark_types.StringType)
    assert isinstance(return_type.valueType, pyspark_types.VariantType)
    assert result["history"].toJson() == '[{"message":"hi"}]'
    assert result["terminated_by"].toJson() == '"done"'


def test_http_runtime_consumes_ratelimit_control_argument(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[
            FunctionParameterIR(name="case_id", type_sql="STRING"),
            FunctionParameterIR(name="limiter", type_sql="RATELIMIT"),
        ],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":"ok"}'

    class _Limiter:
        def acquire_blocking(self, *, permits: int = 1):
            class _Lease:
                def __enter__(self):
                    captured["lease"] = True
                    return self

                def __exit__(self, *args):
                    return None

            return _Lease()

    def _runtime_limiter(default=None, *, key="default"):
        captured["limiter"] = {"default": default, "key": key}
        return _Limiter()

    def _urlopen(request, timeout):
        captured["body"] = request.data.decode("utf-8")
        return _Response()

    monkeypatch.setattr(runtime_http_invoker, "runtime_limiter", _runtime_limiter)
    monkeypatch.setattr(runtime_http_invoker, "urlopen", _urlopen)

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["support_sim"]
    assert func("case-1", {"key": "openai_ratelimit", "max_in_flight": 4}) == "ok"
    assert captured["limiter"] == {"default": 4, "key": "openai_ratelimit"}
    assert captured["body"] == '{"args": {"case_id": "case-1"}}'


def test_http_runtime_consumes_pool_control_argument_before_remote_call(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[
            FunctionParameterIR(name="case_id", type_sql="STRING"),
            FunctionParameterIR(name="pool", type_sql="POOL"),
        ],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":"ok"}'

    class _PoolContext:
        def __enter__(self):
            captured["pool_entered"] = True
            return PoolLease(
                pool_name="session_pool",
                pool_kind="session",
                lease_id="lease.123",
                acquired_at=1.0,
                node_id="manager.1",
                manager_id="manager.1",
                worker_slot_id="manager.1.slot-1",
                generation=3,
                address="http://leased-fixture",
                request_id="request.1",
                fixture_id="fixture.123",
            )

        def __exit__(self, *args):
            captured["pool_exited"] = True
            return None

    def _runtime_pool_for_control_values(values, *, fallback_address=None, definition=None, payload_args=None):
        captured["pool_values"] = values
        captured["fallback_address"] = fallback_address
        captured["definition"] = definition
        captured["payload_args"] = payload_args
        return _PoolContext()

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return _Response()

    monkeypatch.setattr(runtime_http_invoker, "_runtime_pool_for_control_values", _runtime_pool_for_control_values)
    monkeypatch.setattr(runtime_http_invoker, "urlopen", _urlopen)

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["support_sim"]
    assert func("case-1", {"key": "session_pool", "config_json": '{"kind":"session","max_instances":1}'}) == "ok"
    assert captured["pool_entered"] is True
    assert captured["pool_exited"] is True
    assert captured["fallback_address"] == "http://fixture-runtime"
    assert captured["url"] == "http://leased-fixture/invoke/sim"
    assert json.loads(str(captured["body"])) == {
        "args": {"case_id": "case-1"},
        "lease": {
            "pool_name": "session_pool",
            "pool_kind": "session",
            "lease_id": "lease.123",
            "acquired_at": 1.0,
            "node_id": "manager.1",
            "manager_id": "manager.1",
            "worker_slot_id": "manager.1.slot-1",
            "generation": 3,
            "address": "http://leased-fixture",
            "request_id": "request.1",
            "fixture_id": "fixture.123",
        },
    }


def test_http_runtime_wrapped_pool_control_sends_lease_to_remote_manager(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[
            FunctionParameterIR(name="case_id", type_sql="STRING"),
            FunctionParameterIR(name="pool", type_sql="POOL"),
        ],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":"ok"}'

    class _PoolContext:
        def __enter__(self):
            return PoolLease(
                pool_name="sandbox_pool",
                pool_kind="sandbox",
                lease_id="lease.456",
                acquired_at=2.0,
                manager_id="manager.2",
                worker_slot_id="manager.2.slot-1",
                generation=4,
                address="http://sandbox-manager",
                fixture_id="fixture.456",
            )

        def __exit__(self, *args):
            return None

    def _runtime_pool_for_control_values(values, *, fallback_address=None, definition=None, payload_args=None):
        return _PoolContext()

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return _Response()

    monkeypatch.setattr(runtime_http_invoker, "_runtime_pool_for_control_values", _runtime_pool_for_control_values)
    monkeypatch.setattr(runtime_http_invoker, "urlopen", _urlopen)

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["agentcicd_wrapped_support_sim"]
    result = func(
        {"value": "case-1", "metadata": {"errors": []}, "__agentcicd_cell": True},
        {
            "value": {"key": "sandbox_pool", "config_json": '{"kind":"sandbox","max_instances":1}'},
            "metadata": {"errors": []},
            "__agentcicd_cell": True,
        },
    )

    assert result["value"] == "ok"
    assert captured["url"] == "http://sandbox-manager/invoke/sim"
    assert json.loads(str(captured["body"]))["lease"] == {
        "pool_name": "sandbox_pool",
        "pool_kind": "sandbox",
        "lease_id": "lease.456",
        "acquired_at": 2.0,
        "manager_id": "manager.2",
        "worker_slot_id": "manager.2.slot-1",
        "generation": 4,
        "address": "http://sandbox-manager",
        "fixture_id": "fixture.456",
    }


def test_http_runtime_wrapped_alias_fans_out_vector_rows(monkeypatch) -> None:
    import pandas as pd

    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[
            FunctionParameterIR(name="case_id", type_sql="STRING"),
            FunctionParameterIR(name="limiter", type_sql="RATELIMIT"),
        ],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    lock = threading.Lock()
    active = 0
    max_active = 0
    seen_cases: list[str] = []

    class _Response:
        def __init__(self, case_id: str):
            self.case_id = case_id

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"result": f"{self.case_id}:ok"}).encode("utf-8")

    def _urlopen(request, timeout):
        nonlocal active, max_active
        case_id = json.loads(request.data.decode("utf-8"))["args"]["case_id"]
        with lock:
            seen_cases.append(case_id)
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _Response(case_id)

    monkeypatch.setattr(runtime_http_invoker, "urlopen", _urlopen)

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["agentcicd_wrapped_support_sim"]
    case_cells = pd.Series(
        [
            {"value": f"case-{index}", "metadata": {"errors": []}, "__agentcicd_cell": True}
            for index in range(4)
        ]
    )
    limiter_cells = pd.Series(
        [
            {
                "value": {"key": "agent_pool", "max_in_flight": 4},
                "metadata": {"errors": []},
                "__agentcicd_cell": True,
            }
            for _ in range(4)
        ]
    )

    result = func(case_cells, limiter_cells)

    assert list(result["value"]) == [
        "case-0:ok",
        "case-1:ok",
        "case-2:ok",
        "case-3:ok",
    ]
    assert list(result["__agentcicd_cell"]) == [True, True, True, True]
    assert sorted(seen_cases) == ["case-0", "case-1", "case-2", "case-3"]
    assert max_active > 1


def test_http_runtime_vector_concurrency_uses_pool_control() -> None:
    rows = [
        (
            f"case-{index}",
            {"key": "agent_pool", "config_json": '{"kind":"session","max_instances":2}'},
        )
        for index in range(5)
    ]

    assert runtime_http_invoker._vector_max_concurrency(rows, {1}) == 2


def test_http_runtime_vector_concurrency_ignores_service_pool_capacity() -> None:
    rows = [
        (
            f"case-{index}",
            {"key": "service_pool", "config_json": '{"kind":"service","max_instances":3}'},
            {"key": "agent_ratelimit", "max_in_flight": 9},
        )
        for index in range(12)
    ]

    assert runtime_http_invoker._vector_max_concurrency(rows, {1, 2}) == 9


def test_http_runtime_limiter_ignores_pool_control_dict() -> None:
    limiter_key, max_in_flight = runtime_http_invoker._limiter_from_control_values(
        [
            {"key": "service_pool", "config_json": '{"kind":"service","max_instances":3}'},
            {"key": "agent_ratelimit", "max_in_flight": 1},
        ],
        fallback_key="default",
    )

    assert limiter_key == "agent_ratelimit"
    assert max_in_flight == 1


def test_http_runtime_uses_pool_timeout_for_remote_call(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    seen_timeouts: list[int] = []
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[
            FunctionParameterIR(name="case_id", type_sql="STRING"),
            FunctionParameterIR(name="pool", type_sql="POOL"),
        ],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
            "timeout_seconds": 900,
        },
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":"ok"}'

    def _urlopen(request, timeout):
        seen_timeouts.append(timeout)
        return _Response()

    monkeypatch.setattr(runtime_http_invoker, "urlopen", _urlopen)

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["support_sim"]
    result = func("case-1", {"key": "service_pool", "config_json": '{"kind":"service","timeout_seconds":90000}'})

    assert result == "ok"
    assert seen_timeouts == [90000]


def test_http_runtime_pool_lease_serialization_defaults_generation() -> None:
    lease = PoolLease(
        pool_name="session_pool",
        pool_kind="session",
        lease_id="lease.123",
        acquired_at=1.0,
        node_id="manager.1",
        manager_id="manager.1",
        worker_slot_id="manager.1.slot-1",
        generation=None,
        address="http://leased-fixture",
        request_id="request.1",
        fixture_id="fixture.123",
    )

    assert runtime_http_invoker._serialized_pool_lease(lease) == {
        "pool_name": "session_pool",
        "pool_kind": "session",
        "lease_id": "lease.123",
        "acquired_at": 1.0,
        "node_id": "manager.1",
        "manager_id": "manager.1",
        "worker_slot_id": "manager.1.slot-1",
        "generation": 1,
        "address": "http://leased-fixture",
        "request_id": "request.1",
        "fixture_id": "fixture.123",
    }


def test_http_runtime_registers_json_output_schema_as_variant(monkeypatch) -> None:
    pyspark_types = __import__("pyspark.sql.types", fromlist=["VariantType"])
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "json"},
        },
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":{"history":[{"message":"hi"}]}}'

    monkeypatch.setattr(runtime_http_invoker, "urlopen", lambda request, timeout: _Response())

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, return_type = spark.udf.registered["support_sim"]
    result = func("case-1")
    assert isinstance(return_type, pyspark_types.VariantType)
    assert result.toJson() == '{"history":[{"message":"hi"}]}'


def test_http_runtime_registers_wrapped_alias_with_cell_envelope(monkeypatch) -> None:
    pyspark_types = __import__("pyspark.sql.types", fromlist=["StructType"])
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"result":"ok"}'

    monkeypatch.setattr(runtime_http_invoker, "urlopen", lambda request, timeout: _Response())

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, return_type = spark.udf.registered["agentcicd_wrapped_support_sim"]
    result = func(
        {
            "value": "case-1",
            "metadata": {"errors": []},
            "__agentcicd_cell": True,
        }
    )
    assert isinstance(return_type, pyspark_types.StructType)
    assert result["value"] == "ok"
    assert result["metadata"]["errors"] == []
    assert isinstance(result["metadata"]["latency_ms"], int)
    assert "lineage" not in result["metadata"]
    assert result["__agentcicd_cell"] is True


def test_http_runtime_wrapped_alias_short_circuits_input_errors(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    called = False

    def _urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("urlopen should not be called for errored input cells")

    monkeypatch.setattr(runtime_http_invoker, "urlopen", _urlopen)

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["agentcicd_wrapped_support_sim"]
    input_error = {
        "code": "UPSTREAM_ERROR",
        "message": "bad input",
        "source": "prepared.case_id",
        "path": None,
        "recoverable": True,
        "cause_code": None,
        "cause_message": None,
        "details": {},
    }
    result = func({"value": None, "metadata": {"errors": [input_error]}, "__agentcicd_cell": True})
    assert called is False
    assert result["value"] is None
    assert result["metadata"]["errors"] == [input_error]


def test_http_runtime_wrapped_alias_converts_http_errors_to_recoverable_cell(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    monkeypatch.setattr(
        runtime_http_invoker,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            HTTPError("http://fixture-runtime/invoke/sim", 500, "boom", None, BytesIO(b"fixture failed"))
        ),
    )

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["agentcicd_wrapped_support_sim"]
    result = func({"value": "case-1", "metadata": {"errors": []}, "__agentcicd_cell": True})
    assert result["value"] is None
    assert result["metadata"]["errors"][0]["code"] == "AGENTCICD_RUNTIME_HTTP_ERROR"
    assert result["metadata"]["errors"][0]["recoverable"] is True
    assert isinstance(result["metadata"]["latency_ms"], int)
    assert "fixture failed" in result["metadata"]["errors"][0]["message"]


def test_http_runtime_wrapped_alias_uses_trace_summary_from_http_error(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    remote_trace_summary = {
        "schema_version": "agentcicd.fixture_trace.v1",
        "trace_id": "trace-http-error",
        "span_id": "root-span",
        "parent_span_id": "root-span",
        "function_name": "support.sim",
        "runtime_alias": "support_sim",
        "status": "error",
        "trace_summary_path": "debug/fixture_traces/trace-http-error/summary.json",
        "trace_spans_path": "debug/fixture_traces/trace-http-error/spans.jsonl",
    }
    error_body = json.dumps(
        {
            "error": "invoke_timeout",
            "detail": "Fixture invocation exceeded 300s",
            "trace_summary": remote_trace_summary,
        }
    ).encode("utf-8")

    class _Trace:
        def __init__(self, **_kwargs):
            self.extended = []

        def request_context(self):
            return {
                "trace_id": "trace-http-error",
                "parent_span_id": "root-span",
                "parent_call_id": "rtcall_root",
            }

        def extend_records(self, records):
            self.extended.extend(records or [])

        def finish(self, **kwargs):
            return {"status": kwargs["status"], "extended": self.extended}

    monkeypatch.setattr(runtime_http_invoker, "start_fixture_trace", lambda **kwargs: _Trace(**kwargs))
    monkeypatch.setattr(
        runtime_http_invoker,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            HTTPError("http://fixture-runtime/invoke/sim", 408, "timeout", None, BytesIO(error_body))
        ),
    )

    HttpRuntimeFunctionInvoker().register(spark, definition)

    func, _ = spark.udf.registered["agentcicd_wrapped_support_sim"]
    result = func({"value": "case-1", "metadata": {"errors": []}, "__agentcicd_cell": True})

    assert result["metadata"]["errors"][0]["code"] == "AGENTCICD_RUNTIME_HTTP_ERROR"
    assert result["metadata"]["fixture_trace"] == remote_trace_summary


def test_http_runtime_wrapped_alias_emits_diagnostic_for_http_400(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    diagnostics = _MemoryDiagnosticSink()
    definition = SimpleNamespace(
        canonical_name="support.sim",
        runtime_alias="support_sim",
        parameters=[FunctionParameterIR(name="case_id", type_sql="STRING")],
        metadata={
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/sim",
            "output_schema": {"type": "string"},
        },
    )
    monkeypatch.setattr(
        runtime_http_invoker,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            HTTPError("http://fixture-runtime/invoke/sim", 400, "bad request", None, BytesIO(b"bad payload"))
        ),
    )

    HttpRuntimeFunctionInvoker(diagnostic_sink=diagnostics).register(spark, definition)

    func, _ = spark.udf.registered["agentcicd_wrapped_support_sim"]
    result = func({"value": "case-1", "metadata": {"errors": []}, "__agentcicd_cell": True})

    assert result["metadata"]["errors"][0]["code"] == "AGENTCICD_RUNTIME_HTTP_ERROR"
    assert "bad payload" in result["metadata"]["errors"][0]["message"]
    assert diagnostics.events[0]["event"] == "runtime_call.failed"
    assert diagnostics.events[0]["details"]["http_status"] == 400
    assert diagnostics.events[0]["details"]["remote_detail"] == "bad payload"


def test_local_fixture_runtime_invoker_registers_optional_builtin_callable(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="string.extract_from_fence",
        runtime_alias="string_extract_from_fence",
        metadata={"execution_runtime": "function_runner", "output_schema": {"type": "string"}},
        parameters=[
            FunctionParameterIR(name="content", type_sql="STRING"),
            FunctionParameterIR(name="fence_type", type_sql="STRING", has_default=True),
        ],
    )

    def _fake_fixture_callable(**kwargs):
        return f"{kwargs['fence_type']}:{kwargs['content']}"

    monkeypatch.setattr(runtime_local_fixture, "_fixture_callable_for_name", lambda name: _fake_fixture_callable)

    invoker = LocalFixtureRuntimeInvoker(package_distributor=SparkWorkerPackageDistributor())
    assert invoker.can_handle(definition) is True
    invoker.register(spark, definition)

    func, _return_type = spark.udf.registered["string_extract_from_fence"]
    assert func("body", "json") == "json:body"


def test_wrapped_local_fixture_runtime_invoker_captures_trace(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        canonical_name="string.extract_from_fence",
        runtime_alias="string_extract_from_fence",
        metadata={"execution_runtime": "function_runner", "output_schema": {"type": "string"}},
        parameters=[FunctionParameterIR(name="content", type_sql="STRING")],
    )

    monkeypatch.setattr(runtime_local_fixture, "_fixture_callable_for_name", lambda name: lambda **kwargs: kwargs["content"].upper())
    traces = []

    class _Trace:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def finish(self, **kwargs):
            traces.append({**self.kwargs, **kwargs})
            return {
                "backend": self.kwargs["backend"],
                "execution_runtime": self.kwargs["execution_runtime"],
                "status": kwargs["status"],
            }

    monkeypatch.setattr(runtime_local_fixture, "start_fixture_trace", lambda **kwargs: _Trace(**kwargs))

    LocalFixtureRuntimeInvoker(package_distributor=SparkWorkerPackageDistributor()).register_wrapped(spark, definition)

    func, _return_type = spark.udf.registered["agentcicd_wrapped_string_extract_from_fence"]
    result = func({"value": "hello", "metadata": {"errors": []}, "__agentcicd_cell": True})

    assert result["value"] == "HELLO"
    assert result["metadata"]["errors"] == []
    assert result["metadata"]["fixture_trace"]["backend"] == "local_fixture"
    assert result["metadata"]["fixture_trace"]["execution_runtime"] == "function_runner"
    assert traces[0]["function_name"] == "string.extract_from_fence"
    assert traces[0]["status"] == "ok"


def test_remote_result_coercion_unwraps_nested_cell_payloads() -> None:
    return_type = ArrayType(
        StructType(
            [
                StructField("case_id", SparkStringType(), True),
                StructField("braid_factor", DoubleType(), True),
            ]
        )
    )
    payload = [
        {
            "case_id": "maze-1",
            "braid_factor": {
                "cell_id": "inner",
                "value": 0.35,
                "metadata": {"errors": [], "latency_ms": 12, "fixture_trace": None},
                "__agentcicd_cell": True,
            },
        }
    ]

    assert _coerce_remote_result(payload, return_type) == [{"case_id": "maze-1", "braid_factor": 0.35}]


def test_local_fixture_runtime_invoker_defers_without_optional_fixture_callable(monkeypatch) -> None:
    definition = SimpleNamespace(
        canonical_name="string.extract_from_fence",
        runtime_alias="string_extract_from_fence",
        metadata={"execution_runtime": "function_runner"},
        parameters=[],
    )
    monkeypatch.setattr(runtime_local_fixture, "_fixture_callable_for_name", lambda name: None)

    assert LocalFixtureRuntimeInvoker().can_handle(definition) is False


def test_local_fixture_runtime_invoker_defers_stateful_pool_function(monkeypatch) -> None:
    definition = SimpleNamespace(
        canonical_name="envs.agent_harness.run_task",
        runtime_alias="envs_agent_harness_run_task",
        metadata={"execution_runtime": "function_runner", "pool_kind": "session"},
        parameters=[],
    )
    monkeypatch.setattr(runtime_local_fixture, "_fixture_callable_for_name", lambda name: lambda **kwargs: "unused")

    assert LocalFixtureRuntimeInvoker().can_handle(definition) is False


def test_composite_runtime_invoker_prefers_optional_local_fixture_before_http(monkeypatch) -> None:
    spark = _FakeHttpSparkSession()
    definition = SimpleNamespace(
        kind="remote",
        canonical_name="string.extract_from_fence",
        runtime_alias="string_extract_from_fence",
        metadata={
            "execution_runtime": "function_runner",
            "base_url": "http://fixture-runtime",
            "invoke_path": "/invoke/extract_from_fence",
            "output_schema": {"type": "string"},
        },
        parameters=[FunctionParameterIR(name="content", type_sql="STRING")],
    )
    monkeypatch.setattr(runtime_local_fixture, "_fixture_callable_for_name", lambda name: lambda **kwargs: "local")

    CompositeRuntimeFunctionInvoker().register(spark, definition)

    func, _return_type = spark.udf.registered["string_extract_from_fence"]
    assert func("remote would fail") == "local"


def test_composite_runtime_invoker_shares_package_distributor_across_default_invokers() -> None:
    invoker = CompositeRuntimeFunctionInvoker()
    spark_udf_invoker = invoker._invokers[0]
    local_fixture_invoker = invoker._invokers[1]
    http_invoker = invoker._invokers[2]

    assert spark_udf_invoker._package_distributor is local_fixture_invoker.package_distributor
    assert spark_udf_invoker._package_distributor is http_invoker.package_distributor
