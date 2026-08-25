from __future__ import annotations

from agentcicd.fixtures.environments.shell.types import ShellSetupSpec


def shell_setup(cwd: str) -> ShellSetupSpec:
    return ShellSetupSpec(env_id="env.shell", session_id="session.shell", cwd=cwd)
