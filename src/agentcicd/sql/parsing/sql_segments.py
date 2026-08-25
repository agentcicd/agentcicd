from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Union

from sqlglot import expressions as exp


class SqlSegmentType(str, Enum):
    CREATE_FUNCTION = "CREATE_FUNCTION"
    CREATE_TABLE = "CREATE_TABLE"
    LOAD_TABLE = "LOAD_TABLE"
    EXPORT_TABLE = "EXPORT_TABLE"
    PUBLISH_REPORTS = "PUBLISH_REPORTS"
    PUBLISH_DATASET = "PUBLISH_DATASET"
    PUBLISH_ANNOTATION = "PUBLISH_ANNOTATION"
    RETRIEVE_ANNOTATION = "RETRIEVE_ANNOTATION"


class ProgressStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"


@dataclass
class ProgressEvent:
    """Typed structure for progress events written to JSONL."""
    step_type: str
    step_name: str
    status: ProgressStatus
    timestamp: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    error_category: Optional[str] = None
    error_type: Optional[str] = None
    error_summary: Optional[str] = None
    error_traceback: Optional[str] = None
    debug_log_path: Optional[str] = None
    row_count: Optional[int] = None
    row_error_count: Optional[int] = None
    cell_error_count: Optional[int] = None
    reuse_state: Optional[str] = None
    cache_hits: Optional[int] = None
    cache_misses: Optional[int] = None
    cache_writes: Optional[int] = None
    # Annotation-specific metadata
    action: Optional[str] = None
    annotation_request_id: Optional[str] = None
    source_ref: Optional[str] = None
    data_path: Optional[str] = None
    target_table: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        """Convert to dict, excluding None values."""
        result = {}
        for k, v in asdict(self).items():
            if v is not None:
                result[k] = v.value if isinstance(v, Enum) else v
        return result


@dataclass
class SqlSegment:
    block_type: SqlSegmentType
    table: str
    segment_id: str = ""
    dependencies: List[str] = field(default_factory=list)
    original_sql: Optional[str] = None

    # CREATE_TABLE fields
    phase_type: Optional[str] = None
    batch_size: Optional[int] = None
    statement_exprs: Optional[List[exp.Expression]] = None
    source_functions: List[str] = field(default_factory=list)

    # LOAD_TABLE and EXPORT_TABLE fields
    path: Optional[str] = None
    options: Optional[Dict[str, Union[str, List[str]]]] = None

    # PUBLISH_ANNOTATION and RETRIEVE_ANNOTATION fields
    queue_name: Optional[str] = None
    publish_alias: Optional[str] = None
    source_ref: Optional[str] = None
    annotation_request_id: Optional[str] = None
    publish_name: Optional[str] = None
    report_component: Optional[str] = None
    chart_type: Optional[str] = None
    report_options: Dict[str, str] = field(default_factory=dict)

    @property
    def resource_keys(self) -> List[str]:
        resources = []
        if self.block_type in {
            SqlSegmentType.LOAD_TABLE,
            SqlSegmentType.CREATE_TABLE,
            SqlSegmentType.RETRIEVE_ANNOTATION,
        }:
            resources.append(_table_resource(self.table))
        if self.block_type == SqlSegmentType.CREATE_FUNCTION:
            resources.append(_function_resource(self.table))
        if self.block_type == SqlSegmentType.PUBLISH_ANNOTATION:
            resources.append(_annotation_request_resource(self.publish_alias or self.table))
        return resources

    @property
    def is_stream(self) -> bool:
        return (self.phase_type or "BATCH").upper() == "STREAM"

    @property
    def statements_sql(self) -> List[str]:
        if self.statement_exprs is None:
            return []
        return [expr.sql(dialect="spark") for expr in self.statement_exprs]

    @property
    def result_expression(self) -> Optional[exp.Expression]:
        if self.statement_exprs is None or len(self.statement_exprs) == 0:
            return None
        return self.statement_exprs[-1]

    @property
    def source_tables(self) -> List[str]:
        if self.statement_exprs is None:
            return []

        sources: List[str] = []
        seen = set()
        target_normalized = self.table.lower()
        defined = set()
        for expr in self.statement_exprs:
            cte_names = _collect_cte_names(expr)
            defined_in_expr = _collect_defined_names(expr)
            for table_expr in expr.find_all(exp.Table):
                name = _table_name(table_expr)
                if not name:
                    continue
                normalized = name.lower()
                if normalized == target_normalized:
                    continue
                if normalized in defined_in_expr:
                    continue
                if normalized in defined:
                    continue
                if normalized in cte_names:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                sources.append(name)
            defined.update(defined_in_expr)
        return sources


