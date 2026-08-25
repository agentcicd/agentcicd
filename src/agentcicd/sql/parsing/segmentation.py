"""
SQL Script Segmentation Module (V2 - Sqlglot-based)

This module provides functionality to segment SQL scripts into logical parts:
- Functions (CREATE FUNCTION statements)
- Data Loading (LOAD statements)
- Table Creation (CREATE BATCH/STREAM TABLE or BEGIN...END blocks)
- Data Saving (SAVE statements)

Uses the unified AgentCICD SQL parser and projects parsed executable segments into
API-facing segmentation views.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional

from sqlglot import expressions as exp

from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.ir.expressions import SqlAstExpr
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
from agentcicd.sql.surface.top_level_parser import TopLevelParser


class SegmentType(str, Enum):
    """Types of SQL segments"""
    FUNCTIONS = "functions"
    LOAD = "load"
    TABLE = "table"
    SAVE = "save"
    PUBLISH = "publish"
    PUBLISH_ANNOTATION = "publish_annotation"
    RETRIEVE_ANNOTATION = "retrieve_annotation"


@dataclass
class FunctionSegment:
    """Represents a SQL function definition"""
    name: str
    sql_text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class LoadSegment:
    """Represents a data loading operation"""
    table: str
    source: str
    options: Dict[str, object]
    sql_text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class InputSegment:
    """Represents a declared runtime input"""
    name: str
    input_type: str
    options: Dict[str, object]
    default: Optional[str]
    environment: Optional[str]
    required: bool
    sql_text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class TableSegment:
    """Represents a table creation phase (BATCH or STREAM)"""
    table: str
    phase_type: str
    batch_size: Optional[int]
    options: Dict[str, object]
    sql_text: str
    query_sql: str
    depends_on: List[str]
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class SaveSegment:
    """Represents a data saving operation"""
    table: str
    destination: str
    options: Dict[str, object]
    sql_text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class PublishSegment:
    """Represents a publish operation (e.g., PUBLISH <table> TO REPORTS|DATASET)"""
    table: str
    destination: str
    published_name: Optional[str]
    sql_text: str
    component: Optional[str] = None
    chart_type: Optional[str] = None
    report_options: Dict[str, str] = field(default_factory=dict)
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class PublishAnnotationSegment:
    """Represents a publish to annotation operation"""
    table: str
    queue_name: str
    alias: Optional[str]
    options: Dict[str, object]
    sql_text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class RetrieveAnnotationSegment:
    """Represents a retrieve annotation results operation"""
    table: str
    source_ref: str
    annotation_request_id: Optional[str]
    sql_text: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class SQLSegmentation:
    """Complete segmentation of a SQL script"""
    inputs: List[InputSegment]
    functions: List[FunctionSegment]
    loads: List[LoadSegment]
    tables: List[TableSegment]
    saves: List[SaveSegment]
    publishes: List[PublishSegment]
    publish_annotations: List[PublishAnnotationSegment]
    retrieve_annotations: List[RetrieveAnnotationSegment]
    total_lines: int
    has_macros: bool
    macro_placeholders: List[str]

    def get_segment_count(self) -> int:
        """Get total number of segments"""
        return (
            len(self.inputs) + len(self.functions) + len(self.loads) + len(self.tables) +
            len(self.saves) + len(self.publishes) +
            len(self.publish_annotations) + len(self.retrieve_annotations)
        )


class SQLSegmenter:
    """
    Segments SQL scripts from the structural surface parser output.
    """

    def __init__(
        self,
        sql_text: str,
        registered_functions: list[RegisteredRuntimeFunction | Mapping[str, object]] | None = None,
    ):
        self.sql_text = sql_text
        self.lines = sql_text.split('\n')
        self.registered_functions = list(registered_functions or [])

    def segment(self) -> SQLSegmentation:
        """Segment the SQL script using surface parser statements only."""
        items = TopLevelParser(self.sql_text).parse()

        inputs: list[InputSegment] = []
        functions: list[FunctionSegment] = []
        loads: list[LoadSegment] = []
        tables: list[TableSegment] = []
        saves: list[SaveSegment] = []
        publishes: list[PublishSegment] = []
        publish_annotations: list[PublishAnnotationSegment] = []
        retrieve_annotations: list[RetrieveAnnotationSegment] = []

        for item in items:
            if isinstance(item, DeclareInputStmt):
                inputs.append(self._input_to_segment(item))
            elif isinstance(item, SqlFunctionDefStmt):
                functions.append(self._function_to_segment(item))
            elif isinstance(item, LoadStmt):
                loads.append(self._load_to_segment(item))
            elif isinstance(item, (BatchTableStmt, StreamTableStmt)):
                tables.append(self._table_to_segment(item))
            elif isinstance(item, SaveStmt):
                saves.append(self._save_to_segment(item))
            elif isinstance(item, PublishReportsStmt):
                publishes.append(self._publish_to_segment(item, "REPORTS", None))
            elif isinstance(item, PublishDatasetStmt):
                publishes.append(self._publish_to_segment(item, "DATASET", item.dataset_name))
            elif isinstance(item, PublishAnnotationStmt):
                publish_annotations.append(self._publish_annotation_to_segment(item))
            elif isinstance(item, RetrieveAnnotationStmt):
                retrieve_annotations.append(self._retrieve_annotation_to_segment(item))

        # Detect macros
        macro_placeholders = self._find_macro_placeholders()

        return SQLSegmentation(
            inputs=inputs,
            functions=functions,
            loads=loads,
            tables=tables,
            saves=saves,
            publishes=publishes,
            publish_annotations=publish_annotations,
            retrieve_annotations=retrieve_annotations,
            total_lines=len(self.lines),
            has_macros=len(macro_placeholders) > 0,
            macro_placeholders=macro_placeholders,
        )

    def _input_to_segment(self, statement: DeclareInputStmt) -> InputSegment:
        return InputSegment(
            name=statement.name,
            input_type=statement.input_type,
            options=statement.options.to_dict(),
            default=_input_default_display(statement),
            environment=statement.environment,
            required=statement.default_sql is None,
            sql_text=statement.source_text,
        )

    def _function_to_segment(self, statement: SqlFunctionDefStmt) -> FunctionSegment:
        definition = statement.definition
        if definition is None:
            raise ValueError("SQL function statement is missing a definition")
        return FunctionSegment(
            name=definition.canonical_name,
            sql_text=statement.source_text,
        )

    def _load_to_segment(self, load: LoadStmt) -> LoadSegment:
        return LoadSegment(
            table=load.table,
            source=load.path,
            options=dict(load.options),
            sql_text=load.source_text,
        )

    def _table_to_segment(self, statement: BatchTableStmt | StreamTableStmt) -> TableSegment:
        phase_type = "BATCH" if isinstance(statement, BatchTableStmt) else "STREAM"
        return TableSegment(
            table=statement.name,
            phase_type=phase_type,
            batch_size=statement.batch_size,
            options=dict(statement.options),
            sql_text=statement.source_text,
            query_sql=_query_sql(statement.query),
            depends_on=_source_tables(statement.query),
        )

    def _save_to_segment(self, save: SaveStmt) -> SaveSegment:
        return SaveSegment(
            table=save.table,
            destination=save.path,
            options=dict(save.options),
            sql_text=save.source_text,
        )

    def _publish_to_segment(
        self,
        publish: PublishReportsStmt | PublishDatasetStmt,
        destination: str,
        published_name: Optional[str],
    ) -> PublishSegment:
        return PublishSegment(
            table=publish.table,
            destination=destination,
            published_name=published_name,
            component=publish.component if isinstance(publish, PublishReportsStmt) else None,
            chart_type=publish.chart_type if isinstance(publish, PublishReportsStmt) else None,
            report_options=publish.report_options if isinstance(publish, PublishReportsStmt) else {},
            sql_text=publish.source_text,
        )

    def _publish_annotation_to_segment(self, publish: PublishAnnotationStmt) -> PublishAnnotationSegment:
        return PublishAnnotationSegment(
            table=publish.table,
            queue_name=publish.queue_name,
            alias=publish.alias,
            options=dict(publish.options),
            sql_text=publish.source_text,
        )

    def _retrieve_annotation_to_segment(self, retrieve: RetrieveAnnotationStmt) -> RetrieveAnnotationSegment:
        return RetrieveAnnotationSegment(
            table=retrieve.table,
            source_ref=retrieve.source_ref,
            annotation_request_id=retrieve.annotation_request_id,
            sql_text=retrieve.source_text,
        )

    def _find_macro_placeholders(self) -> List[str]:
        """Find all $MACRO placeholders in the SQL"""
        import re
        pattern = r'\$([A-Z][A-Z0-9_]*)\b'
        matches = re.findall(pattern, self.sql_text)
        return sorted(list(set(matches)))


def segment_sql(
    sql_text: str,
    registered_functions: list[RegisteredRuntimeFunction | Mapping[str, object]] | None = None,
) -> SQLSegmentation:
    """
    Segment a SQL script into logical parts using sqlglot-based parsing.

    Args:
        sql_text: The SQL script to segment

    Returns:
        SQLSegmentation object containing all segments
    """
    segmenter = SQLSegmenter(sql_text, registered_functions=registered_functions)
    return segmenter.segment()


def _query_sql(query) -> str:
    if isinstance(query, SqlAstExpr):
        return query.expression.sql(dialect="spark")
    return ""


def _input_default_display(statement: DeclareInputStmt) -> Optional[str]:
    if statement.default_sql is None:
        return None
    default_sql = statement.default_sql.strip()
    if statement.input_type.upper() in {"AISYSTEM", "DATASET", "SECRET"} and len(default_sql) >= 2 and default_sql[0] == "'" and default_sql[-1] == "'":
        return default_sql[1:-1].replace("\\'", "'")
    return default_sql


def _source_tables(query) -> List[str]:
    if not isinstance(query, SqlAstExpr):
        return []
    discovered: list[str] = []
    seen: set[str] = set()
    for table in query.expression.find_all(exp.Table):
        table_sql = table.sql(dialect="spark")
        if table_sql not in seen:
            seen.add(table_sql)
            discovered.append(table_sql)
    return discovered
