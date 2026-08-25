from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Iterator, Literal, Mapping

import sqlglot
from sqlglot import expressions as exp

from agentcicd.sql.ir.expressions import SqlAstExpr
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.ir.options import StatementOptions
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
from agentcicd.sql.lowering.segment_lowering import (
    infer_statement_variant_outputs,
    lower_declare_input_cell_sql,
    lower_statement_cells_expression,
    lower_statement_cells_sql,
    lower_statement_sql,
)
from agentcicd.sql.pool_inputs import canonical_pool_value_json
from agentcicd.sql.semantics.dependency_graph import DependencyGraph
from agentcicd.sql.semantics.registry import FunctionRegistry


class StepPayload(Mapping[str, object]):
    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field_info in fields(self):
            value = getattr(self, field_info.name)
            if isinstance(value, StatementOptions):
                payload[field_info.name] = value.to_dict()
            elif isinstance(value, tuple) and all(isinstance(item, str) for item in value):
                payload[field_info.name] = list(value)
            else:
                payload[field_info.name] = value
        return payload

    def __getitem__(self, key: str) -> object:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


@dataclass(frozen=True)
class DefinitionStepPayload(StepPayload):
    definition: FunctionDefinitionIR


@dataclass(frozen=True)
class SqlStepPayload(StepPayload):
    sql: str
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass(frozen=True)
class DeclareVariableStepPayload(StepPayload):
    sql: str


@dataclass(frozen=True)
class StreamTableStepPayload(StepPayload):
    sql: str
    source_tables: tuple[str, ...] = ()
    batch_size: int | None = None
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass(frozen=True)
class LoadTableStepPayload(StepPayload):
    path: str
    options: StatementOptions = field(default_factory=StatementOptions)
    wrap_cells: bool = False
    limit: int | None = None


@dataclass(frozen=True)
class SaveTableStepPayload(StepPayload):
    path: str
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass(frozen=True)
class PublishDatasetStepPayload(StepPayload):
    dataset_name: str | None = None


@dataclass(frozen=True)
class PublishAnnotationStepPayload(StepPayload):
    queue_name: str
    alias: str | None = None
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass(frozen=True)
class PublishReportStepPayload(StepPayload):
    component: str
    chart_type: str | None = None
    report_options: dict[str, str] | None = None


@dataclass(frozen=True)
class RetrieveAnnotationStepPayload(StepPayload):
    source_ref: str
    annotation_request_id: str | None = None
    wrap_cells: bool = False


PlanPayload = (
    DefinitionStepPayload
    | DeclareVariableStepPayload
    | SqlStepPayload
    | StreamTableStepPayload
    | LoadTableStepPayload
    | SaveTableStepPayload
    | PublishReportStepPayload
    | PublishDatasetStepPayload
    | PublishAnnotationStepPayload
    | RetrieveAnnotationStepPayload
    | None
)


@dataclass
class ExecutionPlanStep:
    kind: Literal[
        "register_sql_function",
        "register_runtime_function",
        "declare_variable",
        "create_batch_table",
        "create_stream_table",
        "load_table",
        "save_table",
        "publish_report",
        "publish_dataset",
        "publish_annotation",
        "retrieve_annotation",
    ]
    name: str
    payload: PlanPayload = None
    dependencies: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.payload, Mapping) and not isinstance(self.payload, StepPayload):
            self.payload = _coerce_payload(self.kind, self.payload)


