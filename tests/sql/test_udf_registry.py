import json
import os
from pathlib import Path

import pytest

from agentcicd.sql.runtime.udf_compat.function import AggregateFunction, BatchFunction, RowExplodeFunction
from agentcicd.sql.runtime.udf_compat.types import FType, JsonType, StringType
from agentcicd.sql.runtime.udf_compat.udf import Param, Udf
from agentcicd.sql.engine import spark_udf
from agentcicd.sql.fixture_manifest import builtin_registered_function_specs
from agentcicd.sql.ir.functions import RegisteredFunctionSpec
from agentcicd.sql.parsing.runtime_signature_registry import (
    clear_registered_runtime_signatures,
    get_runtime_signature,
)
from agentcicd.sql.semantics.registry import build_function_registry
from agentcicd.sql import udf_registry


class DummyUdfRegistry:
    def __init__(self):
        self.registry = {}

    def register(self, name, func):
        self.registry[name] = func


class DummySparkSession:
    def __init__(self):
        self.udf = DummyUdfRegistry()


class EchoBatchFunction(BatchFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def transform(self, texts):
        return [text.upper() for text in texts]


class ExplodeFunction(RowExplodeFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.ROW_EXPLODE_FUNCTION

    def explode(self, value):
        return [value, value]


class AggregateValues(AggregateFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.AGGREGATE_FUNCTION

    def aggregate(self, values):
        return ",".join(values)


class BatchUdf(Udf, name="batch_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("texts",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return EchoBatchFunction


class PrefixedBatchUdf(Udf, name="agentcicd.prefixed_batch_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("texts",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return EchoBatchFunction


class ControlledBatchUdf(Udf, name="controlled_batch_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("texts",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return EchoBatchFunction

    def signature(self):
        return (
            Param("texts", required=True),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

class OptionalPromptBatchFunction(BatchFunction):
    def transform(self, prompt=None, aisystem_id=None, messages=None):
        return [prompt]


class OptionalPromptUdf(Udf, name="optional_prompt_udf"):
    def input_schema(self):
        return (StringType(), StringType(), JsonType())

    def input_args(self):
        return ("prompt", "aisystem_id", "messages")

    def output_schema(self):
        return JsonType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return OptionalPromptBatchFunction


class JsonBatchFunction(BatchFunction):
    def transform(self, texts):
        return [{"text": text} for text in texts]


class JsonBatchUdf(Udf, name="json_batch_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("texts",)

    def output_schema(self):
        return JsonType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return JsonBatchFunction


class ParseJsonFunction(BatchFunction):
    def transform(self, values):
        return [json.loads(value) for value in values]


class ParseJsonUdf(Udf, name="data.parse_json"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("value",)

    def output_schema(self):
        return JsonType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return ParseJsonFunction


class AISystemsLLMChatFunction(BatchFunction):
    def transform(self, aisystem_id, messages):
        return [{"aisystem_id": item, "messages": payload} for item, payload in zip(aisystem_id, messages)]


class AISystemsLLMChatUdf(Udf, name="aisystems.llm.chat"):
    def input_schema(self):
        return (StringType(), JsonType())

    def input_args(self):
        return ("aisystem_id", "messages")

    def output_schema(self):
        return JsonType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return AISystemsLLMChatFunction


class ExplodeUdf(Udf, name="explode_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("value",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.ROW_EXPLODE_FUNCTION

    def function(self):
        return ExplodeFunction


class AggregateUdf(Udf, name="aggregate_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("values",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.AGGREGATE_FUNCTION

    def function(self):
        return AggregateValues


class UnknownUdf(Udf, name="unknown_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("texts",)

    def output_schema(self):
        return StringType()

    def ftype(self):  # type: ignore[override]
        return "UNKNOWN"

    def function(self):
        return EchoBatchFunction


class UnregisteredUdf(Udf):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("texts",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return EchoBatchFunction


def _test_spark_pythonpath() -> str:
    repo_root = Path(__file__).resolve().parents[3]
    paths = [
        repo_root / "agentcicd" / "src",
        repo_root / "agentcicd" / "tests" / "sql",
    ]
    existing = os.environ.get("PYTHONPATH")
    values = [str(path) for path in paths]
    if existing:
        values.append(existing)
    return os.pathsep.join(values)


def _json_value(value):
    try:
        from pyspark.sql.types import VariantVal
    except ImportError:
        VariantVal = ()
    if isinstance(value, VariantVal):
        return json.loads(value.toJson())
    return json.loads(value)


class _TrackedLimiter:
    def __init__(self, key: str, calls: list[object], max_in_flight: int | None = None) -> None:
        self.key = key
        self.calls = calls
        self.max_in_flight = max_in_flight

    def acquire_blocking(self, *, permits: int = 1):
        limiter = self

        class _Lease:
            def __enter__(self):
                limiter.calls.append((limiter.key, limiter.max_in_flight))
                return self

            def __exit__(self, *args):
                return None

        return _Lease()


@pytest.fixture(autouse=True)
def _reset_udf_registry(monkeypatch):
    udf_registry.clear_registered_udfs()
    clear_registered_runtime_signatures()

    def _identity_udf(_return_type=None):
        def decorator(func):
            return func
        return decorator

    def _identity_arrow_udf(func=None, returnType=None, *, useArrow=None):
        _ = returnType, useArrow
        if func is None:
            return lambda inner: inner
        return func

    monkeypatch.setattr(spark_udf, "pandas_udf", _identity_udf)
    monkeypatch.setattr(spark_udf, "spark_udf", _identity_arrow_udf)


def test_register_udf_tracks_name():
    registered_name = udf_registry.register_udf(BatchUdf)

    assert registered_name == "batch_udf"
    assert udf_registry.get_registered_udf("batch_udf") is BatchUdf
    assert udf_registry.registered_udf_name(BatchUdf) == "batch_udf"


def test_register_udf_canonicalizes_optional_agentcicd_prefix():
    registered_name = udf_registry.register_udf(PrefixedBatchUdf)

    assert registered_name == "prefixed_batch_udf"
    assert udf_registry.canonical_udf_name("agentcicd.prefixed_batch_udf") == "prefixed_batch_udf"
    assert udf_registry.get_registered_udf("prefixed_batch_udf") is PrefixedBatchUdf


def test_builtin_runtime_signature_loads_without_optional_runtime_dependencies():
    signature = get_runtime_signature("aisystems.llm.chat")

    assert signature is not None
    assert signature.runtime_alias == "aisystems_llm_chat"
    assert signature.returns_json is True
    assert signature.has_default_by_name["aisystem_id"] is False
    assert signature.has_default_by_name["messages"] is False
    assert signature.type_sql_by_name["limiter"] == "RATELIMIT"
    assert signature.type_sql_by_name["pool"] == "POOL"
    assert "prompt" not in signature.input_args


def test_builtin_manifest_exposes_optional_ratelimit_control_parameter():
    expected_names = (
        "aisystems.llm.chat",
        "aisystems.llm.messages",
        "aisystems.llm.responses",
        "aisystems.a2a.send_message",
        "http.request",
        "http.get",
        "http.post",
        "aisystems.http.get",
        "aisystems.http.post",
        "agent.ragas.context_precision",
        "agent.ragas.context_recall",
        "agent.ragas.context_entities_recall",
        "agent.ragas.noise_sensitivity",
        "agent.ragas.response_relevancy",
        "agent.ragas.faithfulness",
        "agent.ragas.multimodal_faithfulness",
        "agent.ragas.multimodal_relevance",
        "agent.ragas.topic_adherence",
        "agent.ragas.tool_call_accuracy",
        "agent.ragas.tool_call_f1",
        "agent.ragas.agent_goal_accuracy",
        "agent.ragas.aspect_critic",
        "agent.ragas.simple_criteria_scoring",
        "agent.ragas.rubrics_based_scoring",
        "agent.ragas.instance_specific_rubrics_scoring",
        "agent.ragas.summarization",
        "agent.ragas.execution_based_datacompy_score",
        "agent.ragas.sql_query_equivalence",
        "simulators.run",
    )
    specs = {spec.name: spec for spec in builtin_registered_function_specs()}

    for name in expected_names:
        spec = specs.get(name)
        assert spec is not None, name
        limiter = next((parameter for parameter in spec.signature if parameter.name == "limiter"), None)
        assert limiter is not None, name
        assert limiter.has_default is True, name
        assert limiter.type_sql == "RATELIMIT", name


def test_builtin_manifest_exposes_service_pool_for_llm_functions():
    specs = {spec.name: spec for spec in builtin_registered_function_specs()}

    for name in ("aisystems.llm.chat", "aisystems.llm.messages", "aisystems.llm.responses"):
        spec = specs[name]
        pool = next((parameter for parameter in spec.signature if parameter.name == "pool"), None)
        assert pool is not None, name
        assert pool.has_default is True, name
        assert pool.type_sql == "POOL", name
        assert spec.metadata["pool_kind"] == "service", name


def test_register_spark_udf_batch_and_call():
    spark = DummySparkSession()
    udf_registry.register_udf(BatchUdf)

    spark_udf.register_spark_udf(spark, BatchUdf)

    assert "batch_udf" in spark.udf.registry
    batch_func = spark.udf.registry["batch_udf"]

    with pytest.raises(ValueError, match="expects 1 arguments"):
        batch_func()

    assert batch_func("hi") == "HI"


def test_register_spark_udf_batch_does_not_split_scalar_strings():
    spark = DummySparkSession()
    udf_registry.register_udf(BatchUdf)

    spark_udf.register_spark_udf(spark, BatchUdf)

    batch_func = spark.udf.registry["batch_udf"]

    assert batch_func("there") == "THERE"


def test_register_spark_udf_batch_coerces_json_output():
    spark = DummySparkSession()
    udf_registry.register_udf(JsonBatchUdf)

    spark_udf.register_spark_udf(spark, JsonBatchUdf)

    batch_func = spark.udf.registry["json_batch_udf"]
    value = batch_func("hi")

    assert value.toJson() == '{"text":"hi"}'


def test_register_spark_udf_batch_acquires_default_runtime_limiter(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        spark_udf,
        "runtime_limiter",
        lambda default=None, *, key="default": _TrackedLimiter(key, calls, default),
    )
    spark = DummySparkSession()
    udf_registry.register_udf(BatchUdf)

    spark_udf.register_spark_udf(spark, BatchUdf)

    assert spark.udf.registry["batch_udf"]("hi") == "HI"
    assert calls == [("default", None)]


def test_register_spark_udf_uses_sql_control_runtime_limiter_key(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        spark_udf,
        "runtime_limiter",
        lambda default=None, *, key="default": _TrackedLimiter(key, calls, default),
    )
    spark = DummySparkSession()
    udf_registry.register_udf(ControlledBatchUdf)

    spark_udf.register_spark_udf(spark, ControlledBatchUdf, control_arg_indexes={1})

    assert spark.udf.registry["controlled_batch_udf"]("hi", {"key": "openai_ratelimit", "max_in_flight": 4}) == "HI"
    assert calls == [("openai_ratelimit", 4)]


def test_register_spark_udf_infers_control_argument_from_udf_signature(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        spark_udf,
        "runtime_limiter",
        lambda default=None, *, key="default": _TrackedLimiter(key, calls, default),
    )
    spark = DummySparkSession()
    udf_registry.register_udf(ControlledBatchUdf)

    spark_udf.register_spark_udf(spark, ControlledBatchUdf)

    assert spark.udf.registry["controlled_batch_udf"]("hi", {"key": "small_judge_ratelimit", "max_in_flight": 4}) == "HI"
    assert calls == [("small_judge_ratelimit", 4)]


def test_column_to_pylist_handles_scalar_variant_val():
    pyspark = pytest.importorskip("pyspark.sql")

    variant = pyspark.VariantVal.parseJson('{"case_id":"case-001"}')

    assert spark_udf._column_to_pylist(variant) == [{"case_id": "case-001"}]


def test_udf_input_args_are_explicit():
    assert BatchUdf().input_args() == ("texts",)


def test_python_udf_registry_infers_optional_arguments_from_transform_defaults():
    udf_registry.register_udf(OptionalPromptUdf)

    registry = build_function_registry(
        [],
        [
            RegisteredFunctionSpec(
                name="optional_prompt_udf",
                kind="python",
                call_name="optional_prompt_udf",
            )
        ],
    )

    resolved = registry.resolve("optional_prompt_udf")
    assert resolved is not None
    parameters = {parameter.name: parameter for parameter in resolved.parameters}
    assert parameters["prompt"].has_default is True
    assert parameters["aisystem_id"].has_default is True
    assert parameters["messages"].has_default is True


def test_register_spark_udf_row_explode_branch(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        spark_udf,
        "runtime_limiter",
        lambda default=None, *, key="default": _TrackedLimiter(key, calls, default),
    )
    spark = DummySparkSession()
    udf_registry.register_udf(ExplodeUdf)

    spark_udf.register_spark_udf(spark, ExplodeUdf)

    explode_func = spark.udf.registry["explode_udf"]
    result = explode_func("x")
    assert result == ["x", "x"]
    assert calls == [("default", None)]


def test_register_spark_udf_aggregate_branch(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        spark_udf,
        "runtime_limiter",
        lambda default=None, *, key="default": _TrackedLimiter(key, calls, default),
    )
    spark = DummySparkSession()
    udf_registry.register_udf(AggregateUdf)

    spark_udf.register_spark_udf(spark, AggregateUdf)

    aggregate_func = spark.udf.registry["aggregate_udf"]
    result = aggregate_func(["a", "b"])
    assert result == "a,b"
    assert calls == [("default", None)]


def test_register_spark_udf_unknown_type_skips():
    spark = DummySparkSession()
    udf_registry.register_udf(UnknownUdf)

    spark_udf.register_spark_udf(spark, UnknownUdf)

    assert "unknown_udf" not in spark.udf.registry


def test_register_spark_udf_requires_registered_name():
    spark = DummySparkSession()
    with pytest.raises(ValueError, match="is not registered"):
        spark_udf.register_spark_udf(spark, UnregisteredUdf)


def test_register_spark_udf_batch_works_with_pyspark_4_arrow_scalar_udf(monkeypatch):
    pyspark = pytest.importorskip("pyspark.sql")

    monkeypatch.undo()
    udf_registry.clear_registered_udfs()
    udf_registry.register_udf(ParseJsonUdf)
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-udf-registry-pyspark4")
        .config("spark.ui.enabled", "false")
        .config("spark.executorEnv.PYTHONPATH", _test_spark_pythonpath())
        .getOrCreate()
    )
    try:
        spark_udf.register_spark_udf(spark, ParseJsonUdf)
        rows = spark.createDataFrame([('{"a":1}',), ('{"b":[2]}',)], ["value"])
        result = rows.selectExpr("data_parse_json(value) AS out").collect()
        assert [_json_value(row.out) for row in result] == [{"a": 1}, {"b": [2]}]
    finally:
        spark.stop()


def test_register_builtin_llm_chat_udf_with_pyspark_4(monkeypatch):
    pyspark = pytest.importorskip("pyspark.sql")

    monkeypatch.undo()
    udf_registry.clear_registered_udfs()
    udf_registry.register_udf(AISystemsLLMChatUdf)
    spark = (
        pyspark.SparkSession.builder.master("local[1]")
        .appName("agentcicd-udf-registry-llm-chat-pyspark4")
        .config("spark.ui.enabled", "false")
        .config("spark.executorEnv.PYTHONPATH", _test_spark_pythonpath())
        .getOrCreate()
    )
    try:
        spark_udf.register_spark_udf(spark, AISystemsLLMChatUdf)
    finally:
        spark.stop()
