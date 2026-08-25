from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types

import agentcicd.fixtures as agentcicd_fixtures
import agentcicd.fixtures.functions as fixture_functions
import pytest
from agentcicd.fixtures import validate_manifest


ROOT = Path(__file__).resolve().parents[4]
RUNTIME_PATH = ROOT / "agentcicd" / "src" / "agentcicd" / "sandbox" / "function_runner.py"


def _load_runtime_module():
    module_name = "agentcicd_function_runtime_under_test"
    spec = importlib.util.spec_from_file_location(module_name, RUNTIME_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _install_dummy_functions_module(monkeypatch, udf=object()) -> None:
    functions_module = types.ModuleType("agentcicd_fixtures.functions")
    functions_module.udf = udf
    monkeypatch.setitem(agentcicd_fixtures.__dict__, "functions", functions_module)
    monkeypatch.setitem(sys.modules, "agentcicd_fixtures.functions", functions_module)
    monkeypatch.setattr(fixture_functions, "udf", udf)


def test_function_runtime_supports_agentcicd_schema_imports(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import function, Variant


@function
def run(row: Variant) -> Variant:
    return {"value": row["value"] * 2, "status": "ok"}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    manifest = runtime.build_manifest()
    validate_manifest(manifest)
    assert manifest["functions"][0]["name"] == "run"
    assert manifest["functions"][0]["signature"]["parameters"][0]["type_sql"] == "VARIANT"

    result = asyncio.run(runtime.invoke_function("run", {"row": {"value": 3}}))
    assert result == {"value": 6, "status": "ok"}


def test_function_runtime_invokes_function_with_module_level_namedstruct(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from __future__ import annotations

from agentcicd import Array, Int, NamedStruct, Str, function


class Case(NamedStruct):
    case_id: Str
    value: Int


@function
def generate_cases() -> Array[Case]:
    cases: list[Case] = []
    cases.append(Case(case_id="case_1", value=7))
    return cases
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    manifest = runtime.build_manifest()
    validate_manifest(manifest)
    result = asyncio.run(runtime.invoke_function("generate_cases", {}))

    assert result == [{"case_id": "case_1", "value": 7}]


def test_function_runtime_loads_multiple_source_paths(tmp_path: Path, monkeypatch) -> None:
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first_path.write_text(
        """
from agentcicd import Str, Variant, function


@function
def check(task: Str) -> Variant:
    return {"task": task, "source": "first"}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    second_path.write_text(
        """
from agentcicd import Str, Variant, function


@function
def query(sql: Str) -> Variant:
    return {"sql": sql, "source": "second"}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATHS", json.dumps([str(first_path), str(second_path)]))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    manifest = runtime.build_manifest()
    assert {item["name"] for item in manifest["functions"]} == {"check", "query"}
    assert asyncio.run(runtime.invoke_function("check", {"task": "open"})) == {
        "task": "open",
        "source": "first",
    }
    assert asyncio.run(runtime.invoke_function("query", {"sql": "select 1"})) == {
        "sql": "select 1",
        "source": "second",
    }


def test_function_runtime_supports_agentcicd_fixtures_authoring_imports(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd.fixtures import Directory, SecretId, Session, Str, function


@function
def run(secret_id: SecretId, session: Session, entries: Directory) -> Str:
    return entries[0]["path"] + ":" + secret_id + ":" + session.workspace_dir.name
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    monkeypatch.setenv("AGENTCICD_FUNCTION_POOL_KIND", "session")
    monkeypatch.setenv("AGENTCICD_SESSION_WORKSPACE_DIR", str(tmp_path / "workspace"))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    manifest = runtime.build_manifest()
    validate_manifest(manifest)
    signature = manifest["functions"][0]["signature"]
    assert signature["parameters"][0]["type_sql"] == "STRING"
    assert len(signature["parameters"]) == 2
    assert signature["parameters"][1]["type_sql"].startswith("ARRAY<STRUCT<")
    assert manifest["functions"][0]["metadata"]["injected_parameters"] == [{"name": "session", "kind": "session"}]

    result = asyncio.run(runtime.invoke_function("run", {"secret_id": "secret.test", "entries": [{"path": "a.txt"}]}))
    assert result == "a.txt:secret.test:workspace"


def test_function_runtime_hydrates_typed_env_spec_arguments(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import AgentHarnessEnv, Variant, function


@function
def run(agent: AgentHarnessEnv) -> Variant:
    mcp = {"spec_type": "mcp", "kind": "stdio", "name": "ignored"}
    returned = agent.config.add_mcp("playwright", mcp)
    return {
        "same_object": returned is agent,
        "kind": agent["kind"],
        "mcps": agent["config"]["mcps"],
    }
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    spec = {
        "spec_type": "environment",
        "kind": "agent_harness",
        "env_id": "agent",
        "config": {"session_id": "agent", "workdir": "/tmp/workspace"},
    }
    result = asyncio.run(runtime.invoke_function("run", {"agent": json.dumps(spec)}))

    assert result == {
        "same_object": True,
        "kind": "agent_harness",
        "mcps": {"playwright": {"spec_type": "mcp", "kind": "stdio", "name": "playwright"}},
    }


def test_function_runtime_binds_secret_global(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd.fixtures import SecretId, Str, function, secrets


@function
def run(secret_id: SecretId) -> Str:
    return secrets.get(secret_id)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    result = asyncio.run(
        runtime.invoke_function(
            "run",
            {"secret_id": "secret.test"},
            [{"id": "secret.test", "value": "resolved-value"}],
        )
    )

    assert result == "resolved-value"


def test_function_runtime_filters_runtime_control_arguments(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import function, Variant


@function
def run(row: Variant) -> Variant:
    return {"value": row["value"] * 2, "status": "ok"}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    result = asyncio.run(
        runtime.invoke_function(
            "run",
            {
                "row": {"value": 3},
                "limiter": {"key": "fixture_ratelimit", "max_in_flight": 4},
            },
        )
    )

    assert result == {"value": 6, "status": "ok"}


def test_function_runtime_loads_builtin_udf_entrypoint(monkeypatch) -> None:
    calls = []
    spans = []

    async def _fake_builtin(**kwargs):
        calls.append(kwargs)
        return {"status": "completed", "final_output": kwargs["task"]}

    _install_dummy_functions_module(monkeypatch, udf=lambda name: _fake_builtin)
    monkeypatch.setenv("AGENTCICD_FUNCTION_BUILTIN_CALL_NAME", "envs.agent_harness.run_task")
    monkeypatch.setenv("AGENTCICD_FUNCTION_BUILTIN_ENTRYPOINT", "run_task")

    runtime = _load_runtime_module()
    runtime.load_builtin_function()
    from agentcicd.fixtures.core.tracing import use_runtime_trace

    class Trace:
        def span(self, name: str, attributes: dict[str, object] | None = None):
            class Span:
                def __enter__(self) -> None:
                    spans.append((name, dict(attributes or {})))

                def __exit__(self, exc_type, exc, traceback) -> bool:
                    return False

            return Span()

    with use_runtime_trace(Trace()):
        result = asyncio.run(
            runtime.invoke_function(
                "run_task",
                {
                    "env": {"session_id": "agent", "harness": "fake"},
                    "task": "ship it",
                    "pool": {"kind": "session"},
                },
            )
        )

    assert result == {"status": "completed", "final_output": "ship it"}
    assert calls == [
        {
            "env": {"session_id": "agent", "harness": "fake"},
            "task": "ship it",
            "pool": {"kind": "session"},
        }
    ]
    assert (
        "function.envs.agent_harness.run_task",
        {"function_name": "envs.agent_harness.run_task", "arg_count": 0, "kwarg_count": 3},
    ) in spans
    assert (
        "udf.envs.agent_harness.run_task",
        {"udf_name": "envs.agent_harness.run_task", "arg_count": 0, "kwarg_count": 3},
    ) in spans


def test_function_runtime_traces_nested_udf_calls(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import function, Variant, udf


@function
async def run(row: Variant) -> Variant:
    helper = udf("support.helper")
    return await helper(row=row)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    async def _fake_helper(**kwargs):
        return {"value": kwargs["row"]["value"] * 2}

    _install_dummy_functions_module(monkeypatch, udf=lambda name: _fake_helper)
    monkeypatch.syspath_prepend(str(ROOT / "agentcicd" / "src"))
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))

    runtime = _load_runtime_module()
    runtime.load_user_source()
    from agentcicd.fixtures.core.tracing import use_runtime_trace

    spans = []

    class Trace:
        def span(self, name: str, attributes: dict[str, object] | None = None):
            class Span:
                def __enter__(self) -> None:
                    spans.append((name, dict(attributes or {})))

                def __exit__(self, exc_type, exc, traceback) -> bool:
                    return False

            return Span()

    with use_runtime_trace(Trace()):
        result = asyncio.run(runtime.invoke_function("run", {"row": {"value": 21}}))

    assert result == {"value": 42}
    assert ("function.run", {"function_name": "run", "arg_count": 0, "kwarg_count": 1}) in spans
    assert ("udf.support.helper", {"udf_name": "support.helper", "arg_count": 0, "kwarg_count": 1}) in spans


def test_function_runtime_injects_session_for_session_pools(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import Session, function, Variant, objectstore


@function
async def run(session: Session, tree: Variant) -> Variant:
    return await objectstore.materialize(tree, target_dir=session.workspace_dir / 'inputs')
""".strip()
        + "\n",
        encoding="utf-8",
    )

    class Store:
        def get_bytes(self, uri: str) -> bytes:
            return {"object://task": b"payload"}[uri]

    object_store_module = types.ModuleType("agentcicd_dp_common.object_store")
    object_store_module.object_store_from_env = lambda: Store()
    dp_common_module = types.ModuleType("agentcicd_dp_common")
    pandas_module = types.ModuleType("pandas")
    pandas_module.Series = object
    pandas_module.DataFrame = object
    pyarrow_module = types.ModuleType("pyarrow")
    pyarrow_module.Array = object
    pyarrow_module.array = lambda values: values
    monkeypatch.setitem(sys.modules, "agentcicd_dp_common", dp_common_module)
    monkeypatch.setitem(sys.modules, "agentcicd_dp_common.object_store", object_store_module)
    monkeypatch.setitem(sys.modules, "pandas", pandas_module)
    monkeypatch.setitem(sys.modules, "pyarrow", pyarrow_module)
    monkeypatch.syspath_prepend(str(ROOT / "agentcicd" / "src"))
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))

    runtime = _load_runtime_module()
    runtime.load_user_source()
    from agentcicd.fixtures.core.tracing import use_runtime_trace

    class Trace:
        def __init__(self) -> None:
            self.spans: list[tuple[str, dict[str, object]]] = []

        def span(self, name: str, attributes: dict[str, object] | None = None):
            trace = self

            class Span:
                def __enter__(self) -> None:
                    trace.spans.append((name, dict(attributes or {})))

                def __exit__(self, exc_type, exc, traceback) -> bool:
                    return False

            return Span()

    monkeypatch.setenv("AGENTCICD_FUNCTION_POOL_KIND", "session")
    monkeypatch.setenv("AGENTCICD_SESSION_WORKSPACE_DIR", str(tmp_path / "workspace"))
    trace = Trace()
    with use_runtime_trace(trace):
        result = asyncio.run(
            runtime.invoke_function(
                "run",
                {
                    "tree": {"entries": [{"path": "task.txt", "object_uri": "object://task"}]},
                },
            )
        )

    assert result.target_dir == str(tmp_path / "workspace" / "inputs")
    assert [entry["path"] for entry in result.entries] == ["task.txt"]
    assert (tmp_path / "workspace" / "inputs" / "task.txt").read_bytes() == b"payload"
    assert ("function.run", {"function_name": "run", "arg_count": 0, "kwarg_count": 2}) in trace.spans
    assert ("objectstore.materialize", {"method": "materialize"}) in trace.spans


def test_function_runtime_rejects_session_outside_session_pools(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import Session, Str, function


@function
def run(session: Session) -> Str:
    return str(session.workspace_dir)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))
    monkeypatch.setenv("AGENTCICD_FUNCTION_POOL_KIND", "sandbox")
    _install_dummy_functions_module(monkeypatch)

    runtime = _load_runtime_module()
    runtime.load_user_source()

    with pytest.raises(RuntimeError, match="requires a session pool"):
        asyncio.run(runtime.invoke_function("run", {}))


def test_function_runtime_returns_remote_trace_records(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import function, Variant, udf


@function
async def run(row: Variant) -> Variant:
    helper = udf("support.helper")
    return await helper(row=row)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    async def _fake_helper(**kwargs):
        return {"value": kwargs["row"]["value"] * 3}

    _install_dummy_functions_module(monkeypatch, udf=lambda name: _fake_helper)
    monkeypatch.syspath_prepend(str(ROOT / "agentcicd" / "src"))
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))

    runtime = _load_runtime_module()
    runtime.load_user_source()

    with runtime._remote_runtime_trace_context(
        {
            "trace_id": "trace-123",
            "parent_span_id": "root-span",
            "parent_call_id": "rtcall_root",
        }
    ) as trace:
        result = asyncio.run(runtime.invoke_function("run", {"row": {"value": 7}}))

    assert result == {"value": 21}
    assert trace is not None
    records = trace.records()
    assert [record["name"] for record in records] == ["function.run", "udf.support.helper"]
    assert {record["trace_id"] for record in records} == {"trace-123"}
    assert records[0]["parent_span_id"] == "root-span"
    assert records[1]["parent_span_id"] == records[0]["span_id"]


def test_function_runtime_returns_trace_records_without_caller_trace_context(tmp_path: Path, monkeypatch) -> None:
    source_path = tmp_path / "function.py"
    source_path.write_text(
        """
from agentcicd import function, Variant


@function
def run(row: Variant) -> Variant:
    return {"value": row["value"] + 1}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    _install_dummy_functions_module(monkeypatch)
    monkeypatch.syspath_prepend(str(ROOT / "agentcicd" / "src"))
    monkeypatch.setenv("AGENTCICD_FUNCTION_SOURCE_PATH", str(source_path))

    runtime = _load_runtime_module()
    runtime.load_user_source()

    with runtime._remote_runtime_trace_context(None) as trace:
        result = asyncio.run(runtime.invoke_function("run", {"row": {"value": 7}}))

    assert result == {"value": 8}
    assert trace is not None
    records = trace.records()
    assert len(records) == 1
    assert records[0]["name"] == "function.run"
    assert records[0]["trace_id"].startswith("fixture-")
