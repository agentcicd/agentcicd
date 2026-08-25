from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Protocol

from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.ir.options import StatementOptions


@dataclass(frozen=True)
class BackendLayout:
    working_dir: str
    tables_root: str
    sources_root: str
    outputs_root: str
    publish_root: str
    checkpoints_root: str
    stream_batches_root: str
    http_cache_root: str
    annotation_tasks_root: str


class SourceLoader(Protocol):
    def load_dataframe(self, spark_session, path: str, options: StatementOptions):
        ...


class PublicationStore(Protocol):
    def publish_report(
        self,
        layout: BackendLayout,
        name: str,
        component: str,
        chart_type: str | None = None,
        report_options: dict[str, str] | None = None,
    ) -> None:
        ...

    def publish_dataset(self, layout: BackendLayout, name: str, dataset_name: str | None) -> None:
        ...

    def publish_annotation(
        self,
        layout: BackendLayout,
        name: str,
        queue_name: str,
        *,
        alias: str | None = None,
        options: StatementOptions | dict[str, object] | None = None,
    ) -> None:
        ...


class AnnotationStore(Protocol):
    def load_annotation_dataframe(self, spark_session, layout: BackendLayout, annotation_id: str):
        ...


class RuntimeFunctionInvoker(Protocol):
    def register(self, spark_session, definition: FunctionDefinitionIR) -> str:
        ...


def ensure_layout_roots(layout: BackendLayout) -> None:
    for root in [
        layout.tables_root,
        layout.sources_root,
        layout.outputs_root,
        layout.publish_root,
        layout.checkpoints_root,
        layout.stream_batches_root,
        layout.http_cache_root,
        layout.annotation_tasks_root,
    ]:
        if urlparse(root).scheme:
            continue
        Path(root).mkdir(parents=True, exist_ok=True)
