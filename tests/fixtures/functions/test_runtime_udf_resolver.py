from __future__ import annotations

import asyncio
import json
import sys
import types

from agentcicd.fixtures.core.function import AsyncRowFunction, RowFunction
from agentcicd.fixtures.core.types import FType, JsonType
from agentcicd.fixtures.core.udf import Udf

import agentcicd.fixtures.functions
import agentcicd_fixtures.functions


class _SyncEchoFunction(RowFunction):
    calls = 0

    def transform(self, value):
        type(self).calls += 1
        return {"value": value, "calls": type(self).calls}


class _SyncEchoUdf(Udf, name="test.sync_echo"):
    def input_schema(self):
        return (JsonType(),)

    def input_args(self):
        return ("value",)

    def output_schema(self):
        return JsonType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return _SyncEchoFunction


class _AsyncEchoFunction(AsyncRowFunction):
    async def transform(self, value):
        return {"value": value}


class _AsyncEchoUdf(Udf, name="test.async_echo"):
    def input_schema(self):
        return (JsonType(),)

    def input_args(self):
        return ("value",)

    def output_schema(self):
        return JsonType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return _AsyncEchoFunction


class _FakeObjectStore:
    def __init__(self) -> None:
        self.payloads = {}

    def exists(self, ref: str) -> bool:
        return ref in self.payloads

    def put_bytes(self, ref: str, payload: bytes, content_type: str | None = None) -> None:
        self.payloads[ref] = {"payload": payload, "content_type": content_type}

    def get_bytes(self, ref: str) -> bytes:
        return self.payloads[ref]["payload"]


def setup_function():
    agentcicd_fixtures.functions._cached_udf.cache_clear()
    agentcicd_fixtures.functions._runtime_context.cache_clear()
    agentcicd_fixtures.functions._runtime_payload.cache_clear()
    _SyncEchoFunction.calls = 0


def test_udf_returns_cached_sync_row_callable() -> None:
    first = agentcicd_fixtures.functions.udf("test.sync_echo")
    second = agentcicd_fixtures.functions.udf("test.sync_echo")

    assert first is second
    assert first("a") == {"value": "a", "calls": 1}
    assert second(value="b") == {"value": "b", "calls": 2}


def test_udf_records_runtime_trace_span() -> None:
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

    call = agentcicd_fixtures.functions.udf("test.sync_echo")
    with use_runtime_trace(Trace()):
        assert call("a") == {"value": "a", "calls": 1}

    assert ("udf.test.sync_echo", {"udf_name": "test.sync_echo", "arg_count": 1, "kwarg_count": 0}) in spans


def test_udf_returns_async_row_callable_for_async_functions() -> None:
    call = agentcicd_fixtures.functions.udf("test.async_echo")

    assert asyncio.iscoroutinefunction(call)
    assert asyncio.run(call("a")) == {"value": "a"}


def test_udf_resolves_objectstore_upload_all_builtin(monkeypatch, tmp_path) -> None:
    import agentcicd.fixtures.functions.objectstore as objectstore

    store = _FakeObjectStore()
    answer_dir = tmp_path / "answer"
    answer_dir.mkdir()
    (answer_dir / "plot.png").write_bytes(b"png")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
    monkeypatch.setattr(objectstore, "object_store_from_env", lambda: store)

    upload_all = agentcicd_fixtures.functions.udf("objectstore.upload_all")
    entries = upload_all(str(answer_dir), "answer")

    assert [entry["dataset_path"] for entry in entries] == ["answer", "answer/plot.png"]
    assert entries[1]["content_type"] == "image/png"
    assert entries[1]["object_uri"] == "agentcicd-object://org.test/runs/run.test/attempt_1/artifacts/answer/plot.png"
    assert store.get_bytes(entries[1]["object_uri"]) == b"png"


