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
    assert [function.id for function in plan.registered_functions] == ["local.echo"]
    assert plan.registered_functions[0].entrypoint_name == "echo"
    assert plan.registered_functions[0].signature[-1].name == "pool"
    assert plan.registered_functions[0].signature[-1].type_sql == "POOL"
    assert plan.registered_functions[0].pool_kind == "service"


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


def test_local_fixture_runtime_attaches_referenced_builtin_service_function(tmp_path: Path) -> None:
    project = _write_fixture_project(tmp_path)
    (project / "recipe.sql").write_text(
        textwrap.dedent(
            """
            CREATE BATCH TABLE prepared
            SELECT aisystems.llm.chat(
              aisystem_id = 'openai/gpt-4.1-mini',
              messages = parse_json('[]')
            ) AS response_raw;
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    spec = load_project(project)

    with local_fixture_runtime(spec) as runtime:
        functions = {function.name: function for function in runtime.registered_functions}

    assert "aisystems.llm.chat" in functions
    assert functions["aisystems.llm.chat"].base_url
    assert functions["aisystems.llm.chat"].invoke_path == "/invoke/chat"


def test_local_fixture_runtime_starts_for_builtin_service_function_without_user_fixtures(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text(
        textwrap.dedent(
            """
            CREATE BATCH TABLE prepared
            SELECT aisystems.llm.chat(
              aisystem_id = 'openai/gpt-4.1-mini',
              messages = parse_json('[]')
            ) AS response_raw;
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    spec = load_project(project)

    with local_fixture_runtime(spec) as runtime:
        names = [function.name for function in runtime.registered_functions]

    assert names == ["aisystems.llm.chat"]


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
