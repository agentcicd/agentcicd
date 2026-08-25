from __future__ import annotations

import asyncio
import json
import shutil
import tomllib
from pathlib import Path

import pytest

import agentcicd.fixtures.functions.agent_harness as agent_harness_module
from agentcicd.fixtures.core.types import FType, JsonType
from agentcicd.fixtures.functions.agent_harness import (
    AdapterError,
    AgentHarnessRunTaskRowFunction,
    AgentHarnessSetupSpec,
    AgentHarnessExecutionError,
    ArtifactReference,
    CodexCliAdapter,
    EnvsAgentHarnessRunTaskUdf,
    HarnessRunRequest,
    HarnessRunResult,
    HarnessTeardownStatus,
    create_session,
    get_adapter_registry,
    harness_result_to_dict,
    setup_spec_from_aisystem_payload,
)
from agentcicd.fixtures.functions.simulators import EnvsAgentHarnessSpecUdf, RuntimeEnvironments, SimulatorRunRowFunction


class _FakeAdapter:
    def __init__(self) -> None:
        self.requests: list[HarnessRunRequest] = []
        self.teardowns: list[tuple[AgentHarnessSetupSpec, str, str | None]] = []

    async def run_task(self, request: HarnessRunRequest) -> HarnessRunResult:
        self.requests.append(request)
        return HarnessRunResult(
            status="completed",
            final_output=f"done:{request.task}",
            transcript=({"role": "assistant", "content": "done"},),
            artifacts=(ArtifactReference(kind="file", path="/tmp/result.txt", name="result.txt", size_bytes=12),),
            duration_ms=7,
            metadata={"adapter": "fake"},
        )

    async def teardown(self, setup: AgentHarnessSetupSpec, reason_code: str, message: str | None = None) -> HarnessTeardownStatus:
        self.teardowns.append((setup, reason_code, message))
        return HarnessTeardownStatus(status="completed")


def test_agent_harness_run_task_udf_contract() -> None:
    udf = EnvsAgentHarnessRunTaskUdf()

    assert udf.input_args() == (
        "env",
        "task",
        "timeout_seconds",
        "transcript_file",
        "pool",
        "limiter",
    )
    assert isinstance(udf.input_schema()[0], JsonType)
    assert udf.metadata()["return_type_sql"].startswith("STRUCT<status: STRING")
    assert udf.metadata()["execution_runtime"] == "function_runner"
    assert udf.metadata()["pool_kind"] == "session"
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), AgentHarnessRunTaskRowFunction)


def test_agent_harness_run_task_alias_accepts_inline_spec() -> None:
    from agentcicd.fixtures.functions.environment_aliases import AgentHarnessRunTaskUdf

    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)
    try:
        result = asyncio.run(
            AgentHarnessRunTaskUdf().function()().transform(
                {"session_id": "agent", "harness": "fake", "workdir": "/workspace"},
                "inspect app",
                timeout_seconds=12,
            )
        )
    finally:
        registry.unregister("fake")

    assert result["status"] == "completed"
    assert result["final_output"] == "done:inspect app"
    assert adapter.requests[0].environment == AgentHarnessSetupSpec(
        session_id="agent",
        harness="fake",
        workdir="/workspace",
        config={},
    )
    assert adapter.requests[0].timeout_seconds == 12
    assert adapter.teardowns[0][1] == "completed"