def test_env_spec_config_add_mcp_mutates_spec_payload() -> None:
    agent = agentcicd_fixtures.functions.udf("envs.agent_harness.spec")(
        session_id="agent",
        aisystem="aisystem.test",
        workdir="/tmp/workspace",
    )
    mcp = agentcicd_fixtures.functions.udf("envs.mcp.stdio.spec")(
        name="ignored",
        command="playwright-mcp",
    )

    returned = agent.config.add_mcp("playwright", mcp)

    assert returned is agent
    assert agent["config"]["mcps"] == {"playwright": {**mcp, "name": "playwright"}}


def test_env_spec_config_add_mcp_accepts_materialized_mcp_handle(monkeypatch) -> None:
    from agentcicd.fixtures.functions.simulators import MaterializedMcpHandle, materialized_mcp_from_spec

    calls = []

    async def fake_call_tool(self, name, arguments=None):
        calls.append((self.name, name, arguments))
        return {"ok": True}

    monkeypatch.setattr(MaterializedMcpHandle, "call_tool", fake_call_tool)

    agent = agentcicd_fixtures.functions.udf("envs.agent_harness.spec")(
        session_id="agent",
        aisystem="aisystem.test",
        workdir="/tmp/workspace",
    )
    mcp = agentcicd_fixtures.functions.udf("envs.mcp.playwright.spec")(
        output_dir="/tmp/workspace/answer/artifacts/playwright",
    )
    handle = materialized_mcp_from_spec(mcp)
    asyncio.run(handle.screenshot("answer/fixture_artifacts/jupyter-final.png"))

    returned = agent.config.add_mcp("playwright", handle)

    assert returned is agent
    assert calls == [
        (
            "playwright",
            "browser_take_screenshot",
            {
                "filename": "answer/fixture_artifacts/jupyter-final.png",
                "fullPage": True,
            },
        )
    ]
    attached = agent["config"]["mcps"]["playwright"]
    assert attached["name"] == "playwright"
    assert attached["command"] == "playwright-mcp"
    assert attached["transport"] == "stdio"
    assert isinstance(attached["port"], int)


def test_materialized_playwright_mcp_handle_can_navigate_and_manage_tabs(monkeypatch) -> None:
    from agentcicd.fixtures.functions.simulators import MaterializedMcpHandle, materialized_mcp_from_spec

    calls = []

    async def fake_call_tool(self, name, arguments=None):
        calls.append((self.name, name, arguments))
        return {"ok": True}

    monkeypatch.setattr(MaterializedMcpHandle, "call_tool", fake_call_tool)

    mcp = agentcicd_fixtures.functions.udf("envs.mcp.playwright.spec")(
        output_dir="/tmp/workspace/answer/artifacts/playwright",
    )
    handle = materialized_mcp_from_spec(mcp)

    asyncio.run(handle.navigate("http://127.0.0.1:8888/lab/tree/answer/notebook.ipynb?token=agentcicd"))
    asyncio.run(handle.wait_for(text="File"))
    asyncio.run(handle.wait_for(time=2))
    asyncio.run(handle.tabs("select", index=1))

    assert calls == [
        (
            "playwright",
            "browser_navigate",
            {"url": "http://127.0.0.1:8888/lab/tree/answer/notebook.ipynb?token=agentcicd"},
        ),
        (
            "playwright",
            "browser_wait_for",
            {"text": "File"},
        ),
        (
            "playwright",
            "browser_wait_for",
            {"time": 2.0},
        ),
        (
            "playwright",
            "browser_tabs",
            {"action": "select", "index": 1},
        ),
    ]


