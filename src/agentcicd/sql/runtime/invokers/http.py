from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pyspark.sql import SparkSession
from pyspark.sql.functions import pandas_udf
from pyspark.sql.types import StructType

from agentcicd.sql.engine.spark_udf import _column_to_pylist, _has_vector_input
from agentcicd.sql.runtime.udf_compat.runtime_control import runtime_limiter
from agentcicd.sql.engine.runtime_aliases import wrapped_runtime_alias
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.observability.events import DiagnosticEvent
from agentcicd.sql.observability.fixture_traces import fixture_trace_context, start_fixture_trace
from agentcicd.sql.observability.sinks import DiagnosticSink
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
from agentcicd.sql.runtime.controls import (
    _control_argument_indexes,
    _http_timeout_seconds,
    _pool_fixture_id,
    _limiter_from_control_values,
    _pool_payload,
    _rate_limit_payload,
    _runtime_pool_for_control_values,
)
from agentcicd.sql.runtime.invocation_executor import RuntimeInvocation, RuntimeInvocationPoolExecutor
from agentcicd.sql.runtime.invokers.stub import StubRuntimeFunctionInvoker
from agentcicd.sql.runtime.package_distribution import SparkWorkerPackageDistributor
from agentcicd.sql.runtime.payloads import RemoteFunctionResponse, _json_payload_value, _read_http_error_body, read_http_error_payload
from agentcicd.sql.runtime.spark_types import _cell_return_type, _coerce_remote_result, _definition_return_type


