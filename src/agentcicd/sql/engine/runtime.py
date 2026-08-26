from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import time
from typing import Any, Callable, Mapping, Optional, Protocol

from agentcicd.sql.engine.annotation_store import AnnotationResultsPending
from agentcicd.sql.engine.plan import (
    DeclareVariableStepPayload,
    DefinitionStepPayload,
    ExecutionPlanStep,
    LoadTableStepPayload,
    PublishAnnotationStepPayload,
    PublishDatasetStepPayload,
    PublishReportStepPayload,
    RetrieveAnnotationStepPayload,
    SaveTableStepPayload,
    SqlStepPayload,
    StreamTableStepPayload,
    _step_node_name,
    payload_to_dict,
)
from agentcicd.sql.contracts import ProgressCallbackEvent
from agentcicd.sql.ir.functions import FunctionDefinitionIR
from agentcicd.sql.ir.options import StatementOptions


class ExecutionBackend(Protocol):
    """Execution backend contract.

    Registration and variable declaration methods are setup-only and are called sequentially before
    DAG stage execution. Materialization methods (`load_table`, `create_batch_table`, and
    `create_stream_table`) may run concurrently for dependency-independent stages when
    `execute_plan_dag(..., max_parallel_stages>1)` is used. Publication and retrieval methods run
    according to normal dependency ordering and should guard any shared mutable state they touch.
    """

    def declare_variable(self, name: str, sql: str) -> None: ...

    def register_sql_function(self, name: str, definition: FunctionDefinitionIR) -> None: ...

    def register_runtime_function(self, name: str, definition: FunctionDefinitionIR) -> None: ...

    def create_batch_table(
        self,
        name: str,
        sql: str,
        *,
        options: StatementOptions | Mapping[str, object] | None = None,
    ) -> None: ...

    def create_stream_table(
        self,
        name: str,
        sql: str,
        *,
        source_tables: list[str] | None = None,
        batch_size: int | None = None,
        options: StatementOptions | Mapping[str, object] | None = None,
    ) -> None: ...

    def load_table(
        self,
        name: str,
        path: str,
        options: StatementOptions,
        *,
        wrap_cells: bool = False,
        limit: int | None = None,
    ) -> None: ...

    def save_table(self, name: str, path: str, options: StatementOptions) -> None: ...

    def publish_report(
        self,
        name: str,
        component: str,
        chart_type: str | None = None,
        report_options: dict[str, str] | None = None,
    ) -> None: ...

    def publish_dataset(self, name: str, dataset_name: str | None) -> None: ...

    def publish_annotation(
        self,
        name: str,
        queue_name: str,
        *,
        alias: str | None = None,
        options: StatementOptions | Mapping[str, object] | None = None,
    ) -> None: ...

    def retrieve_annotation(self, name: str, source_ref: str, *, wrap_cells: bool = False) -> None: ...


@dataclass
class ExecutionEvent:
    step_kind: str
    step_name: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionReport:
    events: list[ExecutionEvent] = field(default_factory=list)
    failed_step_kind: str | None = None
    failed_step_name: str | None = None
    error: str | None = None