def test_run_task_uses_registered_adapter_and_returns_portable_result() -> None:
    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)
    try:
        session = create_session(
            AgentHarnessSetupSpec(
                session_id="agent",
                harness="fake",
                workdir="/workspace",
                config={"vendor_only": True},
            )
        )
        result = asyncio.run(
            AgentHarnessRunTaskRowFunction().transform(
                session,
                "fix the test",
                timeout_seconds=12,
                transcript_file="answer/codex.jsonl",
            )
        )
    finally:
        registry.unregister("fake")

    assert adapter.requests == [
        HarnessRunRequest(
            environment=AgentHarnessSetupSpec(
                session_id="agent",
                harness="fake",
                workdir="/workspace",
                config={"vendor_only": True},
            ),
            task="fix the test",
            timeout_seconds=12,
            transcript_file="answer/codex.jsonl",
        )
    ]
    assert adapter.teardowns == []
    assert result == {
        "status": "completed",
        "final_output": "done:fix the test",
        "transcript": [{"role": "assistant", "content": "done"}],
        "artifacts": [
            {
                "kind": "file",
                "uri": None,
                "path": "/tmp/result.txt",
                "name": "result.txt",
                "mime_type": None,
                "size_bytes": 12,
                "metadata": {},
            }
        ],
        "error": None,
        "duration_ms": 7,
        "metadata": {"adapter": "fake"},
    }


def test_run_task_with_serialized_environment_spec_tears_down_after_call() -> None:
    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)
    try:
        result = asyncio.run(
            AgentHarnessRunTaskRowFunction().transform(
                {
                    "session_id": "agent",
                    "harness": "fake",
                    "workdir": "/workspace",
                    "config": {"vendor_only": True},
                },
                "fix the test",
            )
        )
    finally:
        registry.unregister("fake")

    setup = AgentHarnessSetupSpec(
        session_id="agent",
        harness="fake",
        workdir="/workspace",
        config={"vendor_only": True},
    )
    assert adapter.requests == [HarnessRunRequest(environment=setup, task="fix the test")]
    assert adapter.teardowns == [(setup, "completed", None)]
    assert result["status"] == "completed"


def test_run_task_accepts_json_encoded_environment_spec() -> None:
    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)
    try:
        result = asyncio.run(
            AgentHarnessRunTaskRowFunction().transform(
                json.dumps(
                    {
                        "session_id": "agent",
                        "harness": "fake",
                        "workdir": "/workspace",
                        "config": {"vendor_only": True},
                    }
                ),
                "fix the test",
            )
        )
    finally:
        registry.unregister("fake")

    assert result["status"] == "completed"
    assert adapter.requests[0].environment == AgentHarnessSetupSpec(
        session_id="agent",
        harness="fake",
        workdir="/workspace",
        config={"vendor_only": True},
    )


def test_run_task_accepts_row_like_environment_spec() -> None:
    class _RowLike:
        def asDict(self, recursive: bool = False) -> dict:
            assert recursive is True
            return {
                "session_id": "agent",
                "harness": "fake",
                "workdir": "/workspace",
                "config": {"vendor_only": True},
            }

    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)
    try:
        result = asyncio.run(
            AgentHarnessRunTaskRowFunction().transform(
                _RowLike(),
                "fix the test",
            )
        )
    finally:
        registry.unregister("fake")

    assert result["status"] == "completed"
    assert adapter.requests[0].environment == AgentHarnessSetupSpec(
        session_id="agent",
        harness="fake",
        workdir="/workspace",
        config={"vendor_only": True},
    )


def test_unknown_harness_fails_clearly() -> None:
    with pytest.raises(ValueError, match="Unknown agent harness 'missing'"):
        create_session(AgentHarnessSetupSpec(session_id="agent", harness="missing", workdir="/workspace"))


def test_result_schema_keeps_adapter_errors_structured() -> None:
    result = harness_result_to_dict(
        HarnessRunResult(
            status="error",
            final_output=None,
            error=AdapterError(
                code="adapter_process_failed",
                message="exit 1",
                retryable=False,
                metadata={"returncode": 1},
            ),
            duration_ms=3,
        )
    )

    assert result["error"] == {
        "code": "adapter_process_failed",
        "message": "exit 1",
        "retryable": False,
        "metadata": {"returncode": 1},
    }


def test_codex_adapter_timeout_returns_timeout_status(tmp_path: Path) -> None:
    script = tmp_path / "slow-codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    result = asyncio.run(
        AgentHarnessRunTaskRowFunction().transform(
            {
                "session_id": "agent",
                "harness": "codex",
                "workdir": str(tmp_path),
                "config": {"binary": str(script)},
            },
            "task",
            timeout_seconds=0.01,
        )
    )

    assert result["status"] == "timeout"
    assert result["error"]["code"] == "timeout"


