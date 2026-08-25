from __future__ import annotations

import json
from typing import Any

from pyspark.sql.types import StringType

from agentcicd.sql.engine.runtime_aliases import wrapped_runtime_alias
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.runtime.cell_envelopes import (
    _cell_value,
    _clean_cell,
    _errored_cell,
    _error_info,
    _merged_cell_errors,
)
from agentcicd.sql.runtime.package_distribution import SparkWorkerPackageDistributor
from agentcicd.sql.runtime.payloads import _render_stub_value
from agentcicd.sql.runtime.spark_types import _cell_return_type, _definition_return_type


class StubRuntimeFunctionInvoker:
    def __init__(self, package_distributor: Any | None = None) -> None:
        self._package_distributor = package_distributor

    def register(self, spark_session, definition: FunctionDefinitionIR) -> str:
        if self._package_distributor is None:
            self._package_distributor = SparkWorkerPackageDistributor()
        self._package_distributor.ensure_distributed(spark_session)
        runtime_alias = self._runtime_alias(definition)

        def _runtime_stub(*args):
            def _render(value: Any) -> str:
                if value is None:
                    return "null"
                if isinstance(value, bool):
                    return "true" if value else "false"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return str(value)
                if isinstance(value, (dict, list, tuple)):
                    return json.dumps(value, sort_keys=True)
                return str(value)

            rendered_args = ", ".join(_render(arg) for arg in args)
            return f"{runtime_alias}({rendered_args})"

        spark_session.udf.register(runtime_alias, _runtime_stub, StringType())
        self.register_wrapped(spark_session, definition)
        return runtime_alias

    def register_wrapped(self, spark_session, definition: FunctionDefinitionIR) -> str:
        if self._package_distributor is None:
            self._package_distributor = SparkWorkerPackageDistributor()
        self._package_distributor.ensure_distributed(spark_session)
        runtime_alias = self._runtime_alias(definition)
        wrapped_alias = wrapped_runtime_alias(runtime_alias)
        return_type = _cell_return_type(_definition_return_type(definition))

        def _wrapped_stub(*arg_cells):
            input_errors = _merged_cell_errors(arg_cells)
            if input_errors:
                return _errored_cell(input_errors)
            try:
                rendered_args = [_cell_value(arg_cell) for arg_cell in arg_cells]
                rendered = ", ".join(_render_stub_value(arg) for arg in rendered_args)
                return _clean_cell(f"{runtime_alias}({rendered})")
            except Exception as exc:
                return _errored_cell(
                    [_error_info("AGENTCICD_RUNTIME_STUB_ERROR", str(exc), runtime_alias, cause=exc)],
                )

        spark_session.udf.register(wrapped_alias, _wrapped_stub, return_type)
        return wrapped_alias

    @staticmethod
    def _runtime_alias(definition: FunctionDefinitionIR) -> str:
        runtime_alias = str(definition.runtime_alias or definition.canonical_name.replace(".", "_")).strip()
        if not runtime_alias:
            raise ValueError(f"Runtime function '{definition.canonical_name}' is missing a runtime alias")
        return runtime_alias
