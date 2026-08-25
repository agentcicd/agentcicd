from __future__ import annotations

import json
import textwrap
from pathlib import Path
from urllib.request import Request, urlopen

from agentcicd.project import load_project
from agentcicd.runtime.local_fixtures import build_fixture_runtime_plan, local_fixture_runtime
from agentcicd.runtime.local_runner import validate_project


def test_fixture_source_generates_registered_function(tmp_path: Path) -> None:
    project = _write_fixture_project(tmp_path)

    spec = load_project(project)
    plan = build_fixture_runtime_plan(spec)

    assert [function.call_name for function in plan.registered_functions] == ["local.echo"]
    assert plan.registered_functions[0].entrypoint_name == "echo"


def test_validate_project_resolves_fixture_source_function(tmp_path: Path) -> None:
    project = _write_fixture_project(tmp_path)

    spec = validate_project(project)

    assert spec.fixture_sources == ((project / "fixture_echo.py").resolve(),)


def test_local_fixture_runtime_invokes_through_sandbox_manager(tmp_path: Path) -> None:
    project = _write_fixture_project(tmp_path)
    spec = load_project(project)

    with local_fixture_runtime(spec) as runtime:
        function = runtime.registered_functions[0]
        request = Request(
            f"{function.base_url}{function.invoke_path}",
            data=json.dumps({"args": {"value": "hello"}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))

    assert payload == {"result": "hello!"}


def _write_fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text(
        "CREATE BATCH TABLE prepared SELECT local.echo('hello') AS value;\n",
        encoding="utf-8",
    )
    (project / "fixture_echo.py").write_text(
        textwrap.dedent(
            """
            from agentcicd import Str, function


            @function
            def echo(value: Str) -> Str:
                return f"{value}!"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return project