def test_codex_adapter_uses_current_exec_config_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        captured["command"] = list(command)
        captured["stdin"] = stdin
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(tmp_path),
                    config={
                        "binary": str(binary),
                        "approval_mode": "never",
                        "sandbox": "workspace-write",
                        "model": "gpt-5-codex",
                    },
                ),
                task="ship it",
                timeout_seconds=10,
            )
        )
    )

    command = captured["command"]
    assert result.status == "completed"
    assert "--ask-for-approval" not in command
    assert command[:8] == [
        str(binary),
        "exec",
        "--cd",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
    ]
    assert "--skip-git-repo-check" in command
    assert "--model" in command
    assert "gpt-5-codex" in command


def test_codex_adapter_creates_nested_workdir_before_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    workdir = tmp_path / "workspace" / "case-1" / "local_only"
    captured: dict[str, object] = {}

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        captured["cwd"] = cwd
        captured["workdir_exists_during_launch"] = Path(cwd).is_dir()
        last_message_path = Path(command[command.index("--output-last-message") + 1])
        last_message_path.write_text('{"move":"east"}', encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(workdir),
                    config={"binary": str(binary)},
                ),
                task="choose a move",
                timeout_seconds=10,
            )
        )
    )

    assert result.status == "completed"
    assert result.final_output == '{"move":"east"}'
    assert captured["cwd"] == str(workdir)
    assert captured["workdir_exists_during_launch"] is True
    assert workdir.is_dir()


def test_codex_adapter_writes_json_stdout_to_transcript_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    transcript = tmp_path / "answer" / "codex.jsonl"

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        last_message_path = Path(command[command.index("--output-last-message") + 1])
        last_message_path.write_text("done", encoding="utf-8")
        return {"returncode": 0, "stdout": '{"type":"message"}\\n', "stdout_bytes": b'{"type":"message"}\n', "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(tmp_path),
                    config={"binary": str(binary)},
                ),
                task="ship it",
                timeout_seconds=10,
                transcript_file="answer/codex.jsonl",
            )
        )
    )

    assert result.final_output == "done"
    assert result.transcript == ()
    assert transcript.read_text(encoding="utf-8") == '{"type":"message"}\n'


def test_codex_adapter_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        return {"returncode": 2, "stdout": "", "stdout_bytes": b"", "stderr": "bad exit"}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    with pytest.raises(AgentHarnessExecutionError, match="bad exit") as exc_info:
        asyncio.run(
            CodexCliAdapter().run_task(
                HarnessRunRequest(
                    environment=AgentHarnessSetupSpec(
                        session_id="agent",
                        harness="codex",
                        workdir=str(tmp_path),
                        config={"binary": str(binary)},
                    ),
                    task="ship it",
                    timeout_seconds=10,
                )
            )
        )

    assert exc_info.value.error.code == "adapter_process_failed"
    assert exc_info.value.error.message == "Codex CLI exited with status 2: bad exit"
    assert exc_info.value.error.metadata == {"returncode": 2}


def test_codex_adapter_nonzero_exit_falls_back_to_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        return {
            "returncode": 1,
            "stdout": '{"type":"error","message":"auth missing"}',
            "stdout_bytes": b'{"type":"error","message":"auth missing"}',
            "stderr": "",
        }

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    with pytest.raises(AgentHarnessExecutionError, match="auth missing") as exc_info:
        asyncio.run(
            CodexCliAdapter().run_task(
                HarnessRunRequest(
                    environment=AgentHarnessSetupSpec(
                        session_id="agent",
                        harness="codex",
                        workdir=str(tmp_path),
                        config={"binary": str(binary)},
                    ),
                    task="ship it",
                    timeout_seconds=10,
                )
            )
        )

    assert exc_info.value.error.message == (
        'Codex CLI exited with status 1: {"type":"error","message":"auth missing"}'
    )


