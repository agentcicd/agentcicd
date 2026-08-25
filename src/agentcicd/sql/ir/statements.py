from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .expressions import ExprIR
from .functions import FunctionDefinitionIR
from .options import StatementOptions


@dataclass
class StatementIR:
    source_text: str = ""


@dataclass
class BatchTableStmt(StatementIR):
    name: str = ""
    query: Optional[ExprIR] = None
    query_source_text: str = ""
    batch_size: Optional[int] = None
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass
class StreamTableStmt(StatementIR):
    name: str = ""
    query: Optional[ExprIR] = None
    query_source_text: str = ""
    batch_size: Optional[int] = None
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass
class QueryStmt(StatementIR):
    query: Optional[ExprIR] = None


@dataclass
class DeclareInputStmt(StatementIR):
    name: str = ""
    input_type: str = ""
    options: StatementOptions = field(default_factory=StatementOptions)
    default_sql: Optional[str] = None
    environment: Optional[str] = None


@dataclass
class LoadStmt(StatementIR):
    table: str = ""
    path: str = ""
    options: StatementOptions = field(default_factory=StatementOptions)
    limit: Optional[int] = None


@dataclass
class SaveStmt(StatementIR):
    table: str = ""
    path: str = ""
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass
class PublishReportsStmt(StatementIR):
    table: str = ""
    component: str = "metric"
    chart_type: Optional[str] = None
    report_options: dict[str, str] = field(default_factory=dict)


@dataclass
class PublishDatasetStmt(StatementIR):
    table: str = ""
    dataset_name: Optional[str] = None


@dataclass
class PublishAnnotationStmt(StatementIR):
    table: str = ""
    queue_name: str = ""
    alias: Optional[str] = None
    options: StatementOptions = field(default_factory=StatementOptions)


@dataclass
class RetrieveAnnotationStmt(StatementIR):
    table: str = ""
    source_ref: str = ""
    annotation_request_id: Optional[str] = None


@dataclass
class SqlFunctionDefStmt(StatementIR):
    definition: Optional[FunctionDefinitionIR] = None
