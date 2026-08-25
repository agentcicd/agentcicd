from __future__ import annotations

import asyncio
import sys
import types

import pytest

from agentcicd.fixtures import objectstore
from agentcicd.fixtures.core.function import AsyncRowFunction
from agentcicd.fixtures.core.types import ArrayType, FType, FunctionType, JsonType, StringType
from agentcicd.fixtures.core.udf import Param, Udf
from agentcicd.fixtures.functions.simulators import (
    DefaultEnvironmentProvider,
    EnvsAgentHarnessSpecUdf,
    EnvsBrowserSpecUdf,
    EnvsMcpHttpSpecUdf,
    EnvsMcpPlaywrightSpecUdf,
    EnvsMcpStdioSpecUdf,
    EnvsShellSpecUdf,
    EnvironmentSpec,
    LazyEnvironmentHandle,
    RuntimeEnvironments,
    RuntimeEnvironmentEntry,
    SimulatorLimitsUdf,
    SimulatorObserverUdf,
    SimulatorRunRowFunction,
    SimulatorRunUdf,
)


class _FakeSession:
    def __init__(self, env_id: str) -> None:
        self.env_id = env_id
        self.session_id = f"session-{env_id}"
        self.teardown_reasons: list[str] = []

    async def teardown(self, reason) -> None:
        self.teardown_reasons.append(reason.code)


class _FakeEnvironmentProvider:
    def __init__(self) -> None:
        self.setup_specs: list[EnvironmentSpec] = []
        self.sessions: list[_FakeSession] = []
        self.teardown_calls: list[tuple[str, str | None]] = []

    async def setup(self, specs):
        self.setup_specs = list(specs)
        runtime = RuntimeEnvironments()
        for spec in specs:
            session = _FakeSession(spec.env_id)
            self.sessions.append(session)
            runtime.entries[spec.env_id] = RuntimeEnvironmentEntry(
                kind=spec.kind,
                env_id=spec.env_id,
                spec=spec,
                session=session,
            )
        return runtime

    async def teardown(self, environments, reason_code, message=None):
        self.teardown_calls.append((reason_code, message))
        for entry in environments.entries.values():
            await entry.session.teardown(type("Reason", (), {"code": reason_code})())


class _FakeAsyncLimiter:
    def __init__(self) -> None:
        self.events: list[str] = []

    def acquire(self, *, permits: int = 1):
        limiter = self

        class _Lease:
            async def __aenter__(self):
                limiter.events.append(f"acquire:{permits}")

            async def __aexit__(self, exc_type, exc, traceback) -> bool:
                limiter.events.append("release")
                return False

        return _Lease()


def test_objectstore_materialize_records_runtime_trace_spans(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    class Store:
        def get_bytes(self, uri: str) -> bytes:
            return {"object://task": b"payload"}[uri]

    object_store_module = types.ModuleType("agentcicd_dp_common.object_store")
    object_store_module.object_store_from_env = lambda: Store()
    dp_common_module = types.ModuleType("agentcicd_dp_common")
    monkeypatch.setitem(sys.modules, "agentcicd_dp_common", dp_common_module)
    monkeypatch.setitem(sys.modules, "agentcicd_dp_common.object_store", object_store_module)

    with use_runtime_trace(Trace()):
        result = asyncio.run(
            objectstore.materialize(
                {"entries": [{"path": "task.txt", "object_uri": "object://task"}]},
                target_dir=tmp_path / "inputs",
            )
        )

    assert result.target_dir == str(tmp_path / "inputs")
    assert result.entries[0]["path"] == "task.txt"
    assert (tmp_path / "inputs" / "task.txt").read_bytes() == b"payload"
    assert ("objectstore.materialize", {"method": "materialize"}) in spans


def test_simulator_run_udf_metadata() -> None:
    udf = SimulatorRunUdf()
    input_schema = udf.input_schema()

    assert udf.input_args() == ("input", "user", "agent", "observers", "environments", "reuse", "limits", "limiter")
    assert isinstance(input_schema[0], JsonType)
    assert isinstance(input_schema[1], FunctionType)
    assert isinstance(input_schema[2], FunctionType)
    assert isinstance(input_schema[3], ArrayType)
    assert isinstance(input_schema[4], ArrayType)
    assert isinstance(input_schema[5], StringType)
    assert isinstance(input_schema[6], JsonType)
    assert isinstance(udf.output_schema(), JsonType)
    assert udf.metadata()["return_type_sql"].startswith("STRUCT<ok: BOOLEAN")
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), SimulatorRunRowFunction)