def test_codex_adapter_applies_auth_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        captured["env"] = dict(env)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(tmp_path),
                    config={
                        "binary": str(binary),
                        "auth": {"env": {"CODEX_API_KEY": "sk-test", "CODEX_AUTH_FILE": "/tmp/auth.json"}},
                    },
                ),
                task="ship it",
                timeout_seconds=10,
            )
        )
    )

    env = captured["env"]
    assert result.status == "completed"
    assert env["CODEX_API_KEY"] == "sk-test"
    assert env["CODEX_AUTH_FILE"] == "/tmp/auth.json"


def test_codex_adapter_writes_isolated_mcp_config_and_preserves_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"token":"chatgpt"}', encoding="utf-8")
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "secret_ids": ["secret.mcp"],
                "secrets": [
                    {
                        "id": "secret.mcp",
                        "key": "mcp",
                        "secret": {
                            "type": "http",
                            "bearer_token": "mcp-token",
                            "headers": {"X-Static": "secret-header"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))
    captured: dict[str, object] = {}

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        codex_home = Path(env["CODEX_HOME"])
        captured["env"] = dict(env)
        captured["config"] = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        captured["auth"] = (codex_home / "auth.json").read_text(encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(tmp_path),
                    config={
                        "binary": str(binary),
                        "auth": {"codex_home": str(source_home), "env": {"CODEX_API_KEY": "sk-test"}},
                        "mcps": [
                            {
                                "spec_type": "mcp",
                                "transport": "http",
                                "name": "metrics",
                                "endpoint": "http://127.0.0.1:3000/mcp",
                                "required": True,
                                "secret_id": "secret.mcp",
                                "allow_tools": ["fetch"],
                                "deny_tools": ["delete"],
                                "headers": {"X-Explicit": "explicit-header"},
                            },
                            {
                                "spec_type": "mcp",
                                "transport": "stdio",
                                "name": "playwright",
                                "command": "playwright-mcp",
                                "args": ["--headless", "--isolated"],
                                "required": True,
                                "allow_tools": ["browser_navigate"],
                                "deny_tools": ["browser_install"],
                                "default_tools_approval_mode": "approve",
                                "env": {"PLAYWRIGHT_BROWSERS_PATH": "/browsers"},
                            }
                        ],
                    },
                ),
                task="ship it",
                timeout_seconds=10,
            )
        )
    )

    env = captured["env"]
    config = captured["config"]
    metrics = config["mcp_servers"]["metrics"]
    playwright = config["mcp_servers"]["playwright"]
    assert result.status == "completed"
    assert result.metadata["mcp_servers"] == ["metrics", "playwright"]
    assert env["CODEX_API_KEY"] == "sk-test"
    assert env["AGENTCICD_MCP_METRICS_BEARER_TOKEN"] == "mcp-token"
    assert env["AGENTCICD_MCP_METRICS_X_STATIC"] == "secret-header"
    assert env["AGENTCICD_MCP_METRICS_X_EXPLICIT"] == "explicit-header"
    assert captured["auth"] == '{"token":"chatgpt"}'
    assert metrics == {
        "url": "http://127.0.0.1:3000/mcp",
        "required": True,
        "enabled_tools": ["fetch"],
        "disabled_tools": ["delete"],
        "bearer_token_env_var": "AGENTCICD_MCP_METRICS_BEARER_TOKEN",
        "env_http_headers": {
            "X-Static": "AGENTCICD_MCP_METRICS_X_STATIC",
            "X-Explicit": "AGENTCICD_MCP_METRICS_X_EXPLICIT",
        },
    }
    assert playwright == {
        "command": "playwright-mcp",
        "args": ["--headless", "--isolated"],
        "required": True,
        "env": {"PLAYWRIGHT_BROWSERS_PATH": "/browsers"},
        "enabled_tools": ["browser_navigate"],
        "disabled_tools": ["browser_install"],
        "default_tools_approval_mode": "approve",
    }