def execute_plan(
    plan: list[ExecutionPlanStep],
    backend: ExecutionBackend,
    progress_callback: Optional[Callable[[ProgressCallbackEvent], None]] = None,
    *,
    raise_on_error: bool = True,
    wait_for_annotations: bool = False,
    annotation_poll_seconds: float = 1.0,
) -> ExecutionReport:
    report = ExecutionReport()
    context_hook = getattr(backend, "set_execution_plan_context", None)
    if callable(context_hook):
        context_hook(plan)
    for step in plan:
        step_payload = payload_to_dict(step.payload)
        if _should_skip_step(backend, step):
            report.events.append(
                ExecutionEvent(
                    step_kind=step.kind,
                    step_name=step.name,
                    status="skipped",
                    payload={"reason": "materialized_table_reused"},
                )
            )
            continue
        if progress_callback is not None:
            progress_callback(
                ProgressCallbackEvent(
                    step_type=step.kind,
                    step_name=step.name,
                    status="started",
                    metadata={"dependencies": list(step.dependencies)},
                )
            )
        report.events.append(
            ExecutionEvent(
                step_kind=step.kind,
                step_name=step.name,
                status="started",
                payload={"dependencies": list(step.dependencies)},
            )
        )
        try:
            _execute_step_waiting_for_annotation(
                step,
                backend,
                progress_callback=progress_callback,
                wait_for_annotations=wait_for_annotations,
                annotation_poll_seconds=annotation_poll_seconds,
            )
        except AnnotationResultsPending as exc:
            if step.kind != "retrieve_annotation":
                raise
            metadata = {
                "action": "wait_for_annotation",
                "annotation_request_id": exc.annotation_id,
                "source_ref": str(step_payload.get("source_ref") or exc.annotation_id),
                "target_table": step.name,
            }
            if progress_callback is not None:
                progress_callback(
                    ProgressCallbackEvent(
                        step_type=step.kind,
                        step_name=step.name,
                        status="waiting",
                        metadata=metadata,
                    )
                )
            report.events.append(
                ExecutionEvent(
                    step_kind=step.kind,
                    step_name=step.name,
                    status="waiting",
                    payload=metadata,
                )
            )
            return report
        except Exception as exc:
            if progress_callback is not None:
                progress_callback(
                    ProgressCallbackEvent(
                        step_type=step.kind,
                        step_name=step.name,
                        status="failed",
                        error=str(exc),
                    )
                )
            report.failed_step_kind = step.kind
            report.failed_step_name = step.name
            report.error = str(exc)
            report.events.append(
                ExecutionEvent(
                    step_kind=step.kind,
                    step_name=step.name,
                    status="failed",
                    payload={"error": str(exc)},
                )
            )
            if raise_on_error:
                raise
            return report
        if progress_callback is not None:
            completion_metadata = dict(step_payload)
            metadata_hook = getattr(backend, "step_completion_metadata", None)
            if callable(metadata_hook):
                completion_metadata.update(dict(metadata_hook(step) or {}))
            progress_callback(
                ProgressCallbackEvent(
                    step_type=step.kind,
                    step_name=step.name,
                    status="completed",
                    metadata=completion_metadata,
                )
            )
        report.events.append(
            ExecutionEvent(
                step_kind=step.kind,
                step_name=step.name,
                status="completed",
                payload=step_payload,
            )
        )
    return report


def execute_plan_dag(
    plan: list[ExecutionPlanStep],
    backend: ExecutionBackend,
    progress_callback: Optional[Callable[[ProgressCallbackEvent], None]] = None,
    *,
    max_parallel_stages: int = 1,
    raise_on_error: bool = True,
    wait_for_annotations: bool = False,
    annotation_poll_seconds: float = 1.0,
) -> ExecutionReport:
    """Execute a dependency DAG with bounded parallel materialization.

    Setup steps run sequentially before the thread pool starts. After that, a step is runnable only
    when all declared dependencies have completed. Backend implementations must keep shared mutable
    state behind thread-safe service APIs because dependency-independent stages can execute on
    different worker threads. A failed node marks pending descendants as blocked and returns their
    dependency metadata when `raise_on_error` is false.
    """

    if max_parallel_stages <= 1:
        return execute_plan(
            plan,
            backend,
            progress_callback=progress_callback,
            raise_on_error=raise_on_error,
            wait_for_annotations=wait_for_annotations,
            annotation_poll_seconds=annotation_poll_seconds,
        )

    report = ExecutionReport()
    context_hook = getattr(backend, "set_execution_plan_context", None)
    if callable(context_hook):
        context_hook(plan)

    setup_kinds = {"declare_variable", "register_sql_function", "register_runtime_function"}
    setup_nodes: set[str] = set()
    remaining_steps: list[ExecutionPlanStep] = []
    for step in plan:
        if step.kind in setup_kinds:
            _execute_step_with_events(
                step,
                backend,
                report,
                progress_callback,
                wait_for_annotations=wait_for_annotations,
                annotation_poll_seconds=annotation_poll_seconds,
            )
            setup_nodes.add(_step_node_name(step))
        else:
            remaining_steps.append(step)

    node_to_step = {_step_node_name(step): step for step in remaining_steps}
    completed: set[str] = set(setup_nodes)
    failed: set[str] = set()
    running: dict[Any, str] = {}
    pending = set(node_to_step)
    dependents: dict[str, set[str]] = {node: set() for node in node_to_step}
    dependencies = {
        node: {dependency for dependency in step.dependencies if dependency in node_to_step or dependency in setup_nodes}
        for node, step in node_to_step.items()
    }
    for node, deps in dependencies.items():
        for dependency in deps:
            dependents.setdefault(dependency, set()).add(node)

    def blocked_descendants(seed: str) -> set[str]:
        blocked: set[str] = set()
        stack = list(dependents.get(seed, set()))
        while stack:
            node = stack.pop()
            if node in blocked:
                continue
            blocked.add(node)
            stack.extend(dependents.get(node, set()))
        return blocked

    with ThreadPoolExecutor(max_workers=max(1, max_parallel_stages)) as executor:
        while pending or running:
            runnable = sorted(
                [
                    node
                    for node in pending
                    if dependencies.get(node, set()).issubset(completed)
                ],
                key=lambda node: list(node_to_step).index(node),
            )
            while runnable and len(running) < max_parallel_stages:
                node = runnable.pop(0)
                pending.remove(node)
                step = node_to_step[node]
                future = executor.submit(
                    _execute_step_with_events,
                    step,
                    backend,
                    report,
                    progress_callback,
                    wait_for_annotations=wait_for_annotations,
                    annotation_poll_seconds=annotation_poll_seconds,
                )
                running[future] = node

            if not running:
                blocked = sorted(pending)
                message = f"No runnable DAG nodes remain; blocked nodes: {', '.join(blocked)}"
                report.error = message
                if raise_on_error:
                    raise RuntimeError(message)
                return report

            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                node = running.pop(future)
                try:
                    future.result()
                except Exception as exc:
                    failed.add(node)
                    report.failed_step_kind = node_to_step[node].kind
                    report.failed_step_name = node_to_step[node].name
                    report.error = str(exc)
                    for blocked_node in sorted(blocked_descendants(node) & pending):
                        pending.remove(blocked_node)
                        blocked_step = node_to_step[blocked_node]
                        metadata = {"reuse_state": "blocked", "blocked_by": node}
                        report.events.append(
                            ExecutionEvent(
                                step_kind=blocked_step.kind,
                                step_name=blocked_step.name,
                                status="blocked",
                                payload=metadata,
                            )
                        )
                        if progress_callback is not None:
                            progress_callback(
                                ProgressCallbackEvent(
                                    step_type=blocked_step.kind,
                                    step_name=blocked_step.name,
                                    status="failed",
                                    error=f"Blocked by failed dependency {node}",
                                    metadata=metadata,
                                )
                            )
                    if raise_on_error:
                        raise
                    return report
                else:
                    completed.add(node)
    return report


