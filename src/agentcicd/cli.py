from __future__ import annotations

import time
import webbrowser
from enum import Enum
from pathlib import Path

import typer

from agentcicd.config import BackendName
from agentcicd.errors import AgentCICDError
from agentcicd.runtime.local_runner import RunResult, prepare_run, run_prepared, run_project, validate_project
from agentcicd.ui_server import serve_local_inspection, start_local_inspection_server


app = typer.Typer(help="Run AgentCICD Engine projects locally.")
ui_app = typer.Typer(help="Inspect AgentCICD projects and local run artifacts.")
app.add_typer(ui_app, name="ui")


class UiMode(str, Enum):
    AUTO = "auto"
    OFF = "off"


@app.command()
def validate(project_dir: Path = typer.Argument(..., help="AgentCICD project directory.")) -> None:
    try:
        spec = validate_project(project_dir)
    except AgentCICDError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Validated {spec.paths.root}")


@app.command()
def run(
    project_dir: Path = typer.Argument(..., help="AgentCICD project directory."),
    backend: BackendName | None = typer.Option(None, "--backend", help="Execution backend."),
    ui: UiMode = typer.Option(UiMode.AUTO, "--ui", help="Start the local inspection UI or disable it."),
    open_browser: bool = typer.Option(False, "--open", help="Open the local inspection URL in a browser."),
) -> None:
    if ui == UiMode.OFF:
        _run_without_ui(project_dir, backend)
        return
    try:
        prepared = prepare_run(project_dir, backend=backend)
    except AgentCICDError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if prepared.backend == BackendName.VALIDATE:
        _run_without_ui(project_dir, backend)
        return
    with start_local_inspection_server(project_dir) as server:
        run_url = server.run_url(prepared.run_dir.name)
        typer.echo(f"Inspect this run: {run_url}")
        if open_browser:
            webbrowser.open(run_url)
        error: AgentCICDError | None = None
        try:
            result = run_prepared(prepared)
        except AgentCICDError as exc:
            error = exc
            typer.echo(f"Run failed: {exc}", err=True)
            result = None
        if result is not None:
            _print_run_result(result)
            typer.echo(f"Inspect this run: {run_url}")
        typer.echo("Inspector is running. Press Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    if error is not None:
        raise typer.Exit(code=1)


def _run_without_ui(project_dir: Path, backend: BackendName | None) -> None:
    try:
        result = run_project(project_dir, backend=backend)
    except AgentCICDError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _print_run_result(result)


def _print_run_result(result: RunResult) -> None:
    typer.echo(f"Run completed with backend {result.backend.value}: {result.run_dir}")
    if result.report_summary is not None:
        typer.echo(
            "Report: "
            f"{result.report_summary.metrics_count} metrics, "
            f"{result.report_summary.issues_count} issues, "
            f"{result.report_summary.charts_count} charts"
        )


@ui_app.command("serve")
def ui_serve(
    project_dir: Path = typer.Argument(..., help="AgentCICD project directory."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Loopback port, or 0 to choose one."),
) -> None:
    try:
        serve_local_inspection(project_dir, port=port)
    except AgentCICDError as exc:
        raise typer.BadParameter(str(exc)) from exc


@ui_app.command("open")
def ui_open(
    run_dir: Path = typer.Argument(..., help="Path to a local .agentcicd run directory."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Loopback port, or 0 to choose one."),
) -> None:
    resolved_run_dir = run_dir.expanduser().resolve()
    try:
        project_dir = resolved_run_dir.parents[2]
    except IndexError as exc:
        raise typer.BadParameter("Run directory must be under <project>/.agentcicd/runs/<run-id>") from exc
    with start_local_inspection_server(project_dir, port=port) as server:
        url = server.run_url(resolved_run_dir.name)
        typer.echo(f"Inspect this run: {url}")
        webbrowser.open(url)
        typer.echo("Inspector is running. Press Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return


def main() -> None:
    app()
