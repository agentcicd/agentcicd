from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from agentcicd.sql.engine.runtime_aliases import wrapped_runtime_alias
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.observability.fixture_traces import fixture_trace_context, start_fixture_trace
from agentcicd.sql.runtime.cache import (
    _runtime_cache_context,
    _runtime_cache_delete,
    _runtime_cache_enabled,
    _runtime_cache_get,
    _runtime_cache_key,
    _runtime_cache_put,
)
from agentcicd.sql.runtime.cell_envelopes import (
    _cell_value,
    _clean_cell,
    _errored_cell,
    _error_info,
    _merged_cell_errors,
)
from agentcicd.sql.runtime.controls import _control_argument_indexes, _limiter_from_control_values
from agentcicd.sql.runtime.invokers.stub import StubRuntimeFunctionInvoker
from agentcicd.sql.runtime.package_distribution import SparkWorkerPackageDistributor
from agentcicd.sql.runtime.payloads import _json_payload_value
from agentcicd.sql.runtime.spark_types import _cell_return_type, _coerce_remote_result, _definition_return_type
from agentcicd.sql.runtime.udf_compat.runtime_control import runtime_limiter


@dataclass
class LocalFixtureRuntimeInvoker:
    package_distributor: SparkWorkerPackageDistributor | None = None

    def __post_init__(self) -> None:
        if self.package_distributor is None:
            self.package_distributor = SparkWorkerPackageDistributor()

    def can_handle(self, definition: FunctionDefinitionIR) -> bool:
        metadata = _definition_metadata(definition)
        if str(metadata.get("execution_runtime") or "").strip().lower() != "function_runner":
            return False
        if _requires_runtime_state(definition, metadata):
            return False
        return _fixture_callable_for_name(str(definition.canonical_name)) is not None

    def register(self, spark_session, definition: FunctionDefinitionIR) -> str:
        runtime_alias = StubRuntimeFunctionInvoker._runtime_alias(definition)
        self.package_distributor.ensure_distributed(spark_session)
        local_callable = _required_fixture_callable(definition)
        param_names = [parameter.name for parameter in definition.parameters]
        return_type = _definition_return_type(definition)
        cache_enabled = _runtime_cache_enabled(_definition_metadata(definition))
        cache_context = _runtime_cache_context()
        control_param_indexes = _control_argument_indexes(definition)

        def _local_fixture_udf(*args: Any) -> Any:
            control_values = [args[index] for index in control_param_indexes if index < len(args)]
            limiter_key, max_in_flight = _limiter_from_control_values(control_values, fallback_key="default")
            payload_args = _payload_args(param_names, args, control_param_indexes)
            cache_key = _runtime_cache_key(definition, payload_args, cache_context=cache_context) if cache_enabled else None
            cached_value = _runtime_cache_get(cache_key)
            if cached_value is not None:
                return _coerce_remote_result(_cached_result(cached_value), return_type)
            with runtime_limiter(max_in_flight, key=limiter_key).acquire_blocking(permits=1):
                result = _call_fixture_callable(local_callable, payload_args)
            if cache_key is not None:
                _runtime_cache_put(cache_key, _cache_payload(result))
            return _coerce_remote_result(result, return_type)

        spark_session.udf.register(runtime_alias, _local_fixture_udf, return_type)
        self.register_wrapped(spark_session, definition)
        return runtime_alias

    def register_wrapped(self, spark_session, definition: FunctionDefinitionIR) -> str:
        runtime_alias = StubRuntimeFunctionInvoker._runtime_alias(definition)
        wrapped_alias = wrapped_runtime_alias(runtime_alias)
        self.package_distributor.ensure_distributed(spark_session)
        local_callable = _required_fixture_callable(definition)
        metadata = _definition_metadata(definition)
        param_names = [parameter.name for parameter in definition.parameters]
        value_return_type = _definition_return_type(definition)
        return_type = _cell_return_type(value_return_type)
        cache_enabled = _runtime_cache_enabled(metadata)
        cache_context = _runtime_cache_context()
        control_param_indexes = _control_argument_indexes(definition)

        def _wrapped_local_fixture_udf(*arg_cells: Any) -> Any:
            input_errors = _merged_cell_errors(arg_cells)
            if input_errors:
                trace = start_fixture_trace(
                    function_name=definition.canonical_name,
                    runtime_alias=runtime_alias,
                    backend="local_fixture",
                    execution_runtime=str(metadata.get("execution_runtime") or "function_runner"),
                )
                with fixture_trace_context(trace):
                    fixture_trace = trace.finish(status="skipped", duration_ms=0, error_message="input cell errors") if trace else None
                return _errored_cell(input_errors, fixture_trace=fixture_trace)
            cell_values = tuple(_cell_value(value) for value in arg_cells)
            control_values = [cell_values[index] for index in control_param_indexes if index < len(cell_values)]
            limiter_key, max_in_flight = _limiter_from_control_values(control_values, fallback_key="default")
            payload_args = _payload_args(param_names, cell_values, control_param_indexes)
            cache_key = _runtime_cache_key(definition, payload_args, cache_context=cache_context) if cache_enabled else None
            start_time = time.perf_counter()
            cached_value = _runtime_cache_get(cache_key)
            trace = start_fixture_trace(
                function_name=definition.canonical_name,
                runtime_alias=runtime_alias,
                backend="local_fixture",
                execution_runtime=str(metadata.get("execution_runtime") or "function_runner"),
                payload_args=payload_args,
                cache_hit=cached_value is not None,
                limiter_key=limiter_key,
                max_in_flight=max_in_flight,
            )
            try:
                with fixture_trace_context(trace):
                    if cached_value is None:
                        with runtime_limiter(max_in_flight, key=limiter_key).acquire_blocking(permits=1):
                            result_payload = _call_fixture_callable(local_callable, payload_args)
                        if cache_key is not None:
                            _runtime_cache_put(cache_key, _cache_payload(result_payload))
                    else:
                        result_payload = _cached_result(cached_value)
                    result = _coerce_remote_result(result_payload, value_return_type)
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    fixture_trace = trace.finish(
                        status="cache_hit" if cached_value is not None else "ok",
                        duration_ms=latency_ms,
                        result_preview=result_payload,
                    ) if trace else None
                    return _clean_cell(result, latency_ms=latency_ms, fixture_trace=fixture_trace)
            except Exception as exc:
                if cache_key is not None and cached_value is not None:
                    _runtime_cache_delete(cache_key)
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                return _errored_cell(
                    [_error_info("AGENTCICD_RUNTIME_LOCAL_FIXTURE_ERROR", str(exc), runtime_alias, cause=exc)],
                    latency_ms=latency_ms,
                    fixture_trace=trace.finish(
                        status="error",
                        duration_ms=latency_ms,
                        error_code="AGENTCICD_RUNTIME_LOCAL_FIXTURE_ERROR",
                        error_message=str(exc),
                        error_type=type(exc).__name__,
                    ) if trace else None,
                )

        spark_session.udf.register(wrapped_alias, _wrapped_local_fixture_udf, return_type)
        return wrapped_alias