def add_segment_dependencies(segments: List[SqlSegment]) -> List[SqlSegment]:
    latest_producer_by_resource: Dict[str, str] = {}

    for segment in segments:
        dependency_ids: List[str] = []
        for resource in _required_resources(segment):
            producer_id = latest_producer_by_resource.get(resource)
            if producer_id is None:
                continue
            if producer_id not in dependency_ids:
                dependency_ids.append(producer_id)
        segment.dependencies = dependency_ids
        for resource in segment.resource_keys:
            latest_producer_by_resource[resource] = segment.segment_id

    return segments


def topologically_sort_segments(segments: List[SqlSegment]) -> List[SqlSegment]:
    segment_by_id = {segment.segment_id: segment for segment in segments}
    indegree = {segment.segment_id: 0 for segment in segments}
    outgoing_edges: Dict[str, List[str]] = defaultdict(list)

    for segment in segments:
        for dependency_id in segment.dependencies:
            if dependency_id not in segment_by_id:
                raise ValueError(
                    f"Segment '{segment.segment_id}' depends on unknown segment '{dependency_id}'"
                )
            outgoing_edges[dependency_id].append(segment.segment_id)
            indegree[segment.segment_id] += 1

    original_position = {
        segment.segment_id: index for index, segment in enumerate(segments)
    }
    ready = deque(
        segment.segment_id
        for segment in segments
        if indegree[segment.segment_id] == 0
    )
    ordered_ids: List[str] = []

    while ready:
        next_segment_id = min(ready, key=lambda segment_id: original_position[segment_id])
        ready.remove(next_segment_id)
        ordered_ids.append(next_segment_id)

        for dependent_id in outgoing_edges[next_segment_id]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)

    if len(ordered_ids) != len(segments):
        unresolved_ids = [
            segment_id for segment_id, remaining in indegree.items() if remaining > 0
        ]
        raise ValueError(f"Cyclic segment dependencies detected: {unresolved_ids}")

    return [segment_by_id[segment_id] for segment_id in ordered_ids]


def _required_resources(segment: SqlSegment) -> List[str]:
    if segment.block_type == SqlSegmentType.CREATE_FUNCTION:
        return [_function_resource(function_name) for function_name in segment.source_functions]
    if segment.block_type == SqlSegmentType.CREATE_TABLE:
        return [_table_resource(table_name) for table_name in segment.source_tables] + [
            _function_resource(function_name) for function_name in segment.source_functions
        ]
    if segment.block_type in {
        SqlSegmentType.EXPORT_TABLE,
        SqlSegmentType.PUBLISH_REPORTS,
        SqlSegmentType.PUBLISH_DATASET,
        SqlSegmentType.PUBLISH_ANNOTATION,
    }:
        return [_table_resource(segment.table)]
    if segment.block_type == SqlSegmentType.RETRIEVE_ANNOTATION and segment.source_ref:
        return [_annotation_request_resource(segment.source_ref)]
    return []


def _table_resource(table_name: str) -> str:
    return f"table:{table_name.lower()}"


def _annotation_request_resource(source_ref: str) -> str:
    return f"annotation_request:{source_ref.lower()}"


def _function_resource(function_name: str) -> str:
    return f"function:{function_name.lower()}"


def _collect_cte_names(expression: exp.Expression) -> set:
    names = set()
    with_expr = expression.args.get("with_")
    if isinstance(with_expr, exp.With):
        for cte in with_expr.expressions:
            alias = getattr(cte, "alias", None)
            if alias:
                if isinstance(alias, str):
                    names.add(alias.lower())
                else:
                    alias_name = getattr(alias, "name", None)
                    if alias_name:
                        names.add(alias_name.lower())
    return names


def _collect_defined_names(expression: exp.Expression) -> set:
    names = set()
    if isinstance(expression, exp.Create):
        target = expression.this
        if isinstance(target, exp.Table):
            name = _table_name(target)
            if name:
                names.add(name.lower())
    return names


def _table_name(table: exp.Table) -> Optional[str]:
    this = table.this
    if isinstance(this, exp.Identifier):
        return this.this
    if isinstance(this, exp.Dot):
        return this.sql(dialect="spark")
    return table.sql(dialect="spark")