def compile_execution_plan(
    statements: list[StatementIR],
    registry: FunctionRegistry,
    *,
    include_cells: bool = False,
    dependency_graph: DependencyGraph | None = None,
    input_values: Mapping[str, str] | None = None,
    render_sql: bool = True,
) -> list[ExecutionPlanStep]:
    steps: list[ExecutionPlanStep] = []
    table_variant_outputs: dict[str, set[str]] = {}
    seen_runtime_functions: set[str] = set()
    normalized_input_values = {str(key).lower(): str(value) for key, value in dict(input_values or {}).items()}
    declared_input_names = {
        statement.name.lower()
        for statement in statements
        if isinstance(statement, DeclareInputStmt)
    }
    dataset_input_defaults = {
        statement.name.lower(): _declared_input_default_sql(statement, normalized_input_values)
        for statement in statements
        if isinstance(statement, DeclareInputStmt) and statement.input_type.upper() == "DATASET"
    }
    for definition in registry.by_canonical_name.values():
        if definition.kind == "sql":
            continue
        canonical_name = definition.canonical_name.lower()
        if dependency_graph is not None and f"function:{canonical_name}" not in dependency_graph.edges:
            continue
        if canonical_name in seen_runtime_functions:
            continue
        seen_runtime_functions.add(canonical_name)
        steps.append(
            ExecutionPlanStep(
                kind="register_runtime_function",
                name=definition.canonical_name,
                payload=DefinitionStepPayload(definition=definition),
            )
        )
    for statement in statements:
        if isinstance(statement, DeclareInputStmt):
            statement_for_execution = _statement_with_input_override(statement, normalized_input_values)
            steps.append(
                ExecutionPlanStep(
                    kind="declare_variable",
                    name=statement.name,
                    payload=DeclareVariableStepPayload(
                        sql=(
                            lower_declare_input_cell_sql(statement_for_execution)
                            if include_cells
                            else lower_statement_sql(statement_for_execution, registry)
                        )
                    ),
                )
            )
        elif isinstance(statement, SqlFunctionDefStmt) and statement.definition is not None:
            steps.append(
                ExecutionPlanStep(
                    kind="register_sql_function",
                    name=statement.definition.canonical_name,
                    payload=DefinitionStepPayload(definition=statement.definition),
                )
            )
        elif isinstance(statement, BatchTableStmt):
            input_variant_columns = _statement_input_variant_columns(
                statement.name,
                dependency_graph,
                table_variant_outputs,
                statement=statement,
            )
            steps.append(
                ExecutionPlanStep(
                    kind="create_batch_table",
                    name=statement.name,
                    payload=SqlStepPayload(
                        sql=_lower_plan_table_sql(
                            statement,
                            registry,
                            include_cells=include_cells,
                            render_sql=render_sql,
                            variant_columns=input_variant_columns,
                            non_cell_columns=set() if include_cells else declared_input_names,
                        ),
                        options=statement.options,
                    ),
                )
            )
            table_variant_outputs[statement.name.lower()] = infer_statement_variant_outputs(
                statement,
                registry,
                variant_columns=input_variant_columns,
            )
        elif isinstance(statement, StreamTableStmt):
            input_variant_columns = _statement_input_variant_columns(
                statement.name,
                dependency_graph,
                table_variant_outputs,
                statement=statement,
            )
            source_tables = []
            if dependency_graph is not None:
                source_tables = sorted(
                    dependency.split(":", 1)[1]
                    for dependency in dependency_graph.edges.get(f"table:{statement.name.lower()}", set())
                    if dependency.startswith("table:")
                )
            steps.append(
                ExecutionPlanStep(
                    kind="create_stream_table",
                    name=statement.name,
                    payload=StreamTableStepPayload(
                        sql=_lower_plan_table_sql(
                            statement,
                            registry,
                            include_cells=include_cells,
                            render_sql=render_sql,
                            variant_columns=input_variant_columns,
                            non_cell_columns=set() if include_cells else declared_input_names,
                        ),
                        source_tables=tuple(source_tables),
                        batch_size=statement.batch_size,
                        options=statement.options,
                    ),
                )
            )
            table_variant_outputs[statement.name.lower()] = infer_statement_variant_outputs(
                statement,
                registry,
                variant_columns=input_variant_columns,
            )
        elif isinstance(statement, LoadStmt):
            load_path = dataset_input_defaults.get(statement.path.lower(), statement.path)
            if load_path is None:
                load_path = statement.path
            steps.append(
                ExecutionPlanStep(
                    kind="load_table",
                    name=statement.table,
                    payload=LoadTableStepPayload(
                        path=load_path,
                        options=statement.options,
                        wrap_cells=include_cells,
                        limit=statement.limit,
                    ),
                )
            )
        elif isinstance(statement, SaveStmt):
            steps.append(
                ExecutionPlanStep(
                    kind="save_table",
                    name=statement.table,
                    payload=SaveTableStepPayload(path=statement.path, options=statement.options),
                )
            )
        elif isinstance(statement, PublishReportsStmt):
            steps.append(
                ExecutionPlanStep(
                    kind="publish_report",
                    name=statement.table,
                    payload=PublishReportStepPayload(
                        component=statement.component,
                        chart_type=statement.chart_type,
                        report_options=statement.report_options,
                    ),
                )
            )
        elif isinstance(statement, PublishDatasetStmt):
            steps.append(
                ExecutionPlanStep(
                    kind="publish_dataset",
                    name=statement.table,
                    payload=PublishDatasetStepPayload(dataset_name=statement.dataset_name),
                )
            )
        elif isinstance(statement, PublishAnnotationStmt):
            steps.append(
                ExecutionPlanStep(
                    kind="publish_annotation",
                    name=statement.table,
                    payload=PublishAnnotationStepPayload(
                        queue_name=statement.queue_name,
                        alias=statement.alias,
                        options=statement.options,
                    ),
                )
            )
        elif isinstance(statement, RetrieveAnnotationStmt):
            steps.append(
                ExecutionPlanStep(
                    kind="retrieve_annotation",
                    name=statement.table,
                    payload=RetrieveAnnotationStepPayload(
                        source_ref=statement.source_ref,
                        annotation_request_id=statement.annotation_request_id,
                        wrap_cells=include_cells,
                    ),
                )
            )
    if dependency_graph is not None:
        return attach_plan_dependencies(steps, dependency_graph)
    return steps