def test_spec_builder_udf_metadata() -> None:
    observer = SimulatorObserverUdf()
    limits = SimulatorLimitsUdf()
    envs_browser = EnvsBrowserSpecUdf()
    envs_shell = EnvsShellSpecUdf()
    envs_agent_harness = EnvsAgentHarnessSpecUdf()
    envs_mcp_http = EnvsMcpHttpSpecUdf()
    envs_mcp_stdio = EnvsMcpStdioSpecUdf()
    envs_mcp_playwright = EnvsMcpPlaywrightSpecUdf()

    assert observer.input_args() == ("callback", "schedule", "config")
    assert isinstance(observer.input_schema()[0], FunctionType)
    assert observer.metadata() == {"return_type_sql": "VARIANT", "pure": True}
    assert limits.input_args() == ("max_turns", "timeout_seconds")
    assert envs_browser.input_args()[0] == "session_id"
    assert envs_shell.input_args() == ("session_id", "cwd", "policy", "env")
    assert envs_agent_harness.input_args() == ("session_id", "aisystem", "workdir", "secret_id", "mcps")
    assert envs_mcp_http.input_args() == (
        "name",
        "endpoint",
        "required",
        "secret_id",
        "allow_tools",
        "deny_tools",
        "headers",
        "start_mode",
    )
    assert envs_mcp_stdio.input_args() == (
        "name",
        "command",
        "args",
        "required",
        "allow_tools",
        "deny_tools",
        "env",
        "default_tools_approval_mode",
        "start_mode",
    )
    assert envs_mcp_playwright.input_args() == (
        "output_dir",
        "name",
        "headless",
        "isolated",
        "allowed_hosts",
        "allow_tools",
        "deny_tools",
        "capture_final_screenshot",
        "final_screenshot_filename",
        "start_mode",
        "command",
        "args",
    )