def _execute_step_with_events(
    step: ExecutionPlanStep,
    backend: ExecutionBackend,
    report: ExecutionReport,
    progress_callback: Optional[Callable[[ProgressCallbackEvent], None]],
    *,
    wait_for_annotations: bool = False,
    annotation_poll_seconds: float = 1.0,
) -> None:
    step_payload = payload_to_dict(step.payload)
    if _should_skip_step(backend, step):
        metadata = {"reason": "materialized_table_reused", "reuse_state": "reused"}
        report.events.append(ExecutionEvent(step_kind=step.kind, step_name=step.name, status="skipped", payload=metadata))
        if progress_callback is not None:
            progress_callback(
                ProgressCallbackEvent(
                    step_type=step.kind,
                    step_name=step.name,
                    status="completed",
                    metadata=metadata,
                )
            )
        return
    if progress_callback is not None:
        progress_callback(
            ProgressCallbackEvent(
                step_type=step.kind,
                step_name=step.name,
                status="started",
                metadata={"dependencies": list(step.dependencies)},
            )
        )
    report.events.append(ExecutionEvent(step_kind=step.kind, step_name=step.name, status="started", payload={"dependencies": list(step.dependencies)}))
    try:
        _execute_step_waiting_for_annotation(
            step,
            backend,
            progress_callback=progress_callback,
            wait_for_annotations=wait_for_annotations,
            annotation_poll_seconds=annotation_poll_seconds,
        )
    except Exception as exc:
        if progress_callback is not None:
            progress_callback(ProgressCallbackEvent(step_type=step.kind, step_name=step.name, status="failed", error=str(exc)))
        report.events.append(ExecutionEvent(step_kind=step.kind, step_name=step.name, status="failed", payload={"error": str(exc)}))
        raise
    completion_metadata = dict(step_payload)
    metadata_hook = getattr(backend, "step_completion_metadata", None)
    if callable(metadata_hook):
        completion_metadata.update(dict(metadata_hook(step) or {}))
    if progress_callback is not None:
        progress_callback(
            ProgressCallbackEvent(
                step_type=step.kind,
                step_name=step.name,
                status="completed",
                metadata=completion_metadata,
            )
        )
    report.events.append(ExecutionEvent(step_kind=step.kind, step_name=step.name, status="completed", payload=completion_metadata))


