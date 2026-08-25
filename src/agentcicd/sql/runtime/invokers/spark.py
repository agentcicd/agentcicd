from __future__ import annotations

import os
import time

from agentcicd.sql.engine.spark_udf import (
    _collect_arrow_results,
    _coerce_result_for_output_schema,
    _dtype_to_spark,
    _to_pyarrow_columns,
    register_spark_udf,
)
from agentcicd.sql.engine.runtime_aliases import wrapped_runtime_alias
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.observability.fixture_traces import fixture_trace_context, start_fixture_trace
from agentcicd.sql.runtime.cell_envelopes import (
    _cell_value,
    _clean_cell,
    _errored_cell,
    _error_info,
    _is_err_payload,
    _merged_cell_errors,
)
from agentcicd.sql.runtime.controls import (
    _control_argument_indexes,
    _limiter_from_control_values,
    _runtime_limit_for_local_function,
    _runtime_pool_for_control_values,
    _udf_control_argument_indexes,
)
from agentcicd.sql.runtime.invokers.stub import StubRuntimeFunctionInvoker
from agentcicd.sql.runtime.package_distribution import SparkWorkerPackageDistributor
from agentcicd.sql.runtime.spark_types import _cell_return_type
from agentcicd.sql.udf_registry import get_registered_udf, load_builtin_udfs


