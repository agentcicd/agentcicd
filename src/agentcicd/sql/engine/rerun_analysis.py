from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.plan import ExecutionPlanStep, _step_node_name
from agentcicd.sql.ir.functions import RegisteredFunctionSpec


MATERIALIZED_STEP_KINDS = {
    "load_table",
    "create_batch_table",
    "create_stream_table",
    "retrieve_annotation",
}


class RerunFeasibilityError(ValueError):
    pass


@dataclass(frozen=True)
class RerunAnalysis:
    dirty_tables: set[str]
    staged_tables: set[str]
    selected_node: str | None
    selected_node_mode: str
    include_descendants: bool


def analyze_rerun_feasibility(
    sql_text: str,
    *,
    completed_tables: Iterable[str],
    registered_functions: (
        list[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]] | None
    ) = None,
    from_node: str | None = None,
    from_node_mode: str = "auto",
    include_descendants: bool = True,
    reuse_policy: str = "reuse_clean_upstream",
) -> RerunAnalysis:
    completed = _normalize_table_names(completed_tables)
    entrypoint = EngineEntrypoint(sql_text, registered_functions=registered_functions or [])
    plan = entrypoint.compile_plan(include_cells=False)
    step_by_node = {_step_node_name(step): step for step in plan}
    materialized_nodes = {
        node
        for node, step in step_by_node.items()
        if step.kind in MATERIALIZED_STEP_KINDS
    }

    if reuse_policy == "force_recompute_all":
        dirty_nodes = set(materialized_nodes)
    elif from_node:
        dirty_nodes = _dirty_nodes_from_selected_node(
            from_node,
            step_by_node=step_by_node,
            include_descendants=include_descendants,
        )
        if include_descendants:
            dirty_nodes = _expand_dirty_descendant_prerequisites(
                dirty_nodes,
                selected_nodes={from_node},
                completed_tables=completed,
                step_by_node=step_by_node,
            )
    else:
        dirty_nodes = _auto_dirty_nodes(
            completed_tables=completed,
            step_by_node=step_by_node,
            materialized_nodes=materialized_nodes,
        )

    dirty_tables = {
        step_by_node[node].name
        for node in dirty_nodes
        if node in step_by_node and step_by_node[node].kind in MATERIALIZED_STEP_KINDS
    }
    staged_tables = _required_clean_upstream_tables(
        dirty_nodes,
        completed_tables=completed,
        step_by_node=step_by_node,
    )
    _validate_dirty_tables_are_executable(
        dirty_nodes,
        completed_tables=completed,
        staged_tables={table.lower() for table in staged_tables},
        step_by_node=step_by_node,
    )

    return RerunAnalysis(
        dirty_tables=dirty_tables,
        staged_tables=staged_tables,
        selected_node=from_node,
        selected_node_mode=from_node_mode,
        include_descendants=include_descendants,
    )


def _dirty_nodes_from_selected_node(
    selected_node: str,
    *,
    step_by_node: dict[str, ExecutionPlanStep],
    include_descendants: bool,
) -> set[str]:
    if selected_node not in step_by_node:
        raise RerunFeasibilityError(f"Unknown rerun from_node: {selected_node}")
    selected_step = step_by_node[selected_node]
    if selected_step.kind not in MATERIALIZED_STEP_KINDS:
        raise RerunFeasibilityError("Rerun from_node must be a materialized node, not a publish/setup node")

    dirty_nodes = {selected_node}
    if not include_descendants:
        return dirty_nodes

    outgoing = _outgoing_dependencies(step_by_node)
    stack = list(outgoing.get(selected_node, set()))
    while stack:
        node = stack.pop()
        if node in dirty_nodes:
            continue
        dirty_nodes.add(node)
        stack.extend(outgoing.get(node, set()))
    return dirty_nodes


def _expand_dirty_descendant_prerequisites(
    dirty_nodes: set[str],
    *,
    selected_nodes: set[str],
    completed_tables: set[str],
    step_by_node: dict[str, ExecutionPlanStep],
) -> set[str]:
    expanded = set(dirty_nodes)
    stack = [node for node in expanded if node not in selected_nodes]
    while stack:
        node = stack.pop()
        step = step_by_node.get(node)
        if step is None:
            continue
        for dependency in step.dependencies:
            if not dependency.startswith("table:"):
                continue
            if dependency in expanded:
                continue
            dependency_table = dependency.split(":", 1)[1]
            if dependency_table.lower() in completed_tables:
                continue
            dependency_step = step_by_node.get(dependency)
            if dependency_step is None or dependency_step.kind not in MATERIALIZED_STEP_KINDS:
                continue
            expanded.add(dependency)
            stack.append(dependency)
    return expanded


def _auto_dirty_nodes(
    *,
    completed_tables: set[str],
    step_by_node: dict[str, ExecutionPlanStep],
    materialized_nodes: set[str],
) -> set[str]:
    dirty_nodes = {
        node
        for node in materialized_nodes
        if step_by_node[node].name.lower() not in completed_tables
    }
    outgoing = _outgoing_dependencies(step_by_node)
    stack = list(dirty_nodes)
    while stack:
        node = stack.pop()
        for descendant in outgoing.get(node, set()):
            if descendant in dirty_nodes:
                continue
            dirty_nodes.add(descendant)
            stack.append(descendant)
    return dirty_nodes


def _required_clean_upstream_tables(
    dirty_nodes: set[str],
    *,
    completed_tables: set[str],
    step_by_node: dict[str, ExecutionPlanStep],
) -> set[str]:
    staged: set[str] = set()
    stack = list(dirty_nodes)
    visited: set[str] = set()
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        step = step_by_node.get(node)
        if step is None:
            continue
        for dependency in step.dependencies:
            if not dependency.startswith("table:"):
                continue
            dependency_table = dependency.split(":", 1)[1]
            if dependency in dirty_nodes:
                stack.append(dependency)
            elif dependency_table.lower() in completed_tables:
                staged.add(dependency_table)
                stack.append(dependency)
    return staged


def _validate_dirty_tables_are_executable(
    dirty_nodes: set[str],
    *,
    completed_tables: set[str],
    staged_tables: set[str],
    step_by_node: dict[str, ExecutionPlanStep],
) -> None:
    missing: dict[str, set[str]] = {}
    for node in dirty_nodes:
        step = step_by_node.get(node)
        if step is None or step.kind not in MATERIALIZED_STEP_KINDS:
            continue
        for dependency in step.dependencies:
            if not dependency.startswith("table:"):
                continue
            dependency_table = dependency.split(":", 1)[1]
            if dependency in dirty_nodes or dependency_table.lower() in staged_tables:
                continue
            if dependency_table.lower() in completed_tables:
                continue
            missing.setdefault(step.name, set()).add(dependency_table)
    if missing:
        details = "; ".join(
            f"{table} requires {', '.join(sorted(dependencies))}"
            for table, dependencies in sorted(missing.items())
        )
        raise RerunFeasibilityError(f"Rerun cannot be staged from the selected node: {details}")


def _outgoing_dependencies(step_by_node: dict[str, ExecutionPlanStep]) -> dict[str, set[str]]:
    outgoing: dict[str, set[str]] = {}
    for node, step in step_by_node.items():
        for dependency in step.dependencies:
            outgoing.setdefault(dependency, set()).add(node)
    return outgoing


def _normalize_table_names(tables: Iterable[str]) -> set[str]:
    return {str(table).strip().lower() for table in tables if str(table).strip()}
