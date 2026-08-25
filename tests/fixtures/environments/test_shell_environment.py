from __future__ import annotations

import sys

import pytest

from agentcicd.fixtures.environments.core.errors import PolicyViolation
from agentcicd.fixtures.environments.core.lifecycle import TeardownReason
from agentcicd.fixtures.environments.shell.subprocess_adapter import SubprocessShellEnvironment
from agentcicd.fixtures.environments.shell.types import EnvironmentVariable, ShellCommand, ShellPolicy, ShellSetupSpec


@pytest.mark.asyncio
async def test_shell_runs_allowlisted_argv_command_and_records_history(tmp_path) -> None:
    session = await SubprocessShellEnvironment().setup(
        ShellSetupSpec(
            env_id="env.shell",
            session_id="session.shell",
            cwd=str(tmp_path),
            env=(EnvironmentVariable(name="AGENTCICD_BASE", value="base"),),
            policy=ShellPolicy(allowed_commands=(sys.executable,), env_allowlist=("AGENTCICD_COMMAND",)),
        )
    )

    result = await session.run(
        ShellCommand(
            argv=(sys.executable, "-c", "import os; print(os.environ['AGENTCICD_COMMAND'])"),
            env=(EnvironmentVariable(name="AGENTCICD_COMMAND", value="ok"),),
        )
    )

    assert result.exit_code == 0
    assert result.stdout == b"ok\n"
    assert (await session.command_history())[0].command_id == result.command_id


@pytest.mark.asyncio
async def test_shell_blocked_command_returns_typed_failure(tmp_path) -> None:
    session = await SubprocessShellEnvironment().setup(
        ShellSetupSpec(
            env_id="env.shell",
            session_id="session.shell",
            cwd=str(tmp_path),
            policy=ShellPolicy(blocked_commands=("sudo",)),
        )
    )

    result = await session.run(ShellCommand(argv=("sudo", "true")))

    assert result.exit_code == 126
    assert result.stderr is not None
    assert b"blocked" in result.stderr


@pytest.mark.asyncio
async def test_shell_timeout_truncation_and_process_teardown(tmp_path) -> None:
    session = await SubprocessShellEnvironment().setup(
        ShellSetupSpec(
            env_id="env.shell",
            session_id="session.shell",
            cwd=str(tmp_path),
            policy=ShellPolicy(allowed_commands=(sys.executable,), max_stdout_bytes=4, timeout_seconds=0.2),
        )
    )

    truncated = await session.run(ShellCommand(argv=(sys.executable, "-c", "print('abcdef')")))
    assert truncated.stdout == b"abcd"
    assert truncated.output_truncated is True

    timed_out = await session.run(ShellCommand(argv=(sys.executable, "-c", "import time; time.sleep(5)")))
    assert timed_out.timed_out is True

    handle = await session.start(ShellCommand(argv=(sys.executable, "-c", "import time; time.sleep(30)")))
    assert [item.process_id for item in await session.running_processes()] == [handle.process_id]
    teardown = await session.teardown(TeardownReason(code="test_complete"))
    assert teardown.terminated_processes == (handle.process_id,)


@pytest.mark.asyncio
async def test_shell_start_respects_background_policy(tmp_path) -> None:
    session = await SubprocessShellEnvironment().setup(
        ShellSetupSpec(
            env_id="env.shell",
            session_id="session.shell",
            cwd=str(tmp_path),
            policy=ShellPolicy(allow_background_processes=False),
        )
    )

    with pytest.raises(PolicyViolation, match="background"):
        await session.start(ShellCommand(argv=(sys.executable, "-c", "print('no')")))
