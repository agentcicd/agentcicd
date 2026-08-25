from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, Set

from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import CallExpr, SqlAstExpr
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.ir.statements import (
    BatchTableStmt,
    DeclareInputStmt,
    LoadStmt,
    PublishAnnotationStmt,
    PublishDatasetStmt,
    PublishReportsStmt,
    RetrieveAnnotationStmt,
    SaveStmt,
    SqlFunctionDefStmt,
    StatementIR,
    StreamTableStmt,
)
from agentcicd.sql.ir.visitors import walk_ir
from agentcicd.sql.surface.sqlglot_bridge import _extract_namespaced_call, expression_to_ir

if TYPE_CHECKING:
    from agentcicd.sql.semantics.registry import FunctionRegistry


@dataclass
class DependencyGraph:
    edges: Dict[str, Set[str]] = field(default_factory=dict)
    node_kinds: Dict[str, str] = field(default_factory=dict)
    edge_kinds: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def add_node(self, name: str, *, kind: str | None = None) -> None:
        self.edges.setdefault(name, set())
        if kind is not None:
            self.node_kinds[name] = kind

    def add_edge(self, source: str, target: str, *, kind: str | None = None) -> None:
        self.add_node(source)
        self.add_node(target)
        self.edges[source].add(target)
        if kind is not None:
            self.edge_kinds.setdefault(source, {})[target] = kind


def ensure_acyclic_dependency_graph(graph: DependencyGraph) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("Cyclic plan dependencies detected")
        visiting.add(node)
        for dependency in sorted(graph.edges.get(node, set())):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph.edges):
        visit(node)


def build_dependency_graph(statements: List[StatementIR], registry: "FunctionRegistry | None" = None) -> DependencyGraph:
    graph = DependencyGraph()
    declared_input_types = {
        statement.name.lower(): statement.input_type.strip().lower()
        for statement in statements
        if isinstance(statement, DeclareInputStmt)
    }

    for statement in statements:
        if isinstance(statement, DeclareInputStmt):
            graph.add_node(
                _input_node_name(statement.name),
                kind=f"input:{statement.input_type.strip().lower()}",
            )
        if isinstance(statement, SqlFunctionDefStmt) and statement.definition is not None:
            graph.add_node(_function_node_name(statement.definition.canonical_name), kind="function:sql")
        if isinstance(statement, BatchTableStmt):
            graph.add_node(_table_node_name(statement.name), kind="table:create_batch")
        if isinstance(statement, StreamTableStmt):
            graph.add_node(_table_node_name(statement.name), kind="table:create_stream")
        if isinstance(statement, LoadStmt):
            graph.add_node(_table_node_name(statement.table), kind="table:load")
        if isinstance(statement, RetrieveAnnotationStmt):
            graph.add_node(_table_node_name(statement.table), kind="table:retrieve_annotation")
            graph.add_node(_publish_node_name("annotation", statement.source_ref), kind="publish:annotation")
        if isinstance(statement, SaveStmt):
            graph.add_node(_save_node_name(statement.table, statement.path), kind="save")
        if isinstance(statement, PublishReportsStmt):
            graph.add_node(_publish_node_name(f"reports:{statement.component}", statement.table), kind="publish:reports")
        if isinstance(statement, PublishDatasetStmt):
            graph.add_node(_publish_node_name("dataset", statement.table), kind="publish:dataset")
        if isinstance(statement, PublishAnnotationStmt):
            graph.add_node(_publish_node_name("annotation", statement.alias or statement.table), kind="publish:annotation")

    for statement in statements:
        if isinstance(statement, SqlFunctionDefStmt) and statement.definition is not None:
            _add_function_dependencies(graph, statement.definition, registry=registry)
        elif isinstance(statement, (BatchTableStmt, StreamTableStmt)) and statement.query is not None:
            table_node = _table_node_name(statement.name)
            for function_name in _find_function_calls(statement.query):
                graph.add_edge(table_node, _function_node_name(_canonical_function_name(function_name, registry)), kind="calls_function")
            for table_name in _find_table_references(statement.query):
                graph.add_edge(table_node, _table_node_name(table_name), kind="reads_table")
        elif isinstance(statement, LoadStmt) and declared_input_types.get(statement.path.lower()) == "dataset":
            graph.add_edge(_table_node_name(statement.table), _input_node_name(statement.path), kind="loads_dataset_input")
        elif isinstance(statement, SaveStmt):
            graph.add_edge(_save_node_name(statement.table, statement.path), _table_node_name(statement.table), kind="saves_table")
        elif isinstance(statement, PublishReportsStmt):
            graph.add_edge(
                _publish_node_name(f"reports:{statement.component}", statement.table),
                _table_node_name(statement.table),
                kind="publishes_table",
            )
        elif isinstance(statement, PublishDatasetStmt):
            graph.add_edge(_publish_node_name("dataset", statement.table), _table_node_name(statement.table), kind="publishes_table")
        elif isinstance(statement, PublishAnnotationStmt):
            graph.add_edge(
                _publish_node_name("annotation", statement.alias or statement.table),
                _table_node_name(statement.table),
                kind="publishes_table",
            )
        elif isinstance(statement, RetrieveAnnotationStmt):
            graph.add_edge(
                _table_node_name(statement.table),
                _publish_node_name("annotation", statement.source_ref),
                kind="retrieves_annotation",
            )
    return graph