def _execute_step_waiting_for_annotation(
    step: ExecutionPlanStep,
    backend: ExecutionBackend,
    *,
    progress_callback: Optional[Callable[[ProgressCallbackEvent], None]],
    wait_for_annotations: bool,
    annotation_poll_seconds: float,
) -> None:
    if not wait_for_annotations or step.kind != "retrieve_annotation":
        _execute_step(step, backend)
        return
    step_payload = payload_to_dict(step.payload)
    emitted_waiting = False
    while True:
        try:
            _execute_step(step, backend)
            return
        except AnnotationResultsPending as exc:
            if not emitted_waiting and progress_callback is not None:
                progress_callback(
                    ProgressCallbackEvent(
                        step_type=step.kind,
                        step_name=step.name,
                        status="waiting",
                        metadata={
                            "action": "wait_for_annotation",
                            "annotation_request_id": exc.annotation_id,
                            "source_ref": str(step_payload.get("source_ref") or exc.annotation_id),
                            "target_table": step.name,
                        },
                    )
                )
            emitted_waiting = True
            time.sleep(max(0.1, float(annotation_poll_seconds or 1.0)))


def _should_skip_step(backend: ExecutionBackend, step: ExecutionPlanStep) -> bool:
    materialized_skip_hook = getattr(backend, "should_skip_materialized_stage", None)
    if callable(materialized_skip_hook) and materialized_skip_hook(step):
        return True
    skip_hook = getattr(backend, "should_skip_step", None)
    return bool(callable(skip_hook) and skip_hook(step))


def _execute_step(step: ExecutionPlanStep, backend: ExecutionBackend) -> None:
    if step.kind == "declare_variable":
        payload = _expect_payload(step.payload, DeclareVariableStepPayload, step.kind)
        backend.declare_variable(step.name, payload.sql)
        return
    if step.kind == "register_sql_function":
        payload = _expect_payload(step.payload, DefinitionStepPayload, step.kind)
        backend.register_sql_function(step.name, payload.definition)
        return
    if step.kind == "register_runtime_function":
        payload = _expect_payload(step.payload, DefinitionStepPayload, step.kind)
        backend.register_runtime_function(step.name, payload.definition)
        return
    if step.kind == "create_batch_table":
        payload = _expect_payload(step.payload, SqlStepPayload, step.kind)
        backend.create_batch_table(step.name, payload.sql, options=payload.options)
        return
    if step.kind == "create_stream_table":
        payload = _expect_payload(step.payload, StreamTableStepPayload, step.kind)
        backend.create_stream_table(
            step.name,
            payload.sql,
            source_tables=list(payload.source_tables),
            batch_size=payload.batch_size,
            options=payload.options,
        )
        return
    if step.kind == "load_table":
        payload = _expect_payload(step.payload, LoadTableStepPayload, step.kind)
        backend.load_table(
            step.name,
            payload.path,
            payload.options,
            wrap_cells=payload.wrap_cells,
            limit=payload.limit,
        )
        return
    if step.kind == "save_table":
        payload = _expect_payload(step.payload, SaveTableStepPayload, step.kind)
        backend.save_table(step.name, payload.path, payload.options)
        return
    if step.kind == "publish_report":
        payload = _expect_payload(step.payload, PublishReportStepPayload, step.kind)
        if payload.report_options and payload.component.strip().lower() == "chart":
            backend.publish_report(step.name, payload.component, payload.chart_type, payload.report_options)
        else:
            backend.publish_report(step.name, payload.component, payload.chart_type)
        return
    if step.kind == "publish_dataset":
        payload = _expect_payload(step.payload, PublishDatasetStepPayload, step.kind)
        backend.publish_dataset(step.name, payload.dataset_name)
        return
    if step.kind == "publish_annotation":
        payload = _expect_payload(step.payload, PublishAnnotationStepPayload, step.kind)
        backend.publish_annotation(
            step.name,
            payload.queue_name,
            alias=payload.alias,
            options=payload.options,
        )
        return
    if step.kind == "retrieve_annotation":
        payload = _expect_payload(step.payload, RetrieveAnnotationStepPayload, step.kind)
        backend.retrieve_annotation(
            step.name,
            payload.source_ref,
            wrap_cells=payload.wrap_cells,
        )
        return
    raise ValueError(f"Unsupported execution step kind '{step.kind}'")


def _expect_payload(payload, expected_type, step_kind: str):
    if not isinstance(payload, expected_type):
        raise TypeError(f"Step '{step_kind}' expected payload '{expected_type.__name__}'")
    return payload
