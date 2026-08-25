from __future__ import annotations

import asyncio
import os
import signal as signal_module
import time
from dataclasses import dataclass
from pathlib import Path

from agentcicd.fixtures.environments.core.errors import ActionFailed, PolicyViolation
from agentcicd.fixtures.environments.core.ids import StableIdFactory
from agentcicd.fixtures.environments.core.lifecycle import TeardownReason
from agentcicd.fixtures.environments.shell.types import (
    CommandResult,
    EnvironmentVariable,
    ProcessHandle,
    ProcessSignal,
    ProcessSummary,
    ResourceUsage,
    ShellCommand,
    ShellPolicy,
    ShellSetupSpec,
    ShellTeardownResult,
)


@dataclass
class _ProcessRecord:
    process_id: str
    command_id: str
    argv: tuple[str, ...]
    cwd: str
    started_at: float
    process: asyncio.subprocess.Process
    capture_stdout: bool
    capture_stderr: bool


class SubprocessShellEnvironment:
    async def setup(self, spec: ShellSetupSpec) -> "SubprocessShellSession":
        cwd = Path(spec.cwd).expanduser().resolve()
        cwd.mkdir(parents=True, exist_ok=True)
        return SubprocessShellSession(spec.env_id, spec.session_id, cwd, spec.env, spec.policy)