class SparkUdfRuntimeInvoker:
    def __init__(self, package_distributor: SparkWorkerPackageDistributor | None = None) -> None:
        self._package_distributor = package_distributor or SparkWorkerPackageDistributor()

    def can_handle(self, definition: FunctionDefinitionIR) -> bool:
        metadata = getattr(definition, "metadata", {}) or {}
        base_url = str(metadata.get("base_url") or "").strip()
        invoke_path = str(metadata.get("invoke_path") or "").strip()
        if base_url and invoke_path:
            return False

        canonical_name = str(getattr(definition, "canonical_name", "") or "").strip()
        canonical_name_lower = canonical_name.lower()
        if getattr(definition, "kind", None) == "remote":
            return canonical_name_lower in {"aisystems.llm.chat", "aisystems.llm.messages"}

        if (
            str(metadata.get("execution_runtime") or "").strip().lower() == "function_runner"
            and _requires_function_runner_udfs()
        ):
            return False

        load_builtin_udfs()
        udf_cls = get_registered_udf(canonical_name)
        if udf_cls is None:
            return False

        if canonical_name_lower in {"aisystems.llm.chat", "aisystems.llm.messages"}:
            return True
        return self._definition_matches_registered_udf(definition, udf_cls)

    def register(self, spark_session, definition: FunctionDefinitionIR) -> str:
        load_builtin_udfs()
        self._package_distributor.ensure_distributed(spark_session)
        udf_cls = get_registered_udf(definition.canonical_name)
        if udf_cls is None:
            raise ValueError(f"Python UDF '{definition.canonical_name}' is not registered")
        register_spark_udf(
            spark_session,
            udf_cls,
            udf_name=StubRuntimeFunctionInvoker._runtime_alias(definition),
            control_arg_indexes=_control_argument_indexes(definition) | _udf_control_argument_indexes(udf_cls),
        )
        self.register_wrapped(spark_session, definition)
        return StubRuntimeFunctionInvoker._runtime_alias(definition)

    def register_wrapped(self, spark_session, definition: FunctionDefinitionIR) -> str:
        load_builtin_udfs()
        self._package_distributor.ensure_distributed(spark_session)
        udf_cls = get_registered_udf(definition.canonical_name)
        if udf_cls is None:
            raise ValueError(f"Python UDF '{definition.canonical_name}' is not registered")

        runtime_alias = StubRuntimeFunctionInvoker._runtime_alias(definition)
        wrapped_alias = wrapped_runtime_alias(runtime_alias)
        udf_instance = udf_cls()
        output_schema = udf_instance.output_schema()
        function_instance = udf_instance.function()()
        return_type = _cell_return_type(_dtype_to_spark(output_schema))
        control_indexes = _control_argument_indexes(definition) | _udf_control_argument_indexes(udf_cls)

        def _wrapped_spark_udf(*arg_cells):
            input_errors = _merged_cell_errors(arg_cells)
            if input_errors:
                trace = start_fixture_trace(
                    function_name=definition.canonical_name,
                    runtime_alias=runtime_alias,
                    backend="spark_udf",
                    execution_runtime=str((getattr(definition, "metadata", {}) or {}).get("execution_runtime") or "local_python"),
                )
                with fixture_trace_context(trace):
                    fixture_trace = trace.finish(status="skipped", duration_ms=0, error_message="input cell errors") if trace else None
                return _errored_cell(input_errors, fixture_trace=fixture_trace)
            try:
                values = tuple(_cell_value(arg_cell) for arg_cell in arg_cells)
                control_values = [values[index] for index in control_indexes if index < len(values)]
                limiter_key, max_in_flight = _limiter_from_control_values(control_values)
                data_values = tuple(
                    value for index, value in enumerate(values) if index not in control_indexes
                )
                setattr(function_instance, "_agentcicd_rate_limit_key", limiter_key)
                setattr(function_instance, "_agentcicd_rate_limit_max_in_flight", max_in_flight)
                start_time = time.perf_counter()
                payload_args = {f"arg_{index}": value for index, value in enumerate(data_values)}
                trace = start_fixture_trace(
                    function_name=definition.canonical_name,
                    runtime_alias=runtime_alias,
                    backend="spark_udf",
                    execution_runtime=str((getattr(definition, "metadata", {}) or {}).get("execution_runtime") or "local_python"),
                    payload_args=payload_args,
                    limiter_key=limiter_key,
                    max_in_flight=max_in_flight,
                )
                try:
                    with _runtime_pool_for_control_values(
                        control_values,
                        definition=definition,
                        payload_args=payload_args,
                    ):
                        with fixture_trace_context(trace):
                            with _runtime_limit_for_local_function(function_instance, limiter_key, max_in_flight):
                                result_iterator = function_instance(*_to_pyarrow_columns(data_values))
                                results = _collect_arrow_results(result_iterator)
                finally:
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                if len(results) != 1:
                    raise ValueError(
                        f"{runtime_alias} expected one result for a scalar row call but received {len(results)}"
                    )
                result = results[0]
                if _is_err_payload(result):
                    raise ValueError(str(result.get("message") or result.get("name") or "Python UDF returned an error"))
                fixture_trace = trace.finish(status="ok", duration_ms=latency_ms, result_preview=result) if trace else None
                return _clean_cell(
                    _coerce_result_for_output_schema(result, output_schema),
                    latency_ms=latency_ms,
                    fixture_trace=fixture_trace,
                )
            except Exception as exc:
                return _errored_cell(
                    [_error_info("AGENTCICD_RUNTIME_PYTHON_ERROR", str(exc), runtime_alias, cause=exc)],
                    latency_ms=locals().get("latency_ms"),
                    fixture_trace=trace.finish(
                        status="error",
                        duration_ms=int(locals().get("latency_ms") or 0),
                        error_code="AGENTCICD_RUNTIME_PYTHON_ERROR",
                        error_message=str(exc),
                        error_type=type(exc).__name__,
                    ) if "trace" in locals() and trace else None,
                )

        spark_session.udf.register(wrapped_alias, _wrapped_spark_udf, return_type)
        return wrapped_alias

    @staticmethod
    def _definition_matches_registered_udf(definition: FunctionDefinitionIR, udf_cls) -> bool:
        try:
            raw_udf_parameters = list(udf_cls().signature())
        except Exception:
            return False
        from agentcicd.sql.runtime.controls import RUNTIME_CONTROL_TYPES

        udf_control_indexes = {
            index
            for index, parameter in enumerate(raw_udf_parameters)
            if str(getattr(parameter, "type_sql", "") or "").strip().upper() in RUNTIME_CONTROL_TYPES
        }
        definition_parameters = [
            parameter
            for index, parameter in enumerate(list(getattr(definition, "parameters", []) or []))
            if index not in udf_control_indexes
            and str(getattr(parameter, "type_sql", "") or "").strip().upper() not in RUNTIME_CONTROL_TYPES
        ]
        if not definition_parameters:
            return True
        udf_parameters = [
            parameter
            for index, parameter in enumerate(raw_udf_parameters)
            if index not in udf_control_indexes
        ]
        if len(definition_parameters) != len(udf_parameters):
            return False
        return all(
            str(defined.name).strip() == str(expected.name).strip()
            for defined, expected in zip(definition_parameters, udf_parameters)
        )


def _requires_function_runner_udfs() -> bool:
    return os.getenv("AGENTCICD_REQUIRE_FUNCTION_RUNNER_UDFS", "").strip().lower() in {"1", "true", "yes", "on"}
