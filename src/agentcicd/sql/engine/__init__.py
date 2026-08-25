"""IR engine public exports.

Keep this module lightweight so non-Spark consumers can import
``agentcicd.sql`` without pulling runtime Spark dependencies.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "EngineEntrypoint",
    "EngineRunConfig",
    "ExecutionBackend",
    "ExecutionEvent",
    "ExecutionPlanStep",
    "ExecutionReport",
    "SparkBackendPaths",
    "SparkExecutionBackend",
    "ValidationResult",
    "attach_plan_dependencies",
    "build_spark_session",
    "compile_execution_plan",
    "default_backend_paths",
    "execute_plan",
    "run_script_with_new_engine",
    "topologically_sort_plan",
    "validate_lowered_sql",
]


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:  # pragma: no cover - Python attribute protocol
        raise AttributeError(name) from exc
    module = import_module(module_name)
    return getattr(module, attr_name)


_EXPORTS = {
    "EngineEntrypoint": ("agentcicd.sql.engine.entrypoint", "EngineEntrypoint"),
    "EngineRunConfig": ("agentcicd.sql.engine.runner", "EngineRunConfig"),
    "ExecutionBackend": ("agentcicd.sql.engine.runtime", "ExecutionBackend"),
    "ExecutionEvent": ("agentcicd.sql.engine.runtime", "ExecutionEvent"),
    "ExecutionPlanStep": ("agentcicd.sql.engine.plan", "ExecutionPlanStep"),
    "ExecutionReport": ("agentcicd.sql.engine.runtime", "ExecutionReport"),
    "SparkBackendPaths": ("agentcicd.sql.engine.spark_backend", "SparkBackendPaths"),
    "SparkExecutionBackend": ("agentcicd.sql.engine.spark_backend", "SparkExecutionBackend"),
    "ValidationResult": ("agentcicd.sql.engine.validator", "ValidationResult"),
    "attach_plan_dependencies": ("agentcicd.sql.engine.plan", "attach_plan_dependencies"),
    "build_spark_session": ("agentcicd.sql.engine.spark_backend", "build_spark_session"),
    "compile_execution_plan": ("agentcicd.sql.engine.plan", "compile_execution_plan"),
    "default_backend_paths": ("agentcicd.sql.engine.spark_backend", "default_backend_paths"),
    "execute_plan": ("agentcicd.sql.engine.runtime", "execute_plan"),
    "run_script_with_new_engine": ("agentcicd.sql.engine.runner", "run_script_with_new_engine"),
    "topologically_sort_plan": ("agentcicd.sql.engine.plan", "topologically_sort_plan"),
    "validate_lowered_sql": ("agentcicd.sql.engine.validator", "validate_lowered_sql"),
}
