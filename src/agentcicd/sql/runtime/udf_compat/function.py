from __future__ import annotations

import abc
import asyncio
import inspect
import threading
from typing import Any, Iterator
from .types import Err, Json
from typing import List
from .runtime_control import runtime_limiter


class Function(abc.ABC):
    """Base class for all function types with lifecycle management."""

    def setup(self) -> None:
        """Setup method called before execute."""
        pass

    def teardown(self) -> None:
        """Teardown method called after execute."""
        pass

    @abc.abstractmethod
    def execute(self, *args: Any) -> Any:
        """Execute the function with given arguments."""
        pass

    def __call__(self, *args: Any) -> Any:
        """Execute with lifecycle management (setup/teardown)."""
        try:
            self.setup()
            return self.execute(*args)
        finally:
            self.teardown()


class BatchFunction(Function):
    """Function that processes entire batches at once."""

    def execute(self, *args: pa.Array) -> Iterator[pa.Array]:
        """Execute batch transformation on PyArrow Arrays."""
        pa = _require_pyarrow()
        py_args = [a.to_pylist() for a in args]
        transformed = self._transform(*py_args)
        yield pa.array(transformed)

    def _transform(self, *args: List[Json]) -> List[Json]:
        try:
            return self.transform(*args)
        except Exception as e:
            return [Err.from_exception(e).model_dump()] * len(args[0])

    @abc.abstractmethod
    def transform(self, *args: List[Json]) -> List[Json]:
        """Transform a batch of data. Must be implemented by subclasses."""
        pass


class RowFunction(Function):
    """Function that processes data row-by-row."""

    def execute(self, *args: pa.Array) -> Iterator[pa.Array]:
        """Execute row-wise transformation on PyArrow Arrays."""
        pa = _require_pyarrow()
        py_args = [a.to_pylist() for a in args]
        transformed = []
        for row_args in zip(*py_args):
            call_args = _resolve_row_call_args(self.transform, row_args)
            transformed.append(self.transform(*call_args))
        yield pa.array(transformed)

    def _transform(self, *args: Json) -> Json:
        try:
            return self.transform(*args)
        except Exception as e:
            return Err.from_exception(e).model_dump()

    @abc.abstractmethod
    def transform(self, *args: Json) -> Json:
        """Transform a single row. Must be implemented by subclasses."""
        pass


class AsyncRowFunction(Function):
    """Function that processes data row-by-row."""

    def execute(self, *args: pa.Array) -> Iterator[pa.Array]:
        """Execute row-wise transformation on PyArrow Arrays."""
        pa = _require_pyarrow()
        py_args = [a.to_pylist() for a in args]
        transformed = _run_coro_sync(self._execute_async(py_args))
        yield pa.array(transformed)

    async def _execute_async(self, py_args: List[List[Json]]) -> List[Json]:
        transformed_tasks = []
        limiter = runtime_limiter(
            self.__dict__.get("_agentcicd_rate_limit_max_in_flight"),
            key=str(self.__dict__.get("_agentcicd_rate_limit_key", "default")),
        )
        for row_args in zip(*py_args):
            call_args = _resolve_row_call_args(self.transform, row_args)
            transformed_tasks.append(self._transform_with_limit(limiter, *call_args))
        if not transformed_tasks:
            return []
        return await asyncio.gather(*transformed_tasks)

    async def _transform_with_limit(self, limiter: Any, *args: Json) -> Json:
        async with limiter.acquire(permits=1):
            return await self._transform(*args)

    async def _transform(self, *args: Json) -> Json:
        try:
            return await self.transform(*args)
        except Exception as e:
            return Err.from_exception(e).model_dump()


    @abc.abstractmethod
    async def transform(self, *args: Json) -> Json:
        """Transform a single row. Must be implemented by subclasses."""
        pass


def _run_coro_sync(coro: Any) -> Any:
    """Run a coroutine from sync code, including when already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - surfaced to caller
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "exc" in error:
        raise error["exc"]
    return result.get("value")


def _resolve_row_call_args(func: Any, provided_args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Resolve row-function args, injecting framework configs when requested."""
    from .retry import RetryConfig
    from .timeout import TimeoutConfig

    signature = inspect.signature(func)
    params = [
        p for p in signature.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]

    resolved = list(provided_args)
    for param in params[len(resolved):]:
        annotation = param.annotation
        if isinstance(annotation, str):
            annotation_name = annotation
        else:
            try:
                annotation_name = str(annotation.__name__)
            except AttributeError:
                annotation_name = ""
        if annotation is TimeoutConfig or annotation_name in {
            "TimeoutConfig",
            "agentcicd.sql.runtime.udf_compat.timeout.TimeoutConfig",
        }:
            resolved.append(TimeoutConfig())
        elif annotation is RetryConfig or annotation_name in {
            "RetryConfig",
            "agentcicd.sql.runtime.udf_compat.retry.RetryConfig",
        }:
            resolved.append(RetryConfig())
        elif param.default is not inspect.Parameter.empty:
            resolved.append(param.default)
        else:
            break

    return tuple(resolved)


class RowExplodeFunction(Function):
    """Function that explodes each row into multiple rows."""

    def execute(self, *args: pa.Array) -> Iterator[pa.Array]:
        """Execute row-wise explosion on PyArrow Arrays."""
        pa = _require_pyarrow()
        py_args = [a.to_pylist() for a in args]
        for row_args in zip(*py_args):
            yield pa.array(self._explode(*row_args))

    def _explode(self, *args: Json) -> List[Json]:
        try:
            return self.explode(*args)
        except Exception as e:
            return [Err.from_exception(e).model_dump()]

    @abc.abstractmethod
    def explode(self, *args: Json) -> List[Json]:
        """Explode a single row into multiple rows. Must be implemented by subclasses."""
        pass


class AggregateFunction(Function):
    """Function that aggregates data into a single result."""

    def execute(self, *args: pd.Series) -> Json:
        """Execute aggregation on pandas Series."""
        py_args = [list(a) for a in args]
        return self._aggregate(*py_args)

    def _aggregate(self, *args: List[Json]) -> Json:
        try:
            return self.aggregate(*args)
        except Exception as e:
            return Err.from_exception(e).model_dump()

    @abc.abstractmethod
    def aggregate(self, *args: List[Json]) -> Json:
        """Aggregate data into a single result. Must be implemented by subclasses."""
        pass


def _require_pyarrow() -> Any:
    try:
        import pyarrow as pa
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "pyarrow is required to execute agentcicd.sql UDF batches. "
            "Install agentcicd.sql[execution] in runtime environments."
        ) from exc
    return pa