def _lower_plan_table_sql(
    statement: BatchTableStmt | StreamTableStmt,
    registry: FunctionRegistry,
    *,
    include_cells: bool,
    render_sql: bool,
    variant_columns: set[str],
    non_cell_columns: set[str],
) -> str:
    if include_cells:
        lowered = lower_statement_cells_expression(
            statement,
            registry,
            variant_columns=variant_columns,
            non_cell_columns=non_cell_columns,
        )
        return lowered.sql(dialect="spark") if render_sql else ""
    if not render_sql:
        return ""
    return lower_statement_sql(
        statement,
        registry,
        variant_columns=variant_columns,
    )


def _string_default_value(default_sql: str | None) -> str | None:
    if not default_sql:
        return None
    expression = sqlglot.parse_one(default_sql, read="spark")
    if isinstance(expression, exp.Literal) and expression.args.get("is_string"):
        return str(expression.this)
    return None


def _declared_input_default_sql(statement: DeclareInputStmt, input_values: Mapping[str, str]) -> str | None:
    overridden = _statement_with_input_override(statement, input_values)
    return _string_default_value(overridden.default_sql)


def _statement_with_input_override(statement: DeclareInputStmt, input_values: Mapping[str, str]) -> DeclareInputStmt:
    value = input_values.get(statement.name.lower())
    if value is None:
        return statement
    return DeclareInputStmt(
        name=statement.name,
        input_type=statement.input_type,
        options=statement.options,
        default_sql=_input_value_to_default_sql(statement.input_type, value),
        source_text=statement.source_text,
    )


def _input_value_to_default_sql(input_type: str, value: str) -> str:
    normalized_type = input_type.strip().upper()
    if normalized_type in {"AISYSTEM", "DATASET", "SECRET", "STRING"}:
        return _quote_sql_string(value)
    if normalized_type == "RATELIMIT":
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"RATELIMIT input value must be a positive integer: {value}") from exc
        if parsed < 1:
            raise ValueError(f"RATELIMIT input value must be a positive integer: {value}")
        return str(parsed)
    if normalized_type == "POOL":
        return _quote_sql_string(canonical_pool_value_json(value))
    if normalized_type == "DATE":
        return f"DATE {_quote_sql_string(value)}"
    if normalized_type == "TIMESTAMP":
        return f"TIMESTAMP {_quote_sql_string(value)}"
    return value


def _quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def attach_plan_dependencies(
    steps: list[ExecutionPlanStep],
    dependency_graph: DependencyGraph,
) -> list[ExecutionPlanStep]:
    node_to_step = {_step_node_name(step): step for step in steps}
    for node_name, step in node_to_step.items():
        step.dependencies = sorted(
            dependency
            for dependency in dependency_graph.edges.get(node_name, set())
            if dependency in node_to_step
        )
    return topologically_sort_plan(steps)


