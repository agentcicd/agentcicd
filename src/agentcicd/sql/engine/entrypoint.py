from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Mapping, Optional

from agentcicd.sql.contracts import ProgressCallbackEvent, RegisteredRuntimeFunction
from agentcicd.sql.engine.plan import (
    ExecutionPlanStep,
    _statement_input_variant_columns,
    compile_execution_plan,
)
from agentcicd.sql.engine.runtime import ExecutionBackend, ExecutionReport, execute_plan, execute_plan_dag
from agentcicd.sql.engine.validator import ValidationResult, validate_lowered_sql
from agentcicd.sql.injections import apply_recipe_injections
from agentcicd.sql.ir.functions import RegisteredFunctionSpec, coerce_registered_function_specs
from agentcicd.sql.ir.statements import BatchTableStmt, DeclareInputStmt, StatementIR, StreamTableStmt
from agentcicd.sql.lowering.segment_lowering import (
    infer_statement_variant_outputs,
    lower_declare_input_cell_sql,
    lower_statement_cells_sql,
    lower_statement_sql,
)
from agentcicd.sql.semantics.dependency_graph import (
    DependencyGraph,
    build_dependency_graph,
    ensure_acyclic_dependency_graph,
    validate_relation_dependencies,
)
from agentcicd.sql.semantics.registry import FunctionRegistry, build_function_registry
from agentcicd.sql.semantics.resolver import resolve_script
from agentcicd.sql.semantics.validation import validate_script
from agentcicd.sql.surface.top_level_parser import TopLevelParser
from agentcicd.sql.wrapped_validation import validate_wrapped_statements


@dataclass
class EngineEntrypoint:
    script: str
    registered_functions: Optional[list[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]]] = None
    input_values: Optional[Mapping[str, str]] = None
    external_tables: Optional[Iterable[str]] = None

    def __post_init__(self) -> None:
        self.registered_functions = coerce_registered_function_specs(self.registered_functions or [])

    def parse(self, *, apply_defaults: bool = False) -> List[StatementIR]:
        statements = TopLevelParser(self.script).parse()
        return apply_recipe_injections(statements) if apply_defaults else statements

    def registry(self, *, apply_defaults: bool = False) -> FunctionRegistry:
        statements = self.parse(apply_defaults=apply_defaults)
        return build_function_registry(statements, self.registered_functions or [])

    def resolve(self, *, apply_defaults: bool = False) -> List[StatementIR]:
        statements, _ = self.resolve_with_registry(apply_defaults=apply_defaults)
        return statements

    def resolve_with_registry(self, *, apply_defaults: bool = False) -> tuple[List[StatementIR], FunctionRegistry]:
        statements = self.parse()
        registry = build_function_registry(statements, self.registered_functions or [])
        if apply_defaults:
            validate_script(statements)
            statements = apply_recipe_injections(statements, registry=registry)
            registry = build_function_registry(statements, self.registered_functions or [])
        validate_script(statements, registry=registry)
        return resolve_script(statements, registry), registry

    def lower_script(self, *, include_cells: bool = False) -> list[str]:
        statements = self.resolve()
        if include_cells:
            validate_wrapped_statements(statements)
        registry = self.registry()
        graph = build_dependency_graph(statements, registry=registry)
        table_variant_outputs: dict[str, set[str]] = {}
        declared_input_names = _declared_input_names(statements)
        lowered: list[str] = []
        for statement in statements:
            if isinstance(statement, DeclareInputStmt):
                lowered.append(lower_declare_input_cell_sql(statement) if include_cells else lower_statement_sql(statement, registry))
            elif isinstance(statement, (BatchTableStmt, StreamTableStmt)):
                input_variant_columns = _statement_input_variant_columns(
                    statement.name,
                    graph,
                    table_variant_outputs,
                    statement=statement,
                )
                lowered.append(
                    lower_statement_cells_sql(
                        statement,
                        registry,
                        variant_columns=input_variant_columns,
                        non_cell_columns=set() if include_cells else declared_input_names,
                    )
                    if include_cells
                    else lower_statement_sql(
                        statement,
                        registry,
                        variant_columns=input_variant_columns,
                    )
                )
                table_variant_outputs[statement.name.lower()] = infer_statement_variant_outputs(
                    statement,
                    registry,
                    variant_columns=input_variant_columns,
                )
        return lowered

    def validate_lowered_script(
        self,
        *,
        include_cells: bool = False,
        spark_session=None,
    ) -> list[ValidationResult]:
        return [
            validate_lowered_sql(sql, spark_session=spark_session)
            for sql in self.lower_script(include_cells=include_cells)
        ]

    def dependency_graph(self) -> DependencyGraph:
        statements = self.resolve()
        return build_dependency_graph(statements, registry=self.registry())

    def compile_plan(self, *, include_cells: bool = False, render_sql: bool = True) -> list[ExecutionPlanStep]:
        statements = self.resolve()
        return self.compile_resolved_plan(statements, include_cells=include_cells, render_sql=render_sql)

    def compile_resolved_plan(
        self,
        statements: list[StatementIR],
        *,
        registry: FunctionRegistry | None = None,
        include_cells: bool = False,
        render_sql: bool = True,
    ) -> list[ExecutionPlanStep]:
        if include_cells:
            validate_wrapped_statements(statements)
        registry = registry or build_function_registry(statements, self.registered_functions or [])
        graph = build_dependency_graph(statements, registry=registry)
        validate_relation_dependencies(statements, graph, external_tables=self.external_tables)
        ensure_acyclic_dependency_graph(graph)
        return compile_execution_plan(
            statements,
            registry,
            include_cells=include_cells,
            dependency_graph=graph,
            input_values=self.input_values or {},
            render_sql=render_sql,
        )

    def execute(
        self,
        backend: ExecutionBackend,
        *,
        include_cells: bool = False,
        progress_callback: Callable[[ProgressCallbackEvent], None] | None = None,
        max_parallel_stages: int = 1,
    ) -> ExecutionReport:
        if max_parallel_stages > 1:
            return execute_plan_dag(
                self.compile_plan(include_cells=include_cells),
                backend,
                progress_callback=progress_callback,
                max_parallel_stages=max_parallel_stages,
            )
        return execute_plan(
            self.compile_plan(include_cells=include_cells),
            backend,
            progress_callback=progress_callback,
        )


def _declared_input_names(statements: list[StatementIR]) -> set[str]:
    return {
        statement.name.lower()
        for statement in statements
        if isinstance(statement, DeclareInputStmt)
    }
