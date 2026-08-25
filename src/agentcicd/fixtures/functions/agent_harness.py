from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Tuple

from agentcicd.fixtures._attrs import callable_attr, read_attr
from agentcicd.fixtures.core.function import AsyncRowFunction
from agentcicd.fixtures.core.types import DType, FType, FloatType, JsonType, StringType
from agentcicd.fixtures.core.udf import Param, Udf
from agentcicd.fixtures.functions.mcp import McpServerConfig, coerce_mcp_servers


HARNESS_RUN_RESULT_TYPE_SQL = (
    "STRUCT<"
    "status: STRING, "
    "final_output: STRING, "
    "transcript: ARRAY<VARIANT>, "
    "artifacts: ARRAY<STRUCT<"
    "kind: STRING, "
    "uri: STRING, "
    "path: STRING, "
    "name: STRING, "
    "mime_type: STRING, "
    "size_bytes: BIGINT, "
    "metadata: VARIANT"
    ">>, "
    "error: STRUCT<code: STRING, message: STRING, retryable: BOOLEAN, metadata: VARIANT>, "
    "duration_ms: BIGINT, "
    "metadata: VARIANT"
    ">"
)

DEFAULT_OUTPUT_SIZE_LIMIT_BYTES = 64 * 1024
DEFAULT_ARTIFACT_SIZE_LIMIT_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class AgentHarnessSetupSpec:
    session_id: str
    harness: str
    workdir: str
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactReference:
    kind: str
    uri: str | None = None
    path: str | None = None
    name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterError:
    code: str
    message: str
    retryable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_exception(cls, exc: BaseException, *, code: str = "adapter_error", retryable: bool = False) -> "AdapterError":
        return cls(code=code, message=str(exc), retryable=retryable)


@dataclass(frozen=True)
class HarnessRunRequest:
    environment: AgentHarnessSetupSpec
    task: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    output_size_limit_bytes: int = DEFAULT_OUTPUT_SIZE_LIMIT_BYTES
    artifact_size_limit_bytes: int = DEFAULT_ARTIFACT_SIZE_LIMIT_BYTES
    transcript_file: str | None = None