def topologically_sort_plan(steps: list[ExecutionPlanStep]) -> list[ExecutionPlanStep]:
    node_to_step = {_step_node_name(step): step for step in steps}
    indegree = {node: 0 for node in node_to_step}
    outgoing: dict[str, list[str]] = {node: [] for node in node_to_step}

    for node, step in node_to_step.items():
        for dependency in step.dependencies:
            if dependency not in node_to_step:
                continue
            outgoing.setdefault(dependency, []).append(node)
            indegree[node] += 1

    ready = [node for node, degree in indegree.items() if degree == 0]
    original = {node: index for index, node in enumerate(node_to_step)}
    ordered: list[ExecutionPlanStep] = []

    while ready:
        next_node = min(ready, key=lambda node: original[node])
        ready.remove(next_node)
        ordered.append(node_to_step[next_node])
        for dependent in outgoing.get(next_node, []):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(steps):
        raise ValueError("Cyclic plan dependencies detected")
    return ordered


def _statement_input_variant_columns(
    statement_name: str,
    dependency_graph: DependencyGraph | None,
    table_variant_outputs: dict[str, set[str]],
    *,
    statement: StatementIR | None = None,
) -> set[str]:
    if dependency_graph is None:
        return set()
    discovered: set[str] = set()
    aliases = _statement_table_aliases(statement)
    for dependency in dependency_graph.edges.get(f"table:{statement_name.lower()}", set()):
        if not dependency.startswith("table:"):
            continue
        table_name = dependency.split(":", 1)[1]
        variant_columns = table_variant_outputs.get(table_name, set())
        discovered.update(variant_columns)
        for column in variant_columns:
            discovered.add(f"{table_name}.{column}")
        for alias in aliases.get(table_name, set()):
            for column in variant_columns:
                discovered.add(f"{alias}.{column}")
    return discovered


def _statement_table_aliases(statement: StatementIR | None) -> dict[str, set[str]]:
    if not isinstance(statement, (BatchTableStmt, StreamTableStmt)):
        return {}
    if not isinstance(statement.query, SqlAstExpr):
        return {}
    aliases: dict[str, set[str]] = {}
    for table in statement.query.expression.find_all(exp.Table):
        table_name = str(table.name or "").strip().lower()
        if not table_name:
            continue
        alias = str(table.alias or "").strip().lower()
        if alias:
            aliases.setdefault(table_name, set()).add(alias)
    return aliases


def _step_node_name(step: ExecutionPlanStep) -> str:
    if step.kind in {"register_sql_function", "register_runtime_function"}:
        return f"function:{step.name.lower()}"
    if step.kind == "declare_variable":
        return f"input:{step.name.lower()}"
    if step.kind in {"create_batch_table", "create_stream_table", "load_table", "retrieve_annotation"}:
        return f"table:{step.name.lower()}"
    if step.kind == "save_table":
        payload = step.payload
        if not isinstance(payload, SaveTableStepPayload):
            raise ValueError("save_table step is missing a typed payload")
        return f"save:{step.name.lower()}->{payload.path}"
    if step.kind == "publish_report":
        payload = step.payload
        component = payload.component if isinstance(payload, PublishReportStepPayload) else "unknown"
        return f"publish:reports:{component}:{step.name.lower()}"
    if step.kind == "publish_dataset":
        return f"publish:dataset:{step.name.lower()}"
    if step.kind == "publish_annotation":
        payload = step.payload
        if isinstance(payload, PublishAnnotationStepPayload) and payload.alias:
            return f"publish:annotation:{payload.alias.lower()}"
        return f"publish:annotation:{step.name.lower()}"
    raise ValueError(f"Unsupported plan step kind '{step.kind}'")


def plan_node_id(step: ExecutionPlanStep) -> str:
    if step.kind == "create_batch_table":
        return f"create_batch_table:{step.name}"
    if step.kind == "create_stream_table":
        return f"create_stream_table:{step.name}"
    if step.kind == "load_table":
        return f"load_table:{step.name}"
    if step.kind == "retrieve_annotation":
        return f"retrieve_annotation:{step.name}"
    if step.kind == "save_table":
        return f"save_table:{step.name}"
    if step.kind == "publish_report":
        return f"publish_report:{step.name}"
    if step.kind == "publish_dataset":
        return f"publish_dataset:{step.name}"
    if step.kind == "publish_annotation":
        return f"publish_annotation:{step.name}"
    if step.kind == "declare_variable":
        return f"declare_variable:{step.name}"
    if step.kind == "register_sql_function":
        return f"register_sql_function:{step.name}"
    if step.kind == "register_runtime_function":
        return f"register_runtime_function:{step.name}"
    return f"{step.kind}:{step.name}"


