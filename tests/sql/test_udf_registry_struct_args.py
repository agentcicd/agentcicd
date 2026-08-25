import pandas as pd

from agentcicd.sql.runtime.udf_compat.function import BatchFunction
from agentcicd.sql.runtime.udf_compat.types import FType, StringType
from agentcicd.sql.runtime.udf_compat.udf import Udf
from agentcicd.sql.engine import spark_udf
from agentcicd.sql import udf_registry


class _DummyUdfRegistry:
    def __init__(self):
        self.registry = {}

    def register(self, name, func):
        self.registry[name] = func


class _DummySparkSession:
    def __init__(self):
        self.udf = _DummyUdfRegistry()


class _EchoBatchFunction(BatchFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def transform(self, values):
        return values


class _StructArgBatchUdf(Udf, name="struct_arg_batch_udf"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("values",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return _EchoBatchFunction


def test_register_spark_udf_accepts_pandas_dataframe_struct_arg(monkeypatch):
    udf_registry.clear_registered_udfs()
    udf_registry.register_udf(_StructArgBatchUdf)

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

    spark = _DummySparkSession()
    spark_udf.register_spark_udf(spark, _StructArgBatchUdf)
    func = spark.udf.registry["struct_arg_batch_udf"]

    struct_col = pd.DataFrame(
        {
            "value": ["a", "b"],
            "metadata": [
                {"error": None, "subdatatype": None},
                {"error": "err", "subdatatype": None},
            ],
            "__agentcicd_cell": [True, True],
        }
    )

    result = func(struct_col)
    assert len(result) == 2
    assert "\"value\": \"a\"" in result.iloc[0]
    assert "\"error\": \"err\"" in result.iloc[1]