class SubprocessShellSession:
    def __init__(
        self,
        env_id: str,
        session_id: str,
        cwd: Path,
        env: tuple[EnvironmentVariable, ...],
        policy: ShellPolicy,
    ) -> None:
        self.env_id = env_id
        self.session_id = session_id
        self.cwd = cwd
        self.env = env
        self.policy = policy
        self._command_ids = StableIdFactory("cmd")
        self._process_ids = StableIdFactory("proc")
        self._processes: dict[str, _ProcessRecord] = {}
        self._history: list[CommandResult] = []

    async def teardown(self, reason: TeardownReason) -> ShellTeardownResult:
        terminated: list[str] = []
        for process_id, record in tuple(self._processes.items()):
            if record.process.returncode is None:
                record.process.terminate()
                terminated.append(process_id)
                try:
                    await asyncio.wait_for(record.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    record.process.kill()
                    await record.process.wait()
            self._processes.pop(process_id, None)
        return ShellTeardownResult(
            session_id=self.session_id,
            env_id=self.env_id,
            ok=True,
            reason=reason,
            terminated_processes=tuple(terminated),
        )

    async def run(self, command: ShellCommand) -> CommandResult:
        command_id = self._command_ids.next_id()
        started_at = time.monotonic()
        try:
            cwd = self._resolve_cwd(command.cwd)
            self._validate_command(command, cwd)
        except PolicyViolation as exc:
            result = _policy_result(command_id, command.argv, str(self.cwd), str(exc), started_at)
            self._history.append(result)
            return result
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=str(cwd),
            env=self._build_env(command.env),
            stdin=asyncio.subprocess.PIPE if command.stdin is not None else None,
            stdout=asyncio.subprocess.PIPE if command.capture_stdout else None,
            stderr=asyncio.subprocess.PIPE if command.capture_stderr else None,
        )
        stdin = _stdin_bytes(command.stdin)
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin),
                timeout=command.timeout_seconds or self.policy.timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        result = self._result_from_output(
            command_id=command_id,
            argv=command.argv,
            cwd=str(cwd),
            exit_code=process.returncode,
            started_at=started_at,
            stdout=stdout if command.capture_stdout else None,
            stderr=stderr if command.capture_stderr else None,
            timed_out=timed_out,
        )
        self._history.append(result)
        return result

    async def start(self, command: ShellCommand) -> ProcessHandle:
        if not self.policy.allow_background_processes:
            raise PolicyViolation("background processes denied by policy")
        active = [record for record in self._processes.values() if record.process.returncode is None]
        if len(active) >= self.policy.max_processes:
            raise PolicyViolation("max_processes exceeded")
        cwd = self._resolve_cwd(command.cwd)
        self._validate_command(command, cwd)
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=str(cwd),
            env=self._build_env(command.env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE if command.capture_stdout else None,
            stderr=asyncio.subprocess.PIPE if command.capture_stderr else None,
        )
        process_id = self._process_ids.next_id()
        self._processes[process_id] = _ProcessRecord(
            process_id=process_id,
            command_id=self._command_ids.next_id(),
            argv=command.argv,
            cwd=str(cwd),
            started_at=time.monotonic(),
            process=process,
            capture_stdout=command.capture_stdout,
            capture_stderr=command.capture_stderr,
        )
        if command.stdin is not None:
            await self.send_stdin(process_id, command.stdin)
        return ProcessHandle(process_id=process_id, argv=command.argv, cwd=str(cwd))

    async def send_stdin(self, process_id: str, data: bytes | str) -> None:
        record = self._require_process(process_id)
        if record.process.stdin is None:
            raise ActionFailed("process stdin is unavailable")
        record.process.stdin.write(_stdin_bytes(data) or b"")
        await record.process.stdin.drain()

    async def signal(self, process_id: str, signal: ProcessSignal) -> None:
        record = self._require_process(process_id)
        if signal == "terminate":
            record.process.terminate()
        elif signal == "kill":
            record.process.kill()
        elif signal == "interrupt":
            record.process.send_signal(signal_module.SIGINT)
        else:
            raise PolicyViolation(f"unsupported signal: {signal}")

    async def terminate(self, process: str | ProcessHandle) -> None:
        await self.signal(_process_id(process), "terminate")

    async def kill(self, process: str | ProcessHandle) -> None:
        await self.signal(_process_id(process), "kill")

    async def wait(self, process_id: str, timeout_seconds: float | None = None) -> CommandResult:
        record = self._require_process(process_id)
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                record.process.communicate(),
                timeout=timeout_seconds or self.policy.timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            record.process.kill()
            stdout, stderr = await record.process.communicate()
        result = self._result_from_output(
            command_id=record.command_id,
            argv=record.argv,
            cwd=record.cwd,
            exit_code=record.process.returncode,
            started_at=record.started_at,
            stdout=stdout if record.capture_stdout else None,
            stderr=stderr if record.capture_stderr else None,
            timed_out=timed_out,
        )
        self._history.append(result)
        self._processes.pop(process_id, None)
        return result

    async def running_processes(self) -> tuple[ProcessSummary, ...]:
        summaries: list[ProcessSummary] = []
        for record in self._processes.values():
            if record.process.returncode is None:
                summaries.append(
                    ProcessSummary(
                        process_id=record.process_id,
                        argv=record.argv,
                        cwd=record.cwd,
                        returncode=record.process.returncode,
                    )
                )
        return tuple(summaries)

    async def command_history(self) -> tuple[CommandResult, ...]:
        return tuple(self._history)

    def _validate_command(self, command: ShellCommand, cwd: Path) -> None:
        if not command.argv:
            raise PolicyViolation("argv command is required")
        executable = command.argv[0]
        if executable in self.policy.blocked_commands:
            raise PolicyViolation(f"command blocked by policy: {executable}")
        if self.policy.allowed_commands and executable not in self.policy.allowed_commands:
            raise PolicyViolation(f"command not allowed by policy: {executable}")
        if self.policy.path_allowlist:
            cwd_text = str(cwd)
            allowed = False
            for root in self.policy.path_allowlist:
                allowed_root = str(Path(root).expanduser().resolve())
                if cwd_text == allowed_root or cwd_text.startswith(f"{allowed_root}{os.sep}"):
                    allowed = True
                    break
            if not allowed:
                raise PolicyViolation(f"cwd denied by policy: {cwd}")
        for variable in command.env:
            if self.policy.env_allowlist and variable.name not in self.policy.env_allowlist:
                raise PolicyViolation(f"env variable denied by policy: {variable.name}")

    def _resolve_cwd(self, cwd: str | None) -> Path:
        if cwd is None:
            return self.cwd
        candidate = Path(cwd).expanduser()
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        return candidate.resolve()

    def _build_env(self, command_env: tuple[EnvironmentVariable, ...]) -> dict[str, str]:
        env = os.environ.copy()
        for variable in self.env:
            env[variable.name] = variable.value
        for variable in command_env:
            env[variable.name] = variable.value
        return env

    def _require_process(self, process_id: str) -> _ProcessRecord:
        record = self._processes.get(process_id)
        if record is None:
            raise ActionFailed(f"unknown process: {process_id}")
        return record

    def _result_from_output(
        self,
        *,
        command_id: str,
        argv: tuple[str, ...],
        cwd: str,
        exit_code: int | None,
        started_at: float,
        stdout: bytes | None,
        stderr: bytes | None,
        timed_out: bool,
    ) -> CommandResult:
        captured_stdout, stdout_truncated = _cap_output(stdout, self.policy.max_stdout_bytes)
        captured_stderr, stderr_truncated = _cap_output(stderr, self.policy.max_stderr_bytes)
        result_signal = None
        if exit_code is not None and exit_code < 0:
            result_signal = str(-exit_code)
        return CommandResult(
            command_id=command_id,
            argv=argv,
            cwd=cwd,
            exit_code=exit_code,
            signal=result_signal,
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            stdout=captured_stdout,
            stderr=captured_stderr,
            output_truncated=stdout_truncated or stderr_truncated,
            resource_usage=ResourceUsage(),
        )


def _stdin_bytes(data: str | bytes | None) -> bytes | None:
    if data is None:
        return None
    return data.encode("utf-8") if isinstance(data, str) else data


def _process_id(process: str | ProcessHandle) -> str:
    if isinstance(process, ProcessHandle):
        return process.process_id
    if isinstance(process, str):
        return process
    raise TypeError("process must be a process id or ProcessHandle")


def _cap_output(data: bytes | None, limit: int) -> tuple[bytes | None, bool]:
    if data is None:
        return None, False
    if len(data) <= limit:
        return data, False
    return data[:limit], True


def _policy_result(command_id: str, argv: tuple[str, ...], cwd: str, message: str, started_at: float) -> CommandResult:
    return CommandResult(
        command_id=command_id,
        argv=argv,
        cwd=cwd,
        exit_code=126,
        signal=None,
        timed_out=False,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        stdout=b"",
        stderr=message.encode("utf-8"),
        output_truncated=False,
        resource_usage=ResourceUsage(),
    )