def payload_to_dict(payload: PlanPayload) -> dict[str, object]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping) and not isinstance(payload, StepPayload):
        return {str(key): value for key, value in payload.items()}
    return payload.as_dict()


def _coerce_payload(kind: str, payload: Mapping[str, object]) -> PlanPayload:
    if kind in {"register_sql_function", "register_runtime_function"}:
        definition = payload.get("definition")
        if isinstance(definition, FunctionDefinitionIR):
            return DefinitionStepPayload(definition=definition)
    if kind == "declare_variable":
        sql = payload.get("sql")
        if isinstance(sql, str):
            return DeclareVariableStepPayload(sql=sql)
    if kind == "create_batch_table":
        sql = payload.get("sql")
        if isinstance(sql, str):
            return SqlStepPayload(
                sql=sql,
                options=StatementOptions.from_mapping(payload.get("options") if isinstance(payload.get("options"), Mapping) else {}),
            )
    if kind == "create_stream_table":
        sql = payload.get("sql")
        if isinstance(sql, str):
            source_tables = payload.get("source_tables")
            batch_size = payload.get("batch_size")
            normalized_source_tables = tuple(str(item) for item in source_tables) if isinstance(source_tables, list) else ()
            normalized_batch_size = int(batch_size) if isinstance(batch_size, int) else None
            return StreamTableStepPayload(
                sql=sql,
                source_tables=normalized_source_tables,
                batch_size=normalized_batch_size,
                options=StatementOptions.from_mapping(payload.get("options") if isinstance(payload.get("options"), Mapping) else {}),
            )
    if kind == "load_table":
        path = payload.get("path")
        if isinstance(path, str):
            return LoadTableStepPayload(
                path=path,
                options=StatementOptions.from_mapping(payload.get("options") if isinstance(payload.get("options"), Mapping) else {}),
                wrap_cells=bool(payload.get("wrap_cells")),
                limit=int(payload["limit"]) if isinstance(payload.get("limit"), int) else None,
            )
    if kind == "save_table":
        path = payload.get("path")
        if isinstance(path, str):
            return SaveTableStepPayload(
                path=path,
                options=StatementOptions.from_mapping(payload.get("options") if isinstance(payload.get("options"), Mapping) else {}),
            )
    if kind == "publish_dataset":
        dataset_name = payload.get("dataset_name")
        if dataset_name is None or isinstance(dataset_name, str):
            return PublishDatasetStepPayload(dataset_name=dataset_name)
    if kind == "publish_report":
        component = payload.get("component")
        chart_type = payload.get("chart_type")
        report_options = payload.get("report_options")
        if (
            isinstance(component, str)
            and (chart_type is None or isinstance(chart_type, str))
            and (report_options is None or isinstance(report_options, dict))
        ):
            return PublishReportStepPayload(
                component=component,
                chart_type=chart_type,
                report_options={str(key): str(value) for key, value in (report_options or {}).items()},
            )
    if kind == "publish_annotation":
        queue_name = payload.get("queue_name")
        alias = payload.get("alias")
        if isinstance(queue_name, str):
            return PublishAnnotationStepPayload(
                queue_name=queue_name,
                alias=alias if isinstance(alias, str) else None,
                options=StatementOptions.from_mapping(payload.get("options") if isinstance(payload.get("options"), Mapping) else {}),
            )
    if kind == "retrieve_annotation":
        source_ref = payload.get("source_ref")
        annotation_request_id = payload.get("annotation_request_id")
        if isinstance(source_ref, str):
            return RetrieveAnnotationStepPayload(
                source_ref=source_ref,
                annotation_request_id=annotation_request_id if isinstance(annotation_request_id, str) else None,
                wrap_cells=bool(payload.get("wrap_cells")),
            )
    return None