def test_codex_adapter_runs_structured_playwright_capture_without_prompting_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    class _McpHandle:
        async def call_tool(self, name: str, arguments: dict):
            captured["tool"] = name
            captured["arguments"] = dict(arguments)
            Path(arguments["filename"]).write_bytes(b"png")
            return {"ok": True}

        async def teardown(self, reason) -> None:
            captured["teardown"] = reason.code

    def fake_materialized_mcp_from_spec(spec):
        captured["spec"] = dict(spec)
        return _McpHandle()

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        captured["stdin"] = stdin
        last_message_path = Path(command[command.index("--output-last-message") + 1])
        last_message_path.write_text("done", encoding="utf-8")
        return {"returncode": 0, "stdout": "", "stdout_bytes": b"", "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)
    import agentcicd.fixtures.functions.simulators as simulators_module

    monkeypatch.setattr(simulators_module, "materialized_mcp_from_spec", fake_materialized_mcp_from_spec)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(tmp_path),
                    config={
                        "binary": str(binary),
                        "mcps": [
                            {
                                "spec_type": "mcp",
                                "transport": "http",
                                "name": "playwright",
                                "endpoint": "http://127.0.0.1:3000/mcp",
                                "playwright": {
                                    "capture_requests": [
                                        {"kind": "screenshot", "path": "screens/final.png", "full_page": False}
                                    ]
                                },
                            }
                        ],
                    },
                ),
                task="inspect the page",
                timeout_seconds=10,
            )
        )
    )

    assert captured["stdin"] == "inspect the page"
    assert captured["tool"] == "browser_take_screenshot"
    assert captured["arguments"]["fullPage"] is False
    assert captured["arguments"]["filename"].endswith("/screens/final.png")
    assert result.metadata["mcp_capture"][0]["status"] == "completed"
    assert any(artifact.name == "final.png" for artifact in result.artifacts)


def test_codex_adapter_materializes_codex_home_files_from_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    async def fake_run_subprocess(command, stdin, *, cwd, env, limit_bytes, timeout_seconds):
        codex_home = Path(env["CODEX_HOME"])
        captured["codex_home"] = codex_home
        captured["auth"] = (codex_home / "auth.json").read_text(encoding="utf-8")
        captured["config"] = (codex_home / "config.toml").read_text(encoding="utf-8")
        captured["home_mode"] = codex_home.stat().st_mode & 0o777
        captured["auth_mode"] = (codex_home / "auth.json").stat().st_mode & 0o777
        captured["config_mode"] = (codex_home / "config.toml").stat().st_mode & 0o777
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_harness_module, "_run_subprocess", fake_run_subprocess)

    result = asyncio.run(
        CodexCliAdapter().run_task(
            HarnessRunRequest(
                environment=AgentHarnessSetupSpec(
                    session_id="agent",
                    harness="codex",
                    workdir=str(tmp_path),
                    config={
                        "binary": str(binary),
                        "auth": {
                            "codex_home_files": {
                                "auth.json": '{"token":"chatgpt"}',
                                "config.toml": 'model = "gpt-5-codex"\n',
                            }
                        },
                    },
                ),
                task="ship it",
                timeout_seconds=10,
            )
        )
    )

    assert result.status == "completed"
    assert captured["auth"] == '{"token":"chatgpt"}'
    assert captured["config"] == 'model = "gpt-5-codex"\n'
    assert captured["home_mode"] == 0o700
    assert captured["auth_mode"] == 0o600
    assert captured["config_mode"] == 0o600
    assert not Path(captured["codex_home"]).exists()


def test_generated_codex_home_uses_non_tmp_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    parent = tmp_path / "workspace" / ".agentcicd" / "codex-home"
    monkeypatch.setenv("AGENTCICD_CODEX_HOME_PARENT", str(parent))

    codex_home, env = agent_harness_module._prepare_codex_home((), auth={"codex_home_files": {"config.toml": ""}})

    try:
        assert env == {}
        assert codex_home.parent == parent
        assert str(codex_home).startswith(str(parent))
        assert codex_home.name.startswith("agentcicd-codex-home-")
        assert codex_home.exists()
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)


