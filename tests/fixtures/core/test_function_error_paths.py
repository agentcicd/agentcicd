import asyncio

import pyarrow as pa

from agentcicd.fixtures.core.function import (
    AggregateFunction,
    AsyncRowFunction,
    BatchFunction,
    RowExplodeFunction,
    RowFunction,
)
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import Err, Json, FType, StringType


class FailingBatchFunction(BatchFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def transform(self, *args):
        raise RuntimeError("batch failed")


class FailingRowFunction(RowFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def transform(self, *args: Json) -> Json:
        raise ValueError("row failed")


class FailingAsyncRowFunction(AsyncRowFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    async def transform(self, *args: Json) -> Json:
        raise ValueError("async row failed")


class UpperAsyncRowFunction(AsyncRowFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    async def transform(self, value: Json) -> Json:
        if isinstance(value, str):
            return value.upper()
        return ""


class ConfigAwareAsyncRowFunction(AsyncRowFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    async def transform(self, value: Json, timeout: TimeoutConfig, retry: RetryConfig) -> Json:
        if not isinstance(value, str):
            return ""
        return f"{value}:{int(timeout.timeout or 0)}:{int(retry.num_retries or 0)}"


class FailingRowExplodeFunction(RowExplodeFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.ROW_EXPLODE_FUNCTION

    def explode(self, *args: Json):
        raise ValueError("explode failed")


class FailingAggregateFunction(AggregateFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.AGGREGATE_FUNCTION

    def aggregate(self, *args):
        raise ValueError("aggregate failed")


def test_batch_function_error_path():
    func = FailingBatchFunction()
    result = func._transform(["a", "b"])

    assert isinstance(result, list)
    assert len(result) == 2
    for item in result:
        assert isinstance(item, dict)
        assert item["name"] == "RuntimeError"
        assert item["message"] == "batch failed"


def test_row_function_error_path():
    func = FailingRowFunction()
    result = func._transform("x")

    assert isinstance(result, dict)
    assert result["name"] == "ValueError"
    assert result["message"] == "row failed"


def test_async_row_function_error_path():
    func = FailingAsyncRowFunction()
    result = asyncio.run(func._transform("x"))

    assert isinstance(result, dict)
    assert result["name"] == "ValueError"
    assert result["message"] == "async row failed"


def test_async_row_function_execute_path():
    func = UpperAsyncRowFunction()
    result = list(func.execute(pa.array(["a", "b"])))

    assert len(result) == 1
    assert result[0].to_pylist() == ["A", "B"]


def test_async_row_function_execute_inside_running_loop():
    func = UpperAsyncRowFunction()

    async def _call_execute():
        return list(func.execute(pa.array(["z"])))

    result = asyncio.run(_call_execute())
    assert result[0].to_pylist() == ["Z"]


def test_async_row_function_injects_timeout_and_retry():
    func = ConfigAwareAsyncRowFunction()
    result = list(func.execute(pa.array(["ok"])))
    assert result[0].to_pylist() == ["ok:60:3"]


def test_row_explode_function_error_path():
    func = FailingRowExplodeFunction()
    result = func._explode("x")

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], dict)
    assert result[0]["name"] == "ValueError"
    assert result[0]["message"] == "explode failed"


def test_aggregate_function_error_path():
    func = FailingAggregateFunction()
    result = func._aggregate(["x", "y"])

    assert isinstance(result, dict)
    assert result["name"] == "ValueError"
    assert result["message"] == "aggregate failed"