def test_spec_builders_return_valid_json_specs() -> None:
    observer = SimulatorObserverUdf().function()().transform(
        "support.observer",
        ["after_turn", "final"],
        {"include": ["url"]},
    )
    limits = SimulatorLimitsUdf().function()().transform(max_turns=2, timeout_seconds=4.5)
    browser = EnvsBrowserSpecUdf().function()().transform(
        "browser",
        start_url="https://example.com",
        policy={"allow_uploads": True},
        viewport={"width": 800, "height": 600},
    )
    shell = EnvsShellSpecUdf().function()().transform("terminal", cwd="/tmp", env=[{"name": "A", "value": "B"}])
    stdio_mcp = EnvsMcpStdioSpecUdf().function()().transform(
        name="tools",
        command="tools-mcp",
        args=["--stdio"],
        required=True,
        allow_tools=["read"],
        deny_tools=["write"],
        env={"TOOLS_HOME": "/tmp/tools"},
        default_tools_approval_mode="approve",
    )
    playwright_mcp = EnvsMcpPlaywrightSpecUdf().function()().transform(
        output_dir="/tmp/workspace/answer/artifacts/playwright",
    )
    npx_playwright_mcp = EnvsMcpPlaywrightSpecUdf().function()().transform(
        command="npx",
        args=["@playwright/mcp@latest"],
        headless=False,
        isolated=False,
    )
    agent_harness = EnvsAgentHarnessSpecUdf().function()().transform(
        "agent",
        aisystem="aisystem.codex",
        workdir="/tmp/workspace",
        secret_id="secret.codex",
        mcps=[
            EnvsMcpHttpSpecUdf().function()().transform(
                name="playwright",
                endpoint="http://127.0.0.1:3000/sse",
                required=True,
                secret_id="secret.mcp",
                allow_tools=["browser_navigate"],
                deny_tools=["browser_install"],
                headers={"X-Test": "yes"},
            )
        ],
    )

    assert observer == {
        "spec_type": "simulator_observer",
        "callback": "support.observer",
        "schedule": ["after_turn", "final"],
        "config": {"include": ["url"]},
    }
    assert limits == {"spec_type": "simulator_limits", "max_turns": 2, "timeout_seconds": 4.5}
    assert browser["kind"] == "browser"
    assert browser["config"]["viewport"] == {"width": 800, "height": 600}
    assert shell["config"]["env"] == [{"name": "A", "value": "B"}]
    assert stdio_mcp == {
        "spec_type": "mcp",
        "transport": "stdio",
        "name": "tools",
        "command": "tools-mcp",
        "args": ["--stdio"],
        "required": True,
        "allow_tools": ["read"],
        "deny_tools": ["write"],
        "env": {"TOOLS_HOME": "/tmp/tools"},
        "default_tools_approval_mode": "approve",
        "start_mode": "lazy",
    }
    assert playwright_mcp["spec_type"] == "mcp"
    assert playwright_mcp["transport"] == "stdio"
    assert playwright_mcp["name"] == "playwright"
    assert playwright_mcp["command"] == "playwright-mcp"
    assert playwright_mcp["required"] is True
    assert playwright_mcp["deny_tools"] == ["browser_install"]
    assert playwright_mcp["default_tools_approval_mode"] == "approve"
    assert "--headless" in playwright_mcp["args"]
    assert "--isolated" in playwright_mcp["args"]
    assert "--no-sandbox" in playwright_mcp["args"]
    assert "--output-dir" in playwright_mcp["args"]
    assert "/tmp/workspace/answer/artifacts/playwright" in playwright_mcp["args"]
    assert playwright_mcp["playwright"] == {
        "output_dir": "/tmp/workspace/answer/artifacts/playwright",
        "capture_final_screenshot": False,
        "final_screenshot_filename": "fixture-final.png",
    }
    assert playwright_mcp["start_mode"] == "lazy"
    assert npx_playwright_mcp["command"] == "npx"
    assert npx_playwright_mcp["args"][0] == "@playwright/mcp@latest"
    assert "--headless" not in npx_playwright_mcp["args"]
    assert "--isolated" not in npx_playwright_mcp["args"]
    assert "--no-sandbox" in npx_playwright_mcp["args"]
    assert agent_harness == {
        "spec_type": "environment",
        "kind": "agent_harness",
        "env_id": "agent",
        "config": {
            "session_id": "agent",
            "aisystem": "aisystem.codex",
            "workdir": "/tmp/workspace",
            "secret_id": "secret.codex",
            "mcps": {
                "playwright": {
                    "spec_type": "mcp",
                    "transport": "http",
                    "name": "playwright",
                    "endpoint": "http://127.0.0.1:3000/sse",
                    "required": True,
                    "secret_id": "secret.mcp",
                    "allow_tools": ["browser_navigate"],
                    "deny_tools": ["browser_install"],
                    "headers": {"X-Test": "yes"},
                    "start_mode": "lazy",
                }
            },
        },
    }


