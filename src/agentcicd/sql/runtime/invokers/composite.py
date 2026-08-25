from __future__ import annotations

from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.runtime.invokers.http import HttpRuntimeFunctionInvoker
from agentcicd.sql.runtime.invokers.local_fixture import LocalFixtureRuntimeInvoker
from agentcicd.sql.runtime.invokers.spark import SparkUdfRuntimeInvoker
from agentcicd.sql.runtime.invokers.stub import StubRuntimeFunctionInvoker
from agentcicd.sql.runtime.package_distribution import SparkWorkerPackageDistributor


class CompositeRuntimeFunctionInvoker:
    def __init__(self, *invokers) -> None:
        package_distributor = SparkWorkerPackageDistributor()
        self._invokers = list(invokers) or [
            SparkUdfRuntimeInvoker(package_distributor),
            LocalFixtureRuntimeInvoker(package_distributor),
            HttpRuntimeFunctionInvoker(package_distributor=package_distributor),
            StubRuntimeFunctionInvoker(package_distributor),
        ]

    def register(self, spark_session, definition: FunctionDefinitionIR) -> str:
        for invoker in self._invokers:
            can_handle = getattr(invoker, "can_handle", None)
            if callable(can_handle) and not can_handle(definition):
                continue
            return invoker.register(spark_session, definition)
        raise ValueError(f"No runtime function invoker could handle '{definition.canonical_name}'")