@dataclass
class HttpRuntimeFunctionInvoker:
    timeout_seconds: int = 900
    package_distributor: SparkWorkerPackageDistributor | None = None
    diagnostic_sink: DiagnosticSink | None = None

    def __post_init__(self) -> None:
        if self.package_distributor is None:
            self.package_distributor = SparkWorkerPackageDistributor()

    def can_handle(self, definition: FunctionDefinitionIR) -> bool:
        metadata = getattr(definition, "metadata", {}) or {}
        base_url = str(metadata.get("base_url") or "").strip()
        invoke_path = str(metadata.get("invoke_path") or "").strip()
        return bool(base_url and invoke_path)

    def register(self, spark_session, definition: FunctionDefinitionIR) -> str:
        runtime_alias = StubRuntimeFunctionInvoker._runtime_alias(definition)
        self.package_distributor.ensure_distributed(spark_session)
        metadata = getattr(definition, "metadata", {}) or {}
        fixture_id = _pool_fixture_id(definition)
        base_url = str(metadata.get("base_url") or "").strip().rstrip("/")
        invoke_path = str(metadata.get("invoke_path") or "").strip()
        if not base_url or not invoke_path:
            raise ValueError("HTTP runtime function registration requires base_url and invoke_path")
        param_names = [parameter.name for parameter in getattr(definition, "parameters", [])]
        timeout_seconds = _http_timeout_seconds(metadata, default=self.timeout_seconds)
        return_type = _definition_return_type(definition)
        cache_enabled = _runtime_cache_enabled(metadata)
        cache_context = _runtime_cache_context()
        control_param_indexes = _control_argument_indexes(definition)

        def _remote_udf(*args):
            vector_rows = _vector_arg_rows(args)
            if vector_rows is not None:
                return _run_vector_invocations(
                    [_remote_invocation(row) for row in vector_rows],
                    max_concurrency=_vector_max_concurrency(vector_rows, control_param_indexes),
                    return_type=return_type,
                )
            return _run_single_invocation(_remote_invocation(args))

        def _remote_invocation(args: tuple[Any, ...]) -> RuntimeInvocation:
            control_values = [args[index] for index in control_param_indexes if index < len(args)]
            limiter_key, max_in_flight = _limiter_from_control_values(control_values, fallback_key="default")
            payload_args = {
                name: _json_payload_value(value)
                for index, (name, value) in enumerate(zip(param_names, args))
                if index not in control_param_indexes
            }
            cache_key = _runtime_cache_key(definition, payload_args, cache_context=cache_context) if cache_enabled else None
            raw_cached_payload = _runtime_cache_get(cache_key)
            if raw_cached_payload is not None:
                return RuntimeInvocation(
                    invoke=lambda _pool_lease: _coerce_raw_remote_payload(raw_cached_payload, runtime_alias, return_type),
                )

            def _invoke(pool_lease: Any) -> Any:
                target_base_url = (pool_lease.address if pool_lease and pool_lease.address else base_url).rstrip("/")
                effective_timeout_seconds = _http_timeout_seconds_for_control_values(
                    control_values,
                    default=timeout_seconds,
                )
                request_payload = {"args": payload_args}
                serialized_lease = _serialized_pool_lease(pool_lease)
                if serialized_lease:
                    request_payload["lease"] = serialized_lease
                request = Request(
                    f"{target_base_url}{invoke_path}",
                    data=json.dumps(request_payload).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=effective_timeout_seconds) as response:  # noqa: S310
                    raw_payload = response.read().decode("utf-8") or "{}"
                if cache_key is not None:
                    _runtime_cache_put(cache_key, raw_payload)
                return _coerce_raw_remote_payload(raw_payload, runtime_alias, return_type)

            def _on_error(exc: BaseException) -> Any:
                effective_timeout_seconds = _http_timeout_seconds_for_control_values(
                    control_values,
                    default=timeout_seconds,
                )
                if isinstance(exc, HTTPError):
                    error_body = _read_http_error_body(exc)
                    detail = f": {error_body}" if error_body else ""
                    raise RuntimeError(
                        f"Remote function '{runtime_alias}' failed with HTTP {exc.code}{detail}"
                    ) from exc
                if isinstance(exc, URLError):
                    raise RuntimeError(f"Remote function '{runtime_alias}' could not be reached") from exc
                if isinstance(exc, TimeoutError):
                    raise RuntimeError(
                        f"Remote function '{runtime_alias}' timed out after {effective_timeout_seconds} seconds"
                    ) from exc
                raise exc

            return RuntimeInvocation(
                acquire_pool=lambda: _runtime_pool_for_control_values(
                    control_values,
                    fallback_address=base_url,
                    definition=definition,
                    payload_args=payload_args,
                ),
                acquire_limiter=lambda: runtime_limiter(max_in_flight, key=limiter_key).acquire_blocking(permits=1),
                invoke=_invoke,
                on_error=_on_error,
            )

        _register_arrow_aware_udf(spark_session, runtime_alias, _remote_udf, return_type)
        self.register_wrapped(spark_session, definition)
        return runtime_alias

    def register_wrapped(self, spark_session, definition: FunctionDefinitionIR) -> str:
        runtime_alias = StubRuntimeFunctionInvoker._runtime_alias(definition)
        wrapped_alias = wrapped_runtime_alias(runtime_alias)
        self.package_distributor.ensure_distributed(spark_session)
        metadata = getattr(definition, "metadata", {}) or {}
        fixture_id = _pool_fixture_id(definition)
        base_url = str(metadata.get("base_url") or "").strip().rstrip("/")
        invoke_path = str(metadata.get("invoke_path") or "").strip()
        if not base_url or not invoke_path:
            raise ValueError("HTTP runtime function registration requires base_url and invoke_path")
        param_names = [parameter.name for parameter in getattr(definition, "parameters", [])]
        timeout_seconds = _http_timeout_seconds(metadata, default=self.timeout_seconds)
        value_return_type = _definition_return_type(definition)
        return_type = _cell_return_type(value_return_type)
        cache_enabled = _runtime_cache_enabled(metadata)
        cache_context = _runtime_cache_context()
        control_param_indexes = _control_argument_indexes(definition)

        def _wrapped_remote_udf(*arg_cells):
            vector_rows = _vector_arg_rows(arg_cells)
            if vector_rows is not None:
                return _run_vector_invocations(
                    [_wrapped_invocation(row) for row in vector_rows],
                    max_concurrency=_vector_max_concurrency(vector_rows, control_param_indexes),
                    return_type=return_type,
                )
            return _run_single_invocation(_wrapped_invocation(arg_cells))

        def _wrapped_invocation(arg_cells: tuple[Any, ...]) -> RuntimeInvocation:
            input_errors = _merged_cell_errors(arg_cells)
            if input_errors:
                trace = start_fixture_trace(
                    function_name=definition.canonical_name,
                    runtime_alias=runtime_alias,
                    backend="http",
                    execution_runtime=str(metadata.get("execution_runtime") or "function_runner"),
                )

                def _skipped(_pool_lease: Any) -> Any:
                    with fixture_trace_context(trace):
                        fixture_trace = trace.finish(status="skipped", duration_ms=0, error_message="input cell errors") if trace else None
                    return _errored_cell(input_errors, fixture_trace=fixture_trace)

                return RuntimeInvocation(invoke=_skipped)

            cell_values = tuple(_cell_value(value) for value in arg_cells)
            control_values = [cell_values[index] for index in control_param_indexes if index < len(cell_values)]
            limiter_key, max_in_flight = _limiter_from_control_values(control_values, fallback_key="default")
            payload_args = {
                name: _json_payload_value(value)
                for index, (name, value) in enumerate(zip(param_names, cell_values))
                if index not in control_param_indexes
            }
            cache_key = _runtime_cache_key(definition, payload_args, cache_context=cache_context) if cache_enabled else None
            start_time = time.perf_counter()
            raw_cached_payload = _runtime_cache_get(cache_key)
            if raw_cached_payload is not None:
                trace = start_fixture_trace(
                    function_name=definition.canonical_name,
                    runtime_alias=runtime_alias,
                    backend="http",
                    execution_runtime=str(metadata.get("execution_runtime") or "function_runner"),
                    payload_args=payload_args,
                    cache_hit=True,
                    limiter_key=limiter_key,
                    max_in_flight=max_in_flight,
                    fixture_id=fixture_id or None,
                )

                try:
                    with fixture_trace_context(trace):
                        response_payload = RemoteFunctionResponse.from_json(raw_cached_payload, runtime_alias=runtime_alias)
                        latency_ms = int((time.perf_counter() - start_time) * 1000)
                        result = _coerce_remote_result(response_payload.result, value_return_type)
                        fixture_trace = trace.finish(
                            status="cache_hit",
                            duration_ms=latency_ms,
                            result_preview=response_payload.result,
                        ) if trace else None
                        cached_cell = _clean_cell(
                            result,
                            latency_ms=latency_ms,
                            fixture_trace=fixture_trace,
                        )
                        return RuntimeInvocation(invoke=lambda _pool_lease: cached_cell)
                except Exception:
                    _runtime_cache_delete(cache_key)

            trace = start_fixture_trace(
                function_name=definition.canonical_name,
                runtime_alias=runtime_alias,
                backend="http",
                execution_runtime=str(metadata.get("execution_runtime") or "function_runner"),
                payload_args=payload_args,
                limiter_key=limiter_key,
                max_in_flight=max_in_flight,
                fixture_id=fixture_id or None,
            )

            def _on_pool_acquired(pool_lease: Any) -> None:
                if trace and pool_lease:
                    trace.pool_name = pool_lease.pool_name
                    trace.pool_kind = pool_lease.pool_kind
                    trace.pool_node_id = pool_lease.node_id
                    trace.fixture_id = pool_lease.fixture_id or fixture_id or trace.fixture_id

            def _invoke(pool_lease: Any) -> Any:
                with fixture_trace_context(trace):
                    target_base_url = (pool_lease.address if pool_lease and pool_lease.address else base_url).rstrip("/")
                    effective_timeout_seconds = _http_timeout_seconds_for_control_values(
                        control_values,
                        default=timeout_seconds,
                    )
                    request_payload = {"args": payload_args}
                    serialized_lease = _serialized_pool_lease(pool_lease)
                    if serialized_lease:
                        request_payload["lease"] = serialized_lease
                    if trace:
                        request_payload["trace"] = trace.request_context()
                    request = Request(
                        f"{target_base_url}{invoke_path}",
                        data=json.dumps(request_payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urlopen(request, timeout=effective_timeout_seconds) as response:  # noqa: S310
                        raw_payload = response.read().decode("utf-8") or "{}"
                    response_payload = RemoteFunctionResponse.from_json(raw_payload, runtime_alias=runtime_alias)
                    if cache_key is not None:
                        _runtime_cache_put(cache_key, raw_payload)
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    result = _coerce_remote_result(response_payload.result, value_return_type)
                    if trace:
                        trace.extend_records(response_payload.trace_records)
                    fixture_trace = response_payload.trace_summary or trace.finish(
                        status="ok",
                        duration_ms=latency_ms,
                        result_preview=response_payload.result,
                    ) if trace else None
                    return _clean_cell(
                        result,
                        latency_ms=latency_ms,
                        fixture_trace=fixture_trace,
                    )

            def _on_error(exc: BaseException) -> Any:
                effective_timeout_seconds = _http_timeout_seconds_for_control_values(
                    control_values,
                    default=timeout_seconds,
                )
                if isinstance(exc, HTTPError):
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    error_payload = read_http_error_payload(exc)
                    error_body = error_payload.text
                    message = f"Remote function '{runtime_alias}' failed with HTTP {exc.code}"
                    if error_body:
                        message = f"{message}: {error_body}"
                    if trace:
                        trace.extend_records(error_payload.trace_records)
                    self._emit_runtime_http_diagnostic(
                        runtime_alias=runtime_alias,
                        code="AGENTCICD_RUNTIME_HTTP_ERROR",
                        message=message,
                        details={
                            "http_status": exc.code,
                            "remote_detail": error_body,
                            "failure_kind": "remote_http",
                        },
                    )
                    return _errored_cell(
                        [_error_info("AGENTCICD_RUNTIME_HTTP_ERROR", message, runtime_alias, cause=exc)],
                        latency_ms=latency_ms,
                        fixture_trace=error_payload.trace_summary or trace.finish(
                            status="error",
                            duration_ms=latency_ms,
                            error_code="AGENTCICD_RUNTIME_HTTP_ERROR",
                            error_message=message,
                            error_type=type(exc).__name__,
                            http_status=exc.code,
                        ) if trace else None,
                    )
                if isinstance(exc, URLError):
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    message = f"Remote function '{runtime_alias}' could not be reached"
                    self._emit_runtime_http_diagnostic(
                        runtime_alias=runtime_alias,
                        code="AGENTCICD_RUNTIME_NETWORK_ERROR",
                        message=message,
                        details={"failure_kind": "network"},
                    )
                    return _errored_cell(
                        [_error_info("AGENTCICD_RUNTIME_NETWORK_ERROR", message, runtime_alias, cause=exc)],
                        latency_ms=latency_ms,
                        fixture_trace=trace.finish(
                            status="error",
                            duration_ms=latency_ms,
                            error_code="AGENTCICD_RUNTIME_NETWORK_ERROR",
                            error_message=message,
                            error_type=type(exc).__name__,
                        ) if trace else None,
                    )
                if isinstance(exc, TimeoutError):
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    message = f"Remote function '{runtime_alias}' timed out after {effective_timeout_seconds} seconds"
                    self._emit_runtime_http_diagnostic(
                        runtime_alias=runtime_alias,
                        code="AGENTCICD_RUNTIME_TIMEOUT",
                        message=message,
                        details={"failure_kind": "timeout", "timeout_seconds": effective_timeout_seconds},
                    )
                    return _errored_cell(
                        [_error_info("AGENTCICD_RUNTIME_TIMEOUT", message, runtime_alias, cause=exc)],
                        latency_ms=latency_ms,
                        fixture_trace=trace.finish(
                            status="error",
                            duration_ms=latency_ms,
                            error_code="AGENTCICD_RUNTIME_TIMEOUT",
                            error_message=message,
                            error_type=type(exc).__name__,
                        ) if trace else None,
                    )
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                self._emit_runtime_http_diagnostic(
                    runtime_alias=runtime_alias,
                    code="AGENTCICD_RUNTIME_REMOTE_ERROR",
                    message=str(exc),
                    details={"failure_kind": "payload_or_schema", "exception_type": type(exc).__name__},
                )
                return _errored_cell(
                    [_error_info("AGENTCICD_RUNTIME_REMOTE_ERROR", str(exc), runtime_alias, cause=exc)],
                    latency_ms=latency_ms,
                    fixture_trace=trace.finish(
                        status="error",
                        duration_ms=latency_ms,
                        error_code="AGENTCICD_RUNTIME_REMOTE_ERROR",
                        error_message=str(exc),
                        error_type=type(exc).__name__,
                    ) if trace else None,
                )

            return RuntimeInvocation(
                acquire_pool=lambda: _runtime_pool_for_control_values(
                    control_values,
                    fallback_address=base_url,
                    definition=definition,
                    payload_args=payload_args,
                ),
                acquire_limiter=lambda: runtime_limiter(max_in_flight, key=limiter_key).acquire_blocking(permits=1),
                invoke=_invoke,
                on_pool_acquired=_on_pool_acquired,
                on_error=_on_error,
            )

        _register_arrow_aware_udf(spark_session, wrapped_alias, _wrapped_remote_udf, return_type)
        return wrapped_alias

    def _emit_runtime_http_diagnostic(
        self,
        *,
        runtime_alias: str,
        code: str,
        message: str,
        details: dict[str, object],
    ) -> None:
        if self.diagnostic_sink is None:
            return
        payload = {
            "runtime_alias": runtime_alias,
            "error_code": code,
            "error_message": message,
            **details,
        }
        self.diagnostic_sink.emit(
            DiagnosticEvent(
                event="runtime_call.failed",
                severity="error",
                stage_name=runtime_alias,
                stage_kind="runtime_function",
                details=payload,
            ).to_dict()
        )


def _serialized_pool_lease(pool_lease: object | None) -> dict[str, object] | None:
    if pool_lease is None or not is_dataclass(pool_lease):
        return None
    payload = {
        key: value
        for key, value in asdict(pool_lease).items()
        if value is not None and value != ""
    }
    payload.setdefault("generation", 1)
    return payload or None


def _http_timeout_seconds_for_control_values(values: list[object], *, default: int) -> int:
    for value in values:
        pool = _pool_payload(value)
        if pool is None:
            continue
        config = pool.get("config") if isinstance(pool.get("config"), dict) else {}
        timeout = _coerce_positive_int(
            pool.get("http_timeout_seconds")
            or config.get("http_timeout_seconds")
            or pool.get("timeout_seconds")
            or config.get("timeout_seconds")
        )
        if timeout is not None:
            return timeout
    return int(default)


def _coerce_positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    if parsed < 1:
        return None
    return parsed


def _register_arrow_aware_udf(spark_session: Any, name: str, func: Callable[..., Any], return_type: Any) -> None:
    if isinstance(spark_session, SparkSession):
        spark_session.udf.register(name, pandas_udf(func, return_type))
        return
    spark_session.udf.register(name, func, return_type)


def _vector_arg_rows(args: tuple[Any, ...]) -> list[tuple[Any, ...]] | None:
    if not _has_vector_input(args):
        return None
    columns = [_column_to_pylist(arg) for arg in args]
    row_count = len(columns[0]) if columns else 0
    if any(len(column) != row_count for column in columns):
        lengths = ", ".join(str(len(column)) for column in columns)
        raise ValueError(f"HTTP runtime UDF received vector columns with mismatched lengths: {lengths}")
    return [tuple(column[row_index] for column in columns) for row_index in range(row_count)]


def _run_single_invocation(invocation: RuntimeInvocation) -> Any:
    return RuntimeInvocationPoolExecutor(max_concurrency=1).run([invocation])[0]


def _run_vector_invocations(
    invocations: list[RuntimeInvocation],
    *,
    max_concurrency: int,
    return_type: Any | None = None,
) -> Any:
    import pandas as pd

    if not invocations:
        if isinstance(return_type, StructType):
            return _vector_result_frame([], return_type)
        return pd.Series([], dtype=object)
    results = RuntimeInvocationPoolExecutor(max_concurrency=max_concurrency).run(invocations)
    if isinstance(return_type, StructType):
        return _vector_result_frame(results, return_type)
    return pd.Series(results)


def _vector_result_frame(results: list[Any], return_type: Any | None) -> Any:
    import pandas as pd

    if not isinstance(return_type, StructType):
        return pd.Series(results, dtype=object)
    field_names = [field.name for field in return_type.fields]
    rows = [
        {field_name: result.get(field_name) for field_name in field_names} if isinstance(result, dict)
        else {field_names[0]: result} if len(field_names) == 1
        else {field_name: None for field_name in field_names}
        for result in results
    ]
    return pd.DataFrame(rows, columns=field_names)


def _coerce_raw_remote_payload(raw_payload: str, runtime_alias: str, return_type: Any) -> Any:
    response = RemoteFunctionResponse.from_json(raw_payload, runtime_alias=runtime_alias)
    return _coerce_remote_result(response.result, return_type)


def _vector_max_concurrency(rows: list[tuple[Any, ...]], control_param_indexes: set[int]) -> int:
    if not rows:
        return 1
    configured = _configured_vector_concurrency(rows, control_param_indexes)
    raw_cap = os.getenv("AGENTCICD_HTTP_UDF_VECTOR_MAX_CONCURRENCY") or os.getenv("AGENTCICD_HTTP_UDF_VECTOR_MAX_WORKERS", "32")
    try:
        cap = int(raw_cap)
    except (TypeError, ValueError):
        cap = 32
    requested = configured if configured is not None else cap
    return max(1, min(len(rows), max(1, requested), max(1, cap)))


def _configured_vector_concurrency(rows: list[tuple[Any, ...]], control_param_indexes: set[int]) -> int | None:
    candidates: list[int] = []
    for row in rows:
        for index in control_param_indexes:
            if index >= len(row):
                continue
            rate_limit = _rate_limit_payload(row[index])
            if rate_limit is not None and rate_limit.get("max_in_flight") is not None:
                candidates.append(int(rate_limit["max_in_flight"]))
                continue
            pool = _pool_payload(row[index])
            if pool is None:
                continue
            config = pool.get("config") if isinstance(pool.get("config"), dict) else {}
            pool_kind = str(pool.get("kind") or config.get("kind") or "").strip().lower()
            if pool_kind == "service":
                continue
            for key in ("max_instances", "max_workers", "capacity"):
                raw_value = pool.get(key) if pool.get(key) is not None else config.get(key)
                if raw_value is None:
                    continue
                try:
                    candidates.append(int(raw_value))
                except (TypeError, ValueError):
                    continue
                break
    if not candidates:
        return None
    return max(1, min(candidates))