def test_simulator_run_loops_until_user_terminates_and_preserves_state() -> None:
    provider = _FakeEnvironmentProvider()
    agent_calls: list[tuple[dict, dict, RuntimeEnvironments, int]] = []
    user_calls: list[tuple[dict, dict, RuntimeEnvironments, int]] = []

    async def agent_fn(request: dict, state: dict, environments: RuntimeEnvironments, turn: int) -> dict:
        agent_calls.append((request, state, environments, turn))
        return {
            "response": {"message": f"agent:{request['message']}"},
            "state": {"metadata": {"agent_turn": turn}, "turns": ["ignored"]},
        }

    async def user_fn(response: dict, state: dict, environments: RuntimeEnvironments, turn: int) -> dict:
        user_calls.append((response, state, environments, turn))
        return {
            "request": {"message": "done" if turn == 2 else "next"},
            "terminate": turn == 2,
            "state": {"metadata": {"user_turn": turn}, "observations": ["ignored"]},
        }

    result = asyncio.run(
        SimulatorRunRowFunction(provider).transform(
            {"message": "start"},
            user_fn,
            agent_fn,
            environments=[{"spec_type": "environment", "kind": "shell", "env_id": "terminal", "config": {}}],
            limits={"max_turns": 3, "timeout_seconds": 10},
        )
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["final_output"] == {"message": "done"}
    assert result["turns"] == [
        {
            "turn": 1,
            "request": {"message": "start"},
            "response": {"message": "agent:start"},
            "user_request": {"message": "next"},
            "terminate": False,
            "error": None,
        },
        {
            "turn": 2,
            "request": {"message": "next"},
            "response": {"message": "agent:next"},
            "user_request": {"message": "done"},
            "terminate": True,
            "error": None,
        },
    ]
    assert [call[3] for call in agent_calls] == [1, 2]
    assert user_calls[0][1]["turns"] == []
    assert user_calls[1][1]["turns"][0]["request"] == {"message": "start"}
    assert user_calls[0][2].require("terminal").env_id == "terminal"
    assert provider.teardown_calls == [("completed", None)]


def test_default_environment_provider_rejects_unknown_environment_kind() -> None:
    provider = DefaultEnvironmentProvider()

    with pytest.raises(ValueError, match="Unknown environment kind 'filesystem'"):
        asyncio.run(
            provider.setup(
                [
                    EnvironmentSpec(kind="filesystem", env_id="workspace", config={"root": "/tmp/a"}),
                ]
            )
        )


def test_simulator_run_defaults_missing_terminate_to_true() -> None:
    async def agent_fn(request, state, environments, turn):
        return {"response": {"ok": True}}

    async def user_fn(response, state, environments, turn):
        return {"request": {"final": response}}

    result = asyncio.run(SimulatorRunRowFunction().transform("start", user_fn, agent_fn))

    assert result["status"] == "completed"
    assert result["turns"][0]["terminate"] is True
    assert result["turns"][0]["user_request"] == {"final": {"ok": True}}


def test_simulator_run_uses_sql_visible_limiter() -> None:
    limiter = _FakeAsyncLimiter()

    async def agent_fn(request, state, environments, turn):
        return {"response": {"ok": True}}

    async def user_fn(response, state, environments, turn):
        return {"request": response}

    result = asyncio.run(SimulatorRunRowFunction().transform("start", user_fn, agent_fn, limiter=limiter))

    assert result["status"] == "completed"
    assert limiter.events == ["acquire:1", "release"]


def test_simulator_run_records_after_turn_and_final_observations() -> None:
    observer_events: list[dict] = []

    async def agent_fn(request, state, environments, turn):
        return {"response": {"turn": turn}}

    async def user_fn(response, state, environments, turn):
        return {"request": {"done": True}, "terminate": True}

    async def observer_fn(event, state, environments):
        observer_events.append(event)
        return {
            "observation": {"schedule": event["schedule"], "turn_count": len(state["turns"])},
            "artifacts": [{"kind": event["schedule"]}],
            "state": {"metadata": {"observed": event["schedule"]}},
        }

    result = asyncio.run(
        SimulatorRunRowFunction().transform(
            {"message": "start"},
            user_fn,
            agent_fn,
            observers=[
                {
                    "spec_type": "simulator_observer",
                    "callback": observer_fn,
                    "schedule": ["after_turn", "final"],
                    "config": {"sample": True},
                }
            ],
        )
    )

    assert [event["schedule"] for event in observer_events] == ["after_turn", "final"]
    assert result["observations"] == [
        {
            "callback": "observer_fn",
            "schedule": "after_turn",
            "turn": 1,
            "observation": {"schedule": "after_turn", "turn_count": 1},
            "artifacts": [{"kind": "after_turn"}],
            "error": None,
        },
        {
            "callback": "observer_fn",
            "schedule": "final",
            "turn": 1,
            "observation": {"schedule": "final", "turn_count": 1},
            "artifacts": [{"kind": "final"}],
            "error": None,
        },
    ]
    assert result["artifacts"] == [{"kind": "after_turn"}, {"kind": "final"}]


def test_simulator_run_records_observer_failures_without_failing_run() -> None:
    async def agent_fn(request, state, environments, turn):
        return {"response": "response"}

    async def user_fn(response, state, environments, turn):
        return {"request": "done", "terminate": True}

    async def observer_fn(event, state, environments):
        raise RuntimeError("observer unavailable")

    result = asyncio.run(
        SimulatorRunRowFunction().transform(
            "start",
            user_fn,
            agent_fn,
            observers=[{"spec_type": "simulator_observer", "callback": observer_fn, "schedule": ["final"]}],
        )
    )

    assert result["ok"] is True
    assert result["observations"][0]["error"] == {
        "code": "failed",
        "message": "observer unavailable",
        "retryable": False,
    }


def test_simulator_run_returns_failed_result_when_agent_fails() -> None:
    async def agent_fn(request, state, environments, turn):
        raise RuntimeError("agent failed")

    async def user_fn(response, state, environments, turn):
        return {"request": "unused", "terminate": True}

    result = asyncio.run(SimulatorRunRowFunction().transform("start", user_fn, agent_fn))

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == {"code": "failed", "message": "agent failed", "retryable": False}
    assert result["turns"][0]["error"] == result["error"]


def test_simulator_run_returns_max_turns_status() -> None:
    async def agent_fn(request, state, environments, turn):
        return {"response": {"turn": turn}}

    async def user_fn(response, state, environments, turn):
        return {"request": {"next": turn}, "terminate": False}

    result = asyncio.run(
        SimulatorRunRowFunction().transform("start", user_fn, agent_fn, limits={"max_turns": 2, "timeout_seconds": 10})
    )

    assert result["ok"] is False
    assert result["status"] == "max_turns"
    assert result["error"]["code"] == "max_turns"
    assert [turn["turn"] for turn in result["turns"]] == [1, 2]


def test_simulator_run_rejects_invalid_specs() -> None:
    async def agent_fn(request, state, environments, turn):
        return {"response": "ok"}

    async def user_fn(response, state, environments, turn):
        return {"request": "ok", "terminate": True}

    with pytest.raises(ValueError, match="reuse must be one of"):
        asyncio.run(SimulatorRunRowFunction().transform("start", user_fn, agent_fn, reuse="forever"))

    with pytest.raises(ValueError, match="Unsupported observer schedule"):
        SimulatorObserverUdf().function()().transform("observer", ["1s"])

    with pytest.raises(ValueError, match="Unknown environment kind"):
        asyncio.run(
            SimulatorRunRowFunction().transform(
                "start",
                user_fn,
                agent_fn,
                environments=[{"spec_type": "environment", "kind": "vm", "env_id": "bad", "config": {}}],
            )
        )


def test_simulator_run_resolves_registered_callback_handles() -> None:
    from agentcicd.sql.udf_registry import clear_registered_udfs, register_udf

    class _RegisteredAgentFunction(AsyncRowFunction):
        async def transform(self, request, state, environments, turn):
            return {"response": {"echo": request, "turn": turn, "turn_count": len(state["turns"])}}

    class _RegisteredUserFunction(AsyncRowFunction):
        async def transform(self, agent_response, state, environments, turn):
            return {"request": {"final": agent_response}, "terminate": True}

    class _RegisteredAgentUdf(Udf):
        def input_schema(self):
            return (JsonType(), JsonType(), JsonType(), JsonType())

        def input_args(self):
            return tuple(parameter.name for parameter in self.signature())

        def signature(self):
            return (
                Param("request", type_sql="VARIANT"),
                Param("state", type_sql="VARIANT"),
                Param("environments", type_sql="ANY"),
                Param("turn", type_sql="INTEGER"),
            )

        def metadata(self):
            return {
                "capabilities": ["row_callable", "simulator_agent"],
                "return_type_sql": "STRUCT<response: VARIANT, state: VARIANT>",
            }

        def output_schema(self):
            return JsonType()

        def ftype(self):
            return FType.BATCH_FUNCTION

        def function(self):
            return _RegisteredAgentFunction

    class _RegisteredUserUdf(Udf):
        def input_schema(self):
            return (JsonType(), JsonType(), JsonType(), JsonType())

        def input_args(self):
            return tuple(parameter.name for parameter in self.signature())

        def signature(self):
            return (
                Param("agent_response", type_sql="VARIANT"),
                Param("state", type_sql="VARIANT"),
                Param("environments", type_sql="ANY"),
                Param("turn", type_sql="INTEGER"),
            )

        def metadata(self):
            return {
                "capabilities": ["row_callable", "simulator_user"],
                "return_type_sql": "STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>",
            }

        def output_schema(self):
            return JsonType()

        def ftype(self):
            return FType.BATCH_FUNCTION

        def function(self):
            return _RegisteredUserFunction

    register_udf(_RegisteredAgentUdf, "test.simulator_agent")
    register_udf(_RegisteredUserUdf, "test.simulator_user")
    try:
        result = asyncio.run(
            SimulatorRunRowFunction().transform(
                {"message": "start"},
                "test.simulator_user",
                "test.simulator_agent",
            )
        )
    finally:
        clear_registered_udfs()

    assert result["status"] == "completed"
    assert result["turns"][0]["response"] == {"echo": {"message": "start"}, "turn": 1, "turn_count": 0}
    assert result["final_output"] == {"final": result["turns"][0]["response"]}