def _definition_metadata(definition: FunctionDefinitionIR) -> dict[str, object]:
    raw_metadata = vars(definition).get("metadata", {}) or {}
    return dict(raw_metadata) if isinstance(raw_metadata, dict) else {}


def _requires_runtime_state(definition: FunctionDefinitionIR, metadata: dict[str, object]) -> bool:
    if str(metadata.get("pool_kind") or "").strip():
        return True
    pool = metadata.get("pool")
    if isinstance(pool, dict) and str(pool.get("kind") or "").strip():
        return True
    return str(definition.canonical_name).strip() == "envs.agent_harness.run_task"


def _required_fixture_callable(definition: FunctionDefinitionIR) -> Callable[..., Any]:
    local_callable = _fixture_callable_for_name(str(definition.canonical_name))
    if local_callable is None:
        raise ValueError(f"Local fixture callable unavailable for '{definition.canonical_name}'")
    return local_callable


def _fixture_callable_for_name(name: str) -> Callable[..., Any] | None:
    try:
        from agentcicd.fixtures.functions import load_builtin_udfs
    except ImportError:
        return None
    normalized = name.strip().lower()
    for udf_name, udf_cls in load_builtin_udfs().items():
        if str(udf_name).strip().lower() == normalized:
            return _build_builtin_udf_callable(udf_cls)
    return None


def _build_builtin_udf_callable(udf_cls: type[Any]) -> Callable[..., Any]:
    def _invoke(**kwargs: Any) -> Any:
        from agentcicd.fixtures.core.types import FType

        import pandas as pd
        import pyarrow as pa

        udf_instance = udf_cls()
        parameter_names = [parameter.name for parameter in udf_instance.signature()]
        values = [kwargs[name] for name in parameter_names if name in kwargs]
        function_instance = udf_instance.function()()
        function_type = udf_instance.ftype()
        if function_type in {FType.BATCH_FUNCTION, FType.ROW_EXPLODE_FUNCTION}:
            arrays = [pa.array([value]) for value in values]
            result_batches = list(function_instance(*arrays))
            if not result_batches:
                return None
            result_values = result_batches[0].to_pylist()
            if not result_values:
                return None
            return result_values[0]
        if function_type == FType.AGGREGATE_FUNCTION:
            series = [pd.Series([value]) for value in values]
            return function_instance(*series)
        raise ValueError(f"Unsupported AgentCICD fixture UDF function type: {function_type}")

    return _invoke


def _payload_args(param_names: list[str], values: tuple[Any, ...], control_param_indexes: set[int]) -> dict[str, object]:
    return {
        name: _json_payload_value(value)
        for index, (name, value) in enumerate(zip(param_names, values))
        if index not in control_param_indexes
    }


def _call_fixture_callable(local_callable: Callable[..., Any], payload_args: dict[str, object]) -> Any:
    result = local_callable(**payload_args)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _cache_payload(result: Any) -> str:
    return json.dumps({"result": result}, default=str)


def _cached_result(raw_payload: str) -> Any:
    payload = json.loads(raw_payload)
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload
