import pytest
from pydantic import BaseModel

from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import (
    Err,
    IntType,
    StringType,
    JsonEncodedPydanticType,
)


class ExampleModel(BaseModel):
    name: str
    count: int


def test_err_from_exception_includes_stacktrace():
    try:
        raise ValueError("bad input")
    except ValueError as exc:
        err = Err.from_exception(exc)

    assert err.name == "ValueError"
    assert err.message == "bad input"
    assert err.stacktrace is not None
    assert any("ValueError" in line for line in err.stacktrace)


def test_dtype_singleton_and_equality():
    first = IntType()
    second = IntType()
    other = StringType()

    assert first is second
    assert first == second
    assert first != other
    assert hash(first) == hash(second)


@pytest.mark.spark
def test_json_encoded_pydantic_type_roundtrip():
    dtype = JsonEncodedPydanticType(ExampleModel)
    model = ExampleModel(name="demo", count=3)

    internal = dtype.from_internal(model)
    restored = dtype.to_internal(internal)

    assert isinstance(restored, ExampleModel)
    assert restored == model


def test_retry_and_timeout_defaults():
    retry = RetryConfig()
    timeout = TimeoutConfig()

    assert retry.num_retries == 3
    assert retry.backoff_exponent == 1.1
    assert retry.max_wait_time == 600.0
    assert timeout.timeout == 60
    assert timeout.read == 60
    assert timeout.connect == 20
    assert timeout.write == 60