def test_aisystem_payload_resolves_harness_config_and_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.codex",
                        "name": "Codex",
                        "target": "codex:gpt-5-codex",
                        "interface": {"interface_type": "llm.chat"},
                        "config": {"approval_mode": "never"},
                    }
                ],
                "secret_ids": ["secret.codex"],
                "secrets": [
                    {
                        "id": "secret.codex",
                        "key": "codex",
                        "secret": {
                            "type": "api_key",
                            "api_key": "sk-test",
                            "codex_home": "/tmp/codex-home",
                            "env": {"CODEX_AUTH_FILE": "/tmp/codex-home/auth.json"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    setup = setup_spec_from_aisystem_payload(
        {
            "session_id": "agent",
            "aisystem": "aisystem.codex",
            "workdir": "/workspace",
            "secret_id": "secret.codex",
        }
    )

    assert setup == AgentHarnessSetupSpec(
        session_id="agent",
        harness="codex",
        workdir="/workspace",
        config={
            "approval_mode": "never",
            "model": "gpt-5-codex",
            "auth": {
                "type": "secret_ref",
                "secret_id": "secret.codex",
                "env": {"CODEX_AUTH_FILE": "/tmp/codex-home/auth.json", "CODEX_API_KEY": "sk-test"},
                "codex_home": "/tmp/codex-home",
            },
            "env": {"CODEX_AUTH_FILE": "/tmp/codex-home/auth.json", "CODEX_API_KEY": "sk-test"},
            "aisystem_id": "aisystem.codex",
        },
    )


def test_aisystem_payload_resolves_raw_codex_home_files_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.json"
    raw_value = json.dumps(
        {
            "codex_home_files": {
                "auth.json": '{"token":"chatgpt"}',
                "config.toml": 'model = "gpt-5-codex"\n',
            },
            "env": {"CODEX_FEATURE": "enabled"},
        }
    )
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.codex",
                        "name": "Codex",
                        "target": "codex:gpt-5-codex",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "secret_ids": ["secret.codex"],
                "secrets": [
                    {
                        "id": "secret.codex",
                        "key": "codex",
                        "secret_type": "raw",
                        "secret": {"type": "raw", "value": raw_value},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    setup = setup_spec_from_aisystem_payload(
        {
            "session_id": "agent",
            "aisystem": "aisystem.codex",
            "workdir": "/workspace",
            "secret_id": "secret.codex",
        }
    )

    auth = setup.config["auth"]
    assert setup.harness == "codex"
    assert auth["type"] == "secret_ref"
    assert auth["secret_id"] == "secret.codex"
    assert auth["codex_home_files"] == {
        "auth.json": '{"token":"chatgpt"}',
        "config.toml": 'model = "gpt-5-codex"\n',
    }
    assert auth["env"] == {"CODEX_FEATURE": "enabled"}


def test_aisystem_payload_accepts_explicit_run_secret_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.codex",
                        "name": "Codex",
                        "target": "codex:gpt-5-codex",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "secret_ids": ["secret.codex"],
                "secrets": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    setup = setup_spec_from_aisystem_payload(
        {
            "session_id": "agent",
            "aisystem": "aisystem.codex",
            "workdir": "/workspace",
            "secret_id": "secret.codex",
        }
    )

    assert setup.config["auth"] == {"type": "secret_ref", "secret_id": "secret.codex"}
    assert setup.config["aisystem_id"] == "aisystem.codex"


def test_llm_aisystem_defaults_to_codex_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.openai_gpt",
                        "name": "openai_gpt-5_4",
                        "target": "openai/gpt-5.4",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "secret_ids": ["secret.codex"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    setup = setup_spec_from_aisystem_payload(
        {
            "session_id": "agent",
            "aisystem": "aisystem.openai_gpt",
            "workdir": "/workspace",
            "secret_id": "secret.codex",
        }
    )

    assert setup.harness == "codex"
    assert setup.config["model"] == "gpt-5.4"
    assert setup.config["auth"] == {"type": "secret_ref", "secret_id": "secret.codex"}


def test_llm_aisystem_configured_openai_model_is_canonicalized_for_codex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.openai_gpt",
                        "name": "openai_gpt-5_4",
                        "target": "codex",
                        "interface": {"interface_type": "llm.chat"},
                        "config": {"model": "openai/gpt-5.4"},
                    }
                ],
                "secret_ids": ["secret.codex"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    setup = setup_spec_from_aisystem_payload(
        {
            "session_id": "agent",
            "aisystem": "aisystem.openai_gpt",
            "workdir": "/workspace",
            "secret_id": "secret.codex",
        }
    )

    assert setup.harness == "codex"
    assert setup.config["model"] == "gpt-5.4"


def test_simulator_can_run_agent_harness_task_with_fixture_callbacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.fake",
                        "target": "fake",
                        "interface": {"interface_type": "llm.chat"},
                        "config": {"adapter": "fake"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))
    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)

    async def one_shot_agent(request: dict, state: dict, environments: RuntimeEnvironments, turn: int) -> dict:
        first = await environments.agent.run_task(
            request["initial_task"],
            timeout_seconds=30,
        )
        second = await environments["agent"].run_task("second task", timeout_seconds=30)
        return {"response": second, "state": {**state, "agent_run": first}}

    async def one_shot_user(agent_response: dict, state: dict, environments: RuntimeEnvironments, turn: int) -> dict:
        return {"request": None, "terminate": True, "state": state}

    try:
        simulation = asyncio.run(
            SimulatorRunRowFunction().transform(
                {"case_id": "case-1", "initial_task": "repair the fixture"},
                one_shot_user,
                one_shot_agent,
                environments=[
                    EnvsAgentHarnessSpecUdf().function()().transform(
                        "agent",
                        aisystem="aisystem.fake",
                        workdir="/workspace",
                    )
                ],
                limits={"max_turns": 2, "timeout_seconds": 60},
            )
        )
    finally:
        registry.unregister("fake")

    assert simulation["status"] == "completed"
    assert simulation["turns"][0]["response"] == "done:second task"
    setup = AgentHarnessSetupSpec(
        session_id="agent",
        harness="fake",
        workdir="/workspace",
        config={"aisystem_id": "aisystem.fake"},
    )
    assert [request.environment for request in adapter.requests] == [setup, setup]
    assert adapter.teardowns == [(setup, "completed", None)]


def test_agent_harness_environment_teardown_allows_recreate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.fake",
                        "target": "fake",
                        "interface": {"interface_type": "llm.chat"},
                        "config": {"adapter": "fake"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))
    registry = get_adapter_registry()
    adapter = _FakeAdapter()
    registry.register("fake", adapter)

    async def agent_fn(request: dict, state: dict, environments: RuntimeEnvironments, turn: int) -> dict:
        first = await environments.agent.run_task("first")
        await environments.agent.teardown(type("Reason", (), {"code": "manual", "message": None})())
        second = await environments.agent.run_task("second")
        return {"response": {"first": first, "second": second}}

    async def user_fn(agent_response: dict, state: dict, environments: RuntimeEnvironments, turn: int) -> dict:
        return {"request": None, "terminate": True}

    try:
        simulation = asyncio.run(
            SimulatorRunRowFunction().transform(
                {},
                user_fn,
                agent_fn,
                environments=[
                    EnvsAgentHarnessSpecUdf().function()().transform(
                        "agent",
                        aisystem="aisystem.fake",
                        workdir="/workspace",
                    )
                ],
                limits={"max_turns": 1, "timeout_seconds": 60},
            )
        )
    finally:
        registry.unregister("fake")

    setup = AgentHarnessSetupSpec(
        session_id="agent",
        harness="fake",
        workdir="/workspace",
        config={"aisystem_id": "aisystem.fake"},
    )
    assert simulation["status"] == "completed"
    assert simulation["turns"][0]["response"] == {"first": "done:first", "second": "done:second"}
    assert [request.task for request in adapter.requests] == ["first", "second"]
    assert adapter.teardowns == [(setup, "manual", None), (setup, "completed", None)]