def validate_relation_dependencies(
    statements: List[StatementIR],
    graph: DependencyGraph,
    *,
    external_tables: Iterable[str] | None = None,
) -> None:
    produced_tables = {
        statement.table.lower()
        for statement in statements
        if isinstance(statement, (LoadStmt, RetrieveAnnotationStmt))
    }
    produced_tables.update(
        statement.name.lower()
        for statement in statements
        if isinstance(statement, (BatchTableStmt, StreamTableStmt))
    )
    dataset_inputs = {
        statement.name.lower()
        for statement in statements
        if isinstance(statement, DeclareInputStmt) and statement.input_type.upper() == "DATASET"
    }
    external_table_names = {str(table).strip().lower() for table in external_tables or [] if str(table).strip()}
    for table_name in external_table_names:
        graph.add_node(_table_node_name(table_name), kind="table:external")

    for source, targets in graph.edges.items():
        if not source.startswith("table:"):
            continue
        for target in sorted(targets):
            if graph.edge_kinds.get(source, {}).get(target) != "reads_table":
                continue
            table_name = target.split(":", 1)[1]
            if table_name in produced_tables or table_name in external_table_names:
                continue
            if table_name in dataset_inputs:
                raise ValueError(
                    f"DATASET input '{table_name}' is not a SQL table. "
                    f"Load it first with `LOAD <table_name> FROM {table_name}` and query the loaded table."
                )
            raise ValueError(
                f"Table '{table_name}' is referenced but is not produced by LOAD, CREATE TABLE, "
                "or RETRIEVE ANNOTATION RESULTS"
            )


def _add_function_dependencies(
    graph: DependencyGraph,
    definition: FunctionDefinitionIR,
    *,
    registry: "FunctionRegistry | None" = None,
) -> None:
    function_node = _function_node_name(definition.canonical_name)
    if definition.sql_body is None:
        return
    for assignment in definition.sql_body.assignments:
        for dependency in _find_function_calls(assignment.value):
            graph.add_edge(function_node, _function_node_name(_canonical_function_name(dependency, registry)), kind="calls_function")
    if definition.sql_body.return_expr is not None:
        for dependency in _find_function_calls(definition.sql_body.return_expr.value):
            graph.add_edge(function_node, _function_node_name(_canonical_function_name(dependency, registry)), kind="calls_function")


def _canonical_function_name(function_name: str, registry: "FunctionRegistry | None") -> str:
    if registry is None:
        return function_name
    definition = registry.resolve(function_name)
    return definition.canonical_name if definition is not None else function_name


def _find_function_calls(expression) -> Set[str]:
    discovered: set[str] = set()

    if isinstance(expression, SqlAstExpr):
        for node in expression.expression.walk():
            if isinstance(node, exp.Func):
                lowered = expression_to_ir(node)
                if isinstance(lowered, CallExpr):
                    discovered.add(lowered.function_name)
            elif _extract_namespaced_call(node) is not None:
                lowered = expression_to_ir(node)
                if isinstance(lowered, CallExpr):
                    discovered.add(lowered.function_name)

    def visit(node) -> None:
        if isinstance(node, CallExpr):
            discovered.add(node.function_name)

    walk_ir(expression, visit)
    return discovered


def _find_table_references(expression) -> Set[str]:
    if not isinstance(expression, SqlAstExpr):
        return set()
    discovered: set[str] = set()
    cte_names = _collect_cte_names(expression.expression)
    for table in expression.expression.find_all(exp.Table):
        name = _table_name(table)
        if not name:
            continue
        if name.lower() in cte_names:
            continue
        discovered.add(name)
    return discovered


def _collect_cte_names(expression: exp.Expression) -> set[str]:
    names: set[str] = set()
    with_expr = expression.args.get("with_")
    if isinstance(with_expr, exp.With):
        for cte in with_expr.expressions:
            alias = getattr(cte, "alias", None)
            if isinstance(alias, str) and alias:
                names.add(alias.lower())
                continue
            alias_name = getattr(alias, "name", None)
            if alias_name:
                names.add(str(alias_name).lower())
    return names


def _table_name(table: exp.Table) -> str | None:
    this = table.this
    if isinstance(this, exp.Identifier):
        return str(this.this)
    if isinstance(this, exp.Dot):
        return this.sql(dialect="spark")
    rendered = table.sql(dialect="spark")
    return rendered or None


def _function_node_name(name: str) -> str:
    return f"function:{name.lower()}"


def _input_node_name(name: str) -> str:
    return f"input:{name.lower()}"


def _table_node_name(name: str) -> str:
    return f"table:{name.lower()}"


def _save_node_name(table: str, path: str) -> str:
    return f"save:{table.lower()}->{path}"


def _publish_node_name(kind: str, table: str) -> str:
    return f"publish:{kind}:{table.lower()}"
