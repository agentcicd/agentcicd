from __future__ import annotations

from dataclasses import asdict

from agentcicd.fixtures.environments.core.lifecycle import Label, TeardownReason
from agentcicd.fixtures.environments.shell.types import EnvironmentVariable, ShellCommand, ShellSetupSpec


def test_setup_specs_are_typed_dataclasses() -> None:
    shell_spec = ShellSetupSpec(
        env_id="env.shell",
        session_id="session.shell",
        run_id="run.1",
        labels=(Label(key="suite", value="unit"),),
        cwd="/tmp/work",
        env=(EnvironmentVariable(name="AGENTCICD_TEST", value="1"),),
    )

    assert asdict(shell_spec)["labels"][0]["key"] == "suite"
    assert shell_spec.env[0].name == "AGENTCICD_TEST"
    assert TeardownReason(code="done").message is None


def test_shell_command_is_argv_only() -> None:
    command = ShellCommand(argv=("python", "-c", "print('ok')"))

    assert command.argv == ("python", "-c", "print('ok')")
    assert command.stdin is None