@dataclass(frozen=True)
class HarnessRunResult:
    status: str
    final_output: str | None
    transcript: tuple[Any, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    error: AdapterError | None = None
    duration_ms: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _CodexRunContext:
    started: float
    config: dict[str, Any]
    executable: str
    workdir: str
    artifact_dir: Path
    last_message_path: Path
    command: list[str]
    env: dict[str, str]
    mcp_servers: tuple[McpServerConfig, ...]
    cleanup_paths: list[Path] = field(default_factory=list)


class AgentHarnessExecutionError(RuntimeError):
    def __init__(self, error: AdapterError, *, final_output: str | None = None, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.final_output = final_output
        self.metadata = dict(metadata or {})


@dataclass(frozen=True)
class HarnessTeardownStatus:
    status: str
    error: AdapterError | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AgentHarnessAdapter(Protocol):
    async def run_task(self, request: HarnessRunRequest) -> HarnessRunResult: ...
    async def teardown(self, setup: AgentHarnessSetupSpec, reason_code: str, message: str | None = None) -> HarnessTeardownStatus: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentHarnessAdapter] = {}

    def register(self, harness: str, adapter: AgentHarnessAdapter) -> None:
        name = _normalize_harness_name(harness)
        self._adapters[name] = adapter

    def unregister(self, harness: str) -> None:
        self._adapters.pop(_normalize_harness_name(harness), None)

    def resolve(self, harness: str) -> AgentHarnessAdapter:
        name = _normalize_harness_name(harness)
        adapter = self._adapters.get(name)
        if adapter is None:
            known = ", ".join(sorted(self._adapters)) or "none"
            raise ValueError(f"Unknown agent harness '{harness}'. Registered harnesses: {known}")
        return adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


class AgentHarnessSession:
    def __init__(self, setup: AgentHarnessSetupSpec, adapter: AgentHarnessAdapter) -> None:
        self.setup = setup
        self.adapter = adapter

    async def run_task(
        self,
        input: str | None = None,
        *,
        timeout: float | None = None,
        task: str | None = None,
        timeout_seconds: float | None = None,
        transcript_file: str | None = None,
    ) -> HarnessRunResult:
        resolved_input = input if input is not None else task
        if resolved_input is None:
            raise ValueError("agent_harness.run_task requires input")
        resolved_timeout = timeout if timeout is not None else timeout_seconds
        request = HarnessRunRequest(
            environment=self.setup,
            task=_coerce_task(resolved_input),
            timeout_seconds=_coerce_positive_float(resolved_timeout, "timeout", DEFAULT_TIMEOUT_SECONDS),
            transcript_file=_coerce_optional_workspace_path(transcript_file, "transcript_file"),
        )
        return await self.adapter.run_task(request)

    async def teardown(self, reason: Any) -> HarnessTeardownStatus:
        return await self.adapter.teardown(
            self.setup,
            reason_code=str(read_attr(reason, "code", "") or "teardown"),
            message=read_attr(reason, "message", None),
        )


class CodexCliAdapter:
    async def run_task(self, request: HarnessRunRequest) -> HarnessRunResult:
        context_or_error = _prepare_codex_run_context(request)
        if isinstance(context_or_error, HarnessRunResult):
            return context_or_error
        context = context_or_error
        try:
            completed = await _execute_codex_run(context, request)
        except asyncio.TimeoutError:
            return _codex_timeout_result(context, request)
        except Exception as exc:
            return _codex_error_result(context, exc)
        finally:
            _cleanup_paths(context.cleanup_paths)

        _write_transcript_file(request, context, completed)
        return await _codex_completed_result(context, request, completed)

    async def teardown(self, setup: AgentHarnessSetupSpec, reason_code: str, message: str | None = None) -> HarnessTeardownStatus:
        return HarnessTeardownStatus(status="completed", metadata={"reason_code": reason_code})


def _prepare_codex_run_context(request: HarnessRunRequest) -> _CodexRunContext | HarnessRunResult:
    started = time.monotonic()
    config = dict(request.environment.config)
    binary = _codex_binary(config)
    executable = _resolve_executable(binary)
    if executable is None:
        return HarnessRunResult(
            status="error",
            final_output=None,
            error=AdapterError(
                code="adapter_unavailable",
                message=f"Codex CLI binary not found: {binary}",
                retryable=False,
            ),
            duration_ms=_duration_ms(started),
        )

    workdir_path = Path(request.environment.workdir).expanduser()
    try:
        workdir_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return HarnessRunResult(
            status="error",
            final_output=None,
            error=AdapterError(
                code="workdir_unavailable",
                message=f"Could not create agent harness workdir {workdir_path}: {exc}",
                retryable=False,
            ),
            duration_ms=_duration_ms(started),
        )
    workdir = str(workdir_path)
    artifact_dir = Path(tempfile.mkdtemp(prefix="agentcicd-agent-harness-codex-"))
    last_message_path = artifact_dir / "codex-last-message.txt"
    command = _codex_command(executable, workdir, config, last_message_path)
    mcp_servers = coerce_mcp_servers(config.get("mcps"))
    env, cleanup_paths = _codex_environment(config, workdir, artifact_dir, request.task, mcp_servers)
    return _CodexRunContext(
        started=started,
        config=config,
        executable=executable,
        workdir=workdir,
        artifact_dir=artifact_dir,
        last_message_path=last_message_path,
        command=command,
        env=env,
        mcp_servers=mcp_servers,
        cleanup_paths=cleanup_paths,
    )


def _codex_binary(config: Mapping[str, Any]) -> str:
    return str(
        config.get("binary")
        or os.getenv("AGENTCICD_AGENT_HARNESS_CODEX_BINARY")
        or os.getenv("AGENTCICD_API_AGENT_CODEX_BINARY")
        or "codex"
    )


def _resolve_executable(binary: str) -> str | None:
    executable = shutil.which(binary) if os.path.basename(binary) == binary else binary
    if not executable:
        return None
    if os.path.sep in executable and not Path(executable).exists():
        return None
    return executable


def _codex_command(executable: str, workdir: str, config: Mapping[str, Any], last_message_path: Path) -> list[str]:
    command = [
        executable,
        "exec",
        "--cd",
        workdir,
        "--sandbox",
        str(config.get("sandbox") or "workspace-write"),
        "-c",
        f"approval_policy={json.dumps(str(config.get('approval_mode') or config.get('ask_for_approval') or 'never'))}",
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        "--output-last-message",
        str(last_message_path),
    ]
    model = str(config.get("model") or "").strip()
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def _codex_environment(
    config: Mapping[str, Any],
    workdir: str,
    artifact_dir: Path,
    task: str,
    mcp_servers: tuple[McpServerConfig, ...],
) -> tuple[dict[str, str], list[Path]]:
    env = os.environ.copy()
    _apply_mapping_env(env, config.get("env"))
    auth = config.get("auth") if isinstance(config.get("auth"), Mapping) else None
    _apply_codex_auth_env(env, auth)
    cleanup_paths = _apply_codex_home(env, auth, mcp_servers)
    env.setdefault("AGENTCICD_WORKSPACE", workdir)
    env.setdefault("AGENTCICD_ARTIFACTS", str(artifact_dir))
    env.setdefault("AGENTCICD_TASK_PROMPT", task)
    return env, cleanup_paths


def _apply_mapping_env(env: dict[str, str], value: Any) -> None:
    if isinstance(value, Mapping):
        env.update({str(key): str(item) for key, item in value.items()})


def _apply_codex_auth_env(env: dict[str, str], auth: Mapping[str, Any] | None) -> None:
    if not isinstance(auth, Mapping):
        return
    _apply_mapping_env(env, auth.get("env"))
    for source_key, env_key in (("codex_home", "CODEX_HOME"), ("codex_auth_file", "CODEX_AUTH_FILE")):
        value = auth.get(source_key)
        if isinstance(value, str) and value.strip():
            env[env_key] = value.strip()


def _apply_codex_home(
    env: dict[str, str],
    auth: Mapping[str, Any] | None,
    mcp_servers: tuple[McpServerConfig, ...],
) -> list[Path]:
    codex_home_files = _coerce_codex_home_files(auth)
    if not mcp_servers and not codex_home_files:
        return []
    codex_home, mcp_env = _prepare_codex_home(mcp_servers, auth=auth)
    env.update(mcp_env)
    env["CODEX_HOME"] = str(codex_home)
    return [codex_home]


async def _execute_codex_run(
    context: _CodexRunContext,
    request: HarnessRunRequest,
) -> dict[str, Any]:
    return await _run_subprocess(
        context.command,
        request.task,
        cwd=context.workdir,
        env=context.env,
        limit_bytes=request.output_size_limit_bytes,
        timeout_seconds=request.timeout_seconds,
    )


def _codex_timeout_result(context: _CodexRunContext, request: HarnessRunRequest) -> HarnessRunResult:
    return HarnessRunResult(
        status="timeout",
        final_output=None,
        artifacts=tuple(_collect_artifacts(context.artifact_dir, request.artifact_size_limit_bytes, include_diff=False)),
        error=AdapterError(code="timeout", message="Agent harness task timed out", retryable=True),
        duration_ms=_duration_ms(context.started),
    )


def _codex_error_result(context: _CodexRunContext, exc: Exception) -> HarnessRunResult:
    return HarnessRunResult(
        status="error",
        final_output=None,
        error=AdapterError.from_exception(exc),
        duration_ms=_duration_ms(context.started),
    )


def _write_transcript_file(
    request: HarnessRunRequest,
    context: _CodexRunContext,
    completed: Mapping[str, Any],
) -> None:
    if not request.transcript_file:
        return
    transcript_path = _resolve_workspace_path(context.workdir, request.transcript_file)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_bytes = completed.get("stdout_bytes")
    if not isinstance(stdout_bytes, (bytes, bytearray)):
        stdout_bytes = str(completed.get("stdout") or "").encode("utf-8")
    transcript_path.write_bytes(bytes(stdout_bytes))


async def _codex_completed_result(
    context: _CodexRunContext,
    request: HarnessRunRequest,
    completed: Mapping[str, Any],
) -> HarnessRunResult:
    final_output = _read_text(context.last_message_path, request.output_size_limit_bytes)
    capture_metadata = await _capture_mcp_artifacts(context.mcp_servers, context.artifact_dir)
    artifacts = tuple(
        _collect_artifacts(
            context.artifact_dir,
            request.artifact_size_limit_bytes,
            include_diff=True,
            workdir=context.workdir,
        )
    )
    returncode = int(completed["returncode"])
    if returncode == 0:
        return HarnessRunResult(
            status="completed",
            final_output=final_output,
            artifacts=artifacts,
            duration_ms=_duration_ms(context.started),
            metadata=_codex_run_metadata(context, request, completed, capture_metadata),
        )
    raise _codex_process_failed(context, request, completed, final_output, artifacts, capture_metadata)


def _codex_run_metadata(
    context: _CodexRunContext,
    request: HarnessRunRequest,
    completed: Mapping[str, Any],
    capture_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "harness": "codex",
        "returncode": int(completed["returncode"]),
        "mcp_servers": [server.name for server in context.mcp_servers],
        "transcript_file": request.transcript_file,
        "mcp_capture": capture_metadata,
    }


def _codex_process_failed(
    context: _CodexRunContext,
    request: HarnessRunRequest,
    completed: Mapping[str, Any],
    final_output: str | None,
    artifacts: tuple[ArtifactReference, ...],
    capture_metadata: list[dict[str, Any]],
) -> AgentHarnessExecutionError:
    stderr = str(completed.get("stderr") or "").strip()
    stdout = str(completed.get("stdout") or "").strip()
    returncode = int(completed["returncode"])
    detail = stderr or final_output or stdout
    message = f"Codex CLI exited with status {returncode}"
    if detail:
        message = f"{message}: {detail}"
    metadata = _codex_run_metadata(context, request, completed, capture_metadata)
    metadata.update(
        {
            "artifacts": [_json_safe(artifact) for artifact in artifacts],
            "duration_ms": _duration_ms(context.started),
            "stderr": _truncate_text(stderr, request.output_size_limit_bytes) if stderr else None,
            "stdout": _truncate_text(stdout, request.output_size_limit_bytes) if stdout else None,
            "final_output": _truncate_text(final_output, request.output_size_limit_bytes) if final_output else None,
        }
    )
    return AgentHarnessExecutionError(
        AdapterError(
            code="adapter_process_failed",
            message=_truncate_text(message, request.output_size_limit_bytes),
            retryable=False,
            metadata={"returncode": returncode},
        ),
        final_output=final_output,
        metadata=metadata,
    )


class AgentHarnessRunTaskRowFunction(AsyncRowFunction):
    async def transform(
        self,
        environment: Any,
        task: str,
        timeout_seconds: float | None = None,
        transcript_file: str | None = None,
        pool: Any = None,
        limiter: Any = None,
    ) -> dict[str, Any]:
        if _is_lazy_agent_harness_environment(environment):
            return await environment.run_task(
                input=task,
                timeout=timeout_seconds,
                transcript_file=transcript_file,
            )
        session, one_shot = _coerce_harness_session(environment)
        try:
            result = await session.run_task(
                input=task,
                timeout=timeout_seconds,
                transcript_file=transcript_file,
            )
            return harness_result_to_dict(result)
        finally:
            if one_shot:
                await session.teardown(type("Reason", (), {"code": "completed", "message": None})())


class EnvsAgentHarnessRunTaskRowFunction(AgentHarnessRunTaskRowFunction):
    async def transform(
        self,
        env: Any,
        task: str,
        timeout_seconds: float | None = None,
        transcript_file: str | None = None,
        pool: Any = None,
        limiter: Any = None,
    ) -> dict[str, Any]:
        return await super().transform(
            env,
            task,
            timeout_seconds=timeout_seconds,
            transcript_file=transcript_file,
            pool=pool,
            limiter=limiter,
        )


class EnvsAgentHarnessRunTaskUdf(Udf, name="envs.agent_harness.run_task"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (JsonType(), StringType(), FloatType(), StringType(), JsonType(), JsonType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("env", required=True, type_sql="VARIANT"),
            Param("task", required=True, type_sql="STRING"),
            Param("timeout_seconds", required=False, type_sql="DOUBLE", default_value=DEFAULT_TIMEOUT_SECONDS),
            Param("transcript_file", required=False, type_sql="STRING", default_value=None),
            Param("pool", required=False, type_sql="POOL"),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def metadata(self) -> dict[str, object]:
        return {
            "return_type_sql": HARNESS_RUN_RESULT_TYPE_SQL,
            "output_type": "variant",
            "execution_runtime": "function_runner",
            "entrypoint_name": "run_task",
            "pool_kind": "session",
        }

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Any]:
        return EnvsAgentHarnessRunTaskRowFunction


_REGISTRY = AdapterRegistry()


def get_adapter_registry() -> AdapterRegistry:
    return _REGISTRY


def create_session(setup: AgentHarnessSetupSpec, registry: AdapterRegistry | None = None) -> AgentHarnessSession:
    active_registry = registry or _REGISTRY
    return AgentHarnessSession(setup=setup, adapter=active_registry.resolve(setup.harness))


def harness_result_to_dict(result: HarnessRunResult) -> dict[str, Any]:
    return _json_safe(
        {
            "status": result.status,
            "final_output": result.final_output,
            "transcript": list(result.transcript),
            "artifacts": list(result.artifacts),
            "error": result.error,
            "duration_ms": result.duration_ms,
            "metadata": dict(result.metadata),
        }
    )


def setup_spec_from_payload(payload: Mapping[str, Any]) -> AgentHarnessSetupSpec:
    config = dict(payload.get("config") or {})
    return AgentHarnessSetupSpec(
        session_id=str(payload.get("session_id") or payload.get("env_id") or "").strip(),
        harness=_normalize_harness_name(payload.get("harness")),
        workdir=str(payload.get("workdir") or "."),
        config=config,
    )


def setup_spec_from_aisystem_payload(payload: Mapping[str, Any]) -> AgentHarnessSetupSpec:
    aisystem_id = str(payload.get("aisystem") or payload.get("aisystem_id") or "").strip()
    if not aisystem_id:
        raise ValueError("envs.agent_harness.spec requires aisystem")
    secret_id = str(payload.get("secret_id") or "").strip() or None
    from agentcicd.fixtures.functions.utils.runtime_context import AISystemRuntimeResolver

    resolved = AISystemRuntimeResolver().resolve_agent_harness_payload(aisystem_id, secret_id=secret_id)
    config = dict(resolved.config)
    mcps = payload.get("mcps")
    if mcps is not None:
        config["mcps"] = _json_safe(mcps)
    return AgentHarnessSetupSpec(
        session_id=str(payload.get("session_id") or payload.get("env_id") or "").strip(),
        harness=resolved.harness,
        workdir=str(payload.get("workdir") or "."),
        config=config,
    )


def _coerce_harness_session(value: Any) -> tuple[AgentHarnessSession, bool]:
    value = _coerce_serialized_environment(value)
    if isinstance(value, AgentHarnessSession):
        return value, False
    if isinstance(value, Mapping):
        if value.get("spec_type") == "environment":
            from agentcicd.fixtures.functions.simulators import EnvironmentSpec

            spec = EnvironmentSpec(
                kind=str(value.get("kind") or ""),
                env_id=str(value.get("env_id") or ""),
                config=dict(value.get("config") or {}),
            )
            if "aisystem" in spec.config or "aisystem_id" in spec.config:
                return create_session(setup_spec_from_aisystem_payload({"session_id": spec.env_id, **spec.config})), True
            return create_session(setup_spec_from_payload({"session_id": spec.env_id, **spec.config})), True
        return create_session(setup_spec_from_payload(value)), True
    raise ValueError("environment must be an envs.agent_harness.spec object or runtime harness session")


def _coerce_serialized_environment(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value
        return parsed
    as_dict = callable_attr(value, "asDict")
    if as_dict is not None:
        try:
            return as_dict(recursive=True)
        except TypeError:
            return as_dict()
    to_python = callable_attr(value, "toPython")
    if to_python is not None:
        try:
            return _coerce_serialized_environment(to_python())
        except Exception:
            return value
    to_json = callable_attr(value, "toJson")
    if to_json is not None:
        try:
            return _coerce_serialized_environment(to_json())
        except Exception:
            return value
    return value


def _is_lazy_agent_harness_environment(value: Any) -> bool:
    return (
        bool(read_attr(value, "__agentcicd_lazy_environment__", False))
        and str(read_attr(value, "kind", "")).strip().lower() == "agent_harness"
        and callable_attr(value, "run_task") is not None
    )


def _normalize_harness_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    if not name:
        raise ValueError("harness is required")
    return name


def _coerce_task(value: Any) -> str:
    task = str(value or "")
    if not task.strip():
        raise ValueError("task is required")
    return task


def _coerce_positive_float(value: Any, field_name: str, default: float) -> float:
    raw = default if value is None else value
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return parsed


def _coerce_positive_int(value: Any, field_name: str, default: int) -> int:
    raw = default if value is None else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return parsed


async def _run_subprocess(
    command: list[str],
    stdin_text: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    limit_bytes: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(stdin_text.encode("utf-8")), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise
    return {
        "returncode": int(process.returncode or 0),
        "stdout_bytes": stdout_raw,
        "stdout": _decode_limited(stdout_raw, limit_bytes),
        "stderr": _decode_limited(stderr_raw, limit_bytes),
    }


async def _capture_mcp_artifacts(
    servers: tuple[McpServerConfig, ...],
    artifact_dir: Path,
) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
    for server in servers:
        for request in _playwright_capture_requests(server):
            path = str(request["path"])
            target = artifact_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                result = await _run_playwright_capture(server, {**request, "path": str(target)})
            except Exception as exc:
                captures.append(
                    {
                        "server": server.name,
                        "kind": request["kind"],
                        "path": str(target),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
            else:
                captures.append(
                    {
                        "server": server.name,
                        "kind": request["kind"],
                        "path": str(target),
                        "status": "completed",
                        "result": _json_safe(result),
                    }
                )
    return captures


def _playwright_capture_requests(server: McpServerConfig) -> tuple[dict[str, Any], ...]:
    playwright = server.metadata.get("playwright") if isinstance(server.metadata, Mapping) else None
    if not isinstance(playwright, Mapping):
        return ()
    capture_requests = playwright.get("capture_requests")
    requests: list[dict[str, Any]] = []
    if isinstance(capture_requests, list) and capture_requests:
        for request in capture_requests:
            if not isinstance(request, Mapping) or str(request.get("kind") or "") != "screenshot":
                continue
            path = str(request.get("path") or "").strip()
            if not path:
                continue
            requests.append({"kind": "screenshot", "path": path, "full_page": bool(request.get("full_page", True))})
    if bool(playwright.get("capture_final_screenshot")):
        filename = str(playwright.get("final_screenshot_filename") or "fixture-final.png").strip() or "fixture-final.png"
        requests.append({"kind": "screenshot", "path": filename, "full_page": True})
    return tuple(requests)


async def _run_playwright_capture(server: McpServerConfig, request: Mapping[str, Any]) -> Any:
    from agentcicd.fixtures.functions.simulators import materialized_mcp_from_spec

    spec = _mcp_server_to_spec(server)
    handle = materialized_mcp_from_spec(spec)
    try:
        if request.get("kind") == "screenshot":
            return await handle.call_tool(
                "browser_take_screenshot",
                {
                    "filename": str(request["path"]),
                    "fullPage": bool(request.get("full_page", True)),
                },
            )
        raise ValueError(f"unsupported Playwright capture kind: {request.get('kind')}")
    finally:
        await handle.teardown(type("Reason", (), {"code": "completed", "message": None})())


def _mcp_server_to_spec(server: McpServerConfig) -> dict[str, Any]:
    if server.transport == "http":
        return {
            "spec_type": "mcp",
            "transport": "http",
            "name": server.name,
            "endpoint": server.endpoint,
            "required": server.required,
            "headers": dict(server.headers),
            "metadata": dict(server.metadata),
        }
    return {
        "spec_type": "mcp",
        "transport": "stdio",
        "name": server.name,
        "command": server.command,
        "args": list(server.args),
        "required": server.required,
        "env": dict(server.env),
        "metadata": dict(server.metadata),
    }


_CODEX_HOME_FILE_NAMES = {"auth.json", "config.toml"}


def _prepare_codex_home(
    servers: tuple[McpServerConfig, ...],
    *,
    auth: Mapping[str, Any] | None,
) -> tuple[Path, dict[str, str]]:
    codex_home_parent = Path(os.environ.get("AGENTCICD_CODEX_HOME_PARENT") or "/workspace/.agentcicd/codex-home")
    try:
        codex_home_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        codex_home_parent = Path.cwd() / ".agentcicd" / "codex-home"
        codex_home_parent.mkdir(parents=True, exist_ok=True)
    codex_home = Path(tempfile.mkdtemp(prefix="agentcicd-codex-home-", dir=str(codex_home_parent)))
    codex_home.chmod(0o700)
    _write_codex_home_files(codex_home, auth)
    _copy_codex_auth_files(codex_home, auth)
    env: dict[str, str] = {}
    if servers:
        config_text, env = _codex_mcp_config_toml(servers)
        config_path = codex_home / "config.toml"
        if config_path.exists():
            existing = config_path.read_text(encoding="utf-8")
            config_text = existing.rstrip() + "\n\n" + config_text
        _write_private_file(config_path, config_text)
    return codex_home, env


def _write_codex_home_files(codex_home: Path, auth: Mapping[str, Any] | None) -> None:
    for name, content in _coerce_codex_home_files(auth).items():
        _write_private_file(codex_home / name, content)


def _coerce_codex_home_files(auth: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(auth, Mapping):
        return {}
    raw_files = auth.get("codex_home_files")
    if not isinstance(raw_files, Mapping):
        return {}
    files: dict[str, str] = {}
    for key, value in raw_files.items():
        name = str(key or "").strip()
        if name not in _CODEX_HOME_FILE_NAMES or not isinstance(value, str):
            continue
        files[name] = value
    return files


def _write_private_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _copy_codex_auth_files(codex_home: Path, auth: Mapping[str, Any] | None) -> None:
    if not isinstance(auth, Mapping):
        return
    target = codex_home / "auth.json"
    if target.exists():
        return
    source_paths: list[Path] = []
    codex_auth_file = auth.get("codex_auth_file")
    if isinstance(codex_auth_file, str) and codex_auth_file.strip():
        source_paths.append(Path(codex_auth_file.strip()).expanduser())
    source_home = auth.get("codex_home")
    if isinstance(source_home, str) and source_home.strip():
        source_paths.append(Path(source_home.strip()).expanduser() / "auth.json")
    for source in source_paths:
        if source.exists() and source.is_file():
            shutil.copy2(source, codex_home / "auth.json")
            target.chmod(0o600)
            break


def _codex_mcp_config_toml(servers: tuple[McpServerConfig, ...]) -> tuple[str, dict[str, str]]:
    env: dict[str, str] = {}
    lines: list[str] = []
    for server in servers:
        server_env, table = _codex_mcp_server_table(server)
        env.update(server_env)
        lines.append(f"[mcp_servers.{_toml_key(server.name)}]")
        for key, value in table.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines), env


def _codex_mcp_server_table(server: McpServerConfig) -> tuple[dict[str, str], dict[str, Any]]:
    from agentcicd.fixtures.functions.utils.runtime_context import resolve_secret_record

    env: dict[str, str] = {}
    if server.transport == "stdio":
        table: dict[str, Any] = {
            "command": server.command,
            "args": list(server.args),
            "required": server.required,
        }
        if server.env:
            table["env"] = dict(server.env)
        if server.allow_tools:
            table["enabled_tools"] = list(server.allow_tools)
        if server.deny_tools:
            table["disabled_tools"] = list(server.deny_tools)
        if server.default_tools_approval_mode:
            table["default_tools_approval_mode"] = server.default_tools_approval_mode
        return env, table

    table = {
        "url": server.endpoint,
        "required": server.required,
    }
    if server.allow_tools:
        table["enabled_tools"] = list(server.allow_tools)
    if server.deny_tools:
        table["disabled_tools"] = list(server.deny_tools)
    if server.default_tools_approval_mode:
        table["default_tools_approval_mode"] = server.default_tools_approval_mode
    secret_record = resolve_secret_record(server.secret_id)
    secret_payload = _secret_payload(secret_record)
    bearer_token = _mcp_bearer_token_from_secret(secret_payload)
    if bearer_token:
        env_name = f"AGENTCICD_MCP_{_env_token(server.name)}_BEARER_TOKEN"
        env[env_name] = bearer_token
        table["bearer_token_env_var"] = env_name
    env_headers: dict[str, str] = {}
    all_headers = {**_mcp_headers_from_secret(secret_payload), **dict(server.headers)}
    for header_name, header_value in all_headers.items():
        env_name = f"AGENTCICD_MCP_{_env_token(server.name)}_{_env_token(header_name)}"
        env[env_name] = header_value
        env_headers[header_name] = env_name
    if env_headers:
        table["env_http_headers"] = env_headers
    return env, table


def _secret_payload(secret_record: Mapping[str, Any]) -> Mapping[str, Any]:
    secret = secret_record.get("secret")
    if isinstance(secret, Mapping):
        return secret
    return secret_record


def _mcp_bearer_token_from_secret(secret: Mapping[str, Any]) -> str:
    for key in ("bearer_token", "token", "access_token", "api_key"):
        value = secret.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _mcp_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    for key in ("headers", "http_headers"):
        value = secret.get(key)
        if isinstance(value, Mapping):
            return _coerce_string_mapping(value, field_name=key)
    return {}


def _coerce_optional_workspace_path(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return None
    if text.startswith("/") or text.startswith("../") or "/../" in f"/{text}/" or text.startswith("~/"):
        raise ValueError(f"{field_name} must be a relative workspace path")
    return text.strip("/")


def _resolve_workspace_path(workdir: str, relative_path: str) -> Path:
    root = Path(workdir).expanduser().resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("transcript_file must stay inside the workspace")
    return target


def _coerce_string_mapping(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        text_key = str(key or "").strip()
        text_value = str(item or "").strip()
        if text_key and text_value:
            result[text_key] = text_value
    return result


def _env_token(value: str) -> str:
    chars: list[str] = []
    for char in value.upper():
        chars.append(char if char.isalnum() else "_")
    return "".join(chars).strip("_") or "VALUE"


def _toml_key(value: str) -> str:
    return json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        entries = [f"{_toml_key(str(key))} = {_toml_value(item)}" for key, item in value.items()]
        return "{ " + ", ".join(entries) + " }"
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError(f"Unsupported TOML value type: {type(value).__name__}")


def _cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def _collect_artifacts(
    artifact_dir: Path,
    artifact_size_limit_bytes: int,
    *,
    include_diff: bool,
    workdir: str | None = None,
) -> list[ArtifactReference]:
    if include_diff and workdir:
        _write_git_diff_artifact(Path(workdir), artifact_dir, artifact_size_limit_bytes)
    artifacts: list[ArtifactReference] = []
    if not artifact_dir.exists():
        return artifacts
    for path in sorted(item for item in artifact_dir.rglob("*") if item.is_file()):
        size = path.stat().st_size
        if size > artifact_size_limit_bytes:
            continue
        artifacts.append(
            ArtifactReference(
                kind="file",
                uri=path.as_uri(),
                path=str(path),
                name=path.name,
                mime_type="text/plain" if path.suffix in {".txt", ".diff", ".jsonl"} else None,
                size_bytes=size,
            )
        )
    return artifacts


def _write_git_diff_artifact(workdir: Path, artifact_dir: Path, artifact_size_limit_bytes: int) -> None:
    if not (workdir / ".git").exists():
        return
    try:
        completed = shutil.which("git")
        if not completed:
            return
        result = __import__("subprocess").run(
            [completed, "-C", str(workdir), "diff", "--no-ext-diff", "--binary"],
            check=False,
            stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").DEVNULL,
            timeout=10,
        )
    except Exception:
        return
    if not result.stdout or len(result.stdout) > artifact_size_limit_bytes:
        return
    (artifact_dir / "workspace.diff").write_bytes(result.stdout)


def _read_text(path: Path, limit_bytes: int) -> str | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return _decode_limited(raw, limit_bytes).strip() or None


def _decode_limited(raw: bytes, limit_bytes: int) -> str:
    return raw[:limit_bytes].decode("utf-8", errors="replace")


def _truncate_text(text: str, limit_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    return encoded[:limit_bytes].decode("utf-8", errors="replace")


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


_REGISTRY.register("codex", CodexCliAdapter())