def test_mcps_playwright_top_level_functions_resolve_public_spec(monkeypatch) -> None:
    from agentcicd.fixtures import mcps
    from agentcicd.fixtures.functions.simulators import MaterializedMcpHandle

    calls = []

    async def fake_call_tool(self, name, arguments=None):
        calls.append((self.name, name, arguments))
        return {"ok": True}

    monkeypatch.setattr(MaterializedMcpHandle, "call_tool", fake_call_tool)

    spec = mcps.playwright.spec(output_dir="/tmp/workspace/answer/artifacts/playwright")

    navigate = agentcicd_fixtures.functions.udf("mcps.playwright.browser.navigate")
    wait_for = agentcicd_fixtures.functions.udf("mcps.playwright.browser.wait_for")
    screenshot = agentcicd_fixtures.functions.udf("mcps.playwright.browser.screenshot")
    tabs = agentcicd_fixtures.functions.udf("mcps.playwright.browser.tabs")
    call_tool = agentcicd_fixtures.functions.udf("mcps.playwright.browser.call_tool")

    asyncio.run(navigate(spec, "https://example.test"))
    asyncio.run(wait_for(spec, text="Ready"))
    asyncio.run(screenshot(spec, path="answer/final.png", full_page=False))
    asyncio.run(tabs(spec, action="new", url="https://example.test/next"))
    asyncio.run(call_tool(spec, "browser_click", {"element": "Submit", "ref": "button-ref"}))

    assert calls == [
        ("playwright", "browser_navigate", {"url": "https://example.test"}),
        ("playwright", "browser_wait_for", {"text": "Ready"}),
        ("playwright", "browser_take_screenshot", {"filename": "answer/final.png", "fullPage": False}),
        ("playwright", "browser_tabs", {"action": "new", "url": "https://example.test/next"}),
        ("playwright", "browser_click", {"element": "Submit", "ref": "button-ref"}),
    ]


def test_new_agent_harness_and_mcps_udf_names_are_registered() -> None:
    names = agentcicd_fixtures.functions.load_builtin_udfs()

    assert "agent_harness.spec" in names
    assert "agent_harness.run_task" in names
    assert "mcps.spec" in names
    assert "mcps.stdio.spec" in names
    assert "mcps.playwright.spec" in names
    assert "mcps.playwright.browser.navigate" in names


def test_materialized_playwright_mcp_handle_exposes_tool_named_methods(monkeypatch) -> None:
    from agentcicd.fixtures.functions.simulators import MaterializedMcpHandle, materialized_mcp_from_spec

    calls = []

    async def fake_call_tool(self, name, arguments=None):
        calls.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(MaterializedMcpHandle, "call_tool", fake_call_tool)

    mcp = agentcicd_fixtures.functions.udf("envs.mcp.playwright.spec")()
    handle = materialized_mcp_from_spec(mcp)

    asyncio.run(handle.browser_navigate(url="https://example.test"))
    asyncio.run(handle.browser_take_screenshot(filename="final.png", fullPage=False))
    asyncio.run(handle.browser_click(element="Submit", ref="button-ref"))
    asyncio.run(handle.browser_tabs(action="new", url="https://example.test/next"))

    assert calls == [
        ("browser_navigate", {"url": "https://example.test"}),
        ("browser_take_screenshot", {"filename": "final.png", "fullPage": False}),
        ("browser_click", {"element": "Submit", "ref": "button-ref"}),
        ("browser_tabs", {"action": "new", "url": "https://example.test/next"}),
    ]


def test_agent_resolution_exposes_and_tears_down_attached_mcps(monkeypatch) -> None:
    from agentcicd.fixtures.functions.simulators import AgentHarnessEnvironmentHandle, MaterializedPlaywrightMcpHandle

    events = []

    class FakeExitStack:
        async def aclose(self):
            events.append(("teardown", "playwright"))

    async def fake_setup(self):
        events.append(("setup", self.name))
        self._exit_stack = FakeExitStack()
        return object()

    monkeypatch.setattr(MaterializedPlaywrightMcpHandle, "setup", fake_setup)

    handle = AgentHarnessEnvironmentHandle(
        env_id="agent",
        payload={
            "session_id": "agent",
            "aisystem": "aisystem.test",
            "workdir": "/tmp/workspace",
            "mcps": {
                "playwright": {
                    "spec_type": "mcp",
                    "transport": "stdio",
                    "name": "playwright",
                    "command": "playwright-mcp",
                    "start_mode": "early",
                }
            },
        },
    )

    asyncio.run(handle.setup_attached_mcps())
    asyncio.run(handle.teardown())
    asyncio.run(handle.teardown())

    assert "playwright" in handle.mcps
    assert events == [("setup", "playwright"), ("teardown", "playwright")]


def test_playwright_mcp_handle_exposes_http_agent_spec() -> None:
    from agentcicd.fixtures.functions.simulators import materialized_mcp_from_spec

    mcp = agentcicd_fixtures.functions.udf("envs.mcp.playwright.spec")(
        output_dir="/tmp/workspace/answer",
    )
    handle = materialized_mcp_from_spec(mcp)
    agent_spec = handle.to_agent_mcp_spec()

    assert handle.requires_setup_for_agent is True
    assert agent_spec["transport"] == "http"
    assert agent_spec["endpoint"].startswith("http://localhost:")
    assert agent_spec["endpoint"].endswith("/mcp")


def test_shell_spec_accepts_public_session_id_argument() -> None:
    shell = agentcicd_fixtures.functions.udf("envs.shell.spec")(
        session_id="python",
        cwd="/tmp/workspace",
        policy={"allowed_commands": ["python"]},
    )

    assert shell["spec_type"] == "environment"
    assert shell["kind"] == "shell"
    assert shell["env_id"] == "python"
    assert shell["config"]["cwd"] == "/tmp/workspace"
    assert shell["config"]["policy"] == {"allowed_commands": ["python"]}


def test_udf_accepts_optional_agentcicd_prefix_for_all_udfs() -> None:
    call = agentcicd_fixtures.functions.udf("agentcicd.test.sync_echo")

    assert call("a") == {"value": "a", "calls": 1}


def test_udf_resolves_async_remote_fixture_from_runtime_context(monkeypatch, tmp_path) -> None:
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "name": "support.helper",
                        "call_name": "support.helper",
                        "runtime_alias": "support_helper",
                        "base_url": "http://fixture-runtime",
                        "invoke_path": "/invoke/helper",
                        "async": True,
                        "signature": {"parameters": [{"name": "value"}]},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"result": {"ok": True}}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(agentcicd_fixtures.functions, "urlopen", fake_urlopen)

    call = agentcicd_fixtures.functions.udf("support.helper")

    assert asyncio.iscoroutinefunction(call)
    assert asyncio.run(call("x")) == {"ok": True}
    assert captured == {
        "url": "http://fixture-runtime/invoke/helper",
        "timeout": 900,
        "body": {"args": {"value": "x"}},
    }


def test_udf_dispatches_grouped_runtime_fixture_locally(monkeypatch, tmp_path) -> None:
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "fixture.local",
                        "name": "support.local",
                        "call_name": "support.local",
                        "runtime_alias": "support_local",
                        "entrypoint_name": "local",
                        "base_url": "http://fixture-runtime",
                        "invoke_path": "/invoke/local",
                        "signature": {"parameters": [{"name": "value"}]},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))
    monkeypatch.setenv("AGENTCICD_FUNCTION_GROUP_FIXTURE_IDS", json.dumps(["fixture.local"]))
    agentcicd_fixtures.functions._runtime_payload.cache_clear()
    agentcicd_fixtures.functions._cached_udf.cache_clear()

    calls: list[tuple[str, dict[str, object]]] = []
    sandbox_pkg = types.ModuleType("agentcicd_sandbox")
    function_runner = types.ModuleType("agentcicd_sandbox.function_runner")

    async def invoke_function(function_name, payload, secret_records=None):
        calls.append((function_name, dict(payload)))
        return {"local": True, "value": payload["value"]}

    function_runner.invoke_function = invoke_function
    monkeypatch.setitem(sys.modules, "agentcicd_sandbox", sandbox_pkg)
    monkeypatch.setitem(sys.modules, "agentcicd_sandbox.function_runner", function_runner)

    def unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("local nested dispatch should not make an HTTP call")

    monkeypatch.setattr(agentcicd_fixtures.functions, "urlopen", unexpected_urlopen)

    call = agentcicd_fixtures.functions.udf("support.local")

    assert call("x") == {"local": True, "value": "x"}
    assert calls == [("local", {"value": "x"})]
