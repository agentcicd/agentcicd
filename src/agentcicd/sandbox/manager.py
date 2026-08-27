from __future__ import annotations

import concurrent.futures
import json
import os
import select
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside runtime images
    object_store_from_env = None  # type: ignore[assignment]


TRACE_SCHEMA_VERSION = "agentcicd.fixture_trace.v1"


class InvocationTimeoutError(TimeoutError):
    def __init__(self, message: str, *, trace_summary: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.trace_summary = trace_summary or None


class InvocationFailedError(RuntimeError):
    def __init__(self, message: str, *, code: str = "invoke_failed", trace_summary: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.trace_summary = trace_summary or None


@dataclass(frozen=True)
class ManagerConfig:
    fixture_id: str
    function_name: str
    pool_name: str
    pool_kind: str
    manager_id: str
    generation: int
    address: str
    max_workers: int
    require_lease: bool
    debug: bool
    fixture_ids: tuple[str, ...] = ()
    function_names: tuple[str, ...] = ()
    fixture_worker_images: dict[str, str] = field(default_factory=dict)
    driver_base_url: str = ""
    heartbeat_ttl_seconds: float = 60.0
    min_warm: int = 0
    call_timeout_seconds: float = 300.0
    session_idle_ttl_seconds: float = 900.0
    worker_create_rate_limit_per_second: float = 0.0


@dataclass(frozen=True)
class InvocationLease:
    lease_id: str
    pool_name: str
    pool_kind: str
    manager_id: str
    worker_slot_id: str
    fixture_id: str
    generation: int
    request_id: str = ""


@dataclass
class WorkerRecord:
    worker_id: str
    slot_id: str
    session_key: str = ""
    fixture_id: str = ""
    image: str = ""
    invocation_count: int = 0
    healthy: bool = True
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    warm: bool = False
    acquire_decision: str = "start_compatible"
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    address: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass(frozen=True)
class DockerWorkerConfig:
    image: str
    docker_host: str = ""
    network: str = ""
    cpu: str = ""
    memory: str = ""
    tmpfs_size: str = ""
    source_path: str = ""
    worker_source_path: str = ""
    source_paths: tuple[str, ...] = ()
    worker_source_paths: tuple[str, ...] = ()
    create_timeout_seconds: float = 60.0
    start_timeout_seconds: float = 60.0
    remove_timeout_seconds: float = 30.0
    extra_env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HelperConfig:
    mode: Literal["command", "endpoint"]
    command: tuple[str, ...] = ()
    endpoint: str = ""
    timeout_seconds: float = 60.0


@dataclass
class ManagerMetrics:
    invocations: int = 0
    failures: int = 0
    timeouts: int = 0
    cleanup_failures: int = 0
    worker_create_count: int = 0
    worker_failure_count: int = 0
    total_worker_startup_ms: int = 0
    total_invocation_ms: int = 0
    total_cleanup_ms: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "invocations": self.invocations,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "cleanup_failures": self.cleanup_failures,
            "worker_create_count": self.worker_create_count,
            "worker_failure_count": self.worker_failure_count,
            "total_worker_startup_ms": self.total_worker_startup_ms,
            "total_invocation_ms": self.total_invocation_ms,
            "total_cleanup_ms": self.total_cleanup_ms,
        }


class WorkerLifecycle(Protocol):
    def create(self, slot_id: str, *, session_key: str = "", fixture_id: str = "", image: str = "") -> WorkerRecord:
        ...

    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        ...

    def stop(self, worker: WorkerRecord, *, reason: str) -> None:
        ...

    def clear(self, worker: WorkerRecord, *, reason: str) -> None:
        ...

    def status(self, worker: WorkerRecord) -> dict[str, Any]:
        ...


class SubprocessFunctionWorkerLifecycle:
    """Worker substrate that keeps function_runner alive as a local HTTP server."""

    def create(self, slot_id: str, *, session_key: str = "", fixture_id: str = "", image: str = "") -> WorkerRecord:
        worker = WorkerRecord(worker_id=f"worker.{secrets.token_hex(8)}", slot_id=slot_id, session_key=session_key, fixture_id=fixture_id, image=image)
        self._attach_process(worker, self._start_process())
        return worker

    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        timeout_seconds = max(0.1, float(os.getenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", "300")))
        payload = {"args": arguments, "trace": trace, "secrets": secrets_payload}
        started_at = time.monotonic()
        collector = _TraceFrameCollector(trace)
        with worker.lock:
            process = self._healthy_process(worker)
            address = str(worker.address or "").rstrip("/")
            if not address:
                worker.healthy = False
                raise InvocationFailedError("Worker HTTP address is not available")
            request = Request(
                f"{address}/invoke/{function_name}",
                data=json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                    response_payload = json.loads(response.read().decode("utf-8") or "{}")
            except HTTPError as exc:
                response_payload = _read_http_error_payload(exc)
                summary = collector.write_summary(
                    status="error",
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    error_code=str(response_payload.get("error") or "invoke_failed"),
                    error_message=str(response_payload.get("detail") or response_payload.get("error") or "Worker invocation failed"),
                    error_type="RuntimeError",
                    http_status=exc.code,
                )
                raise InvocationFailedError(
                    str(response_payload.get("detail") or response_payload.get("error") or "Worker invocation failed"),
                    code=str(response_payload.get("error") or "invoke_failed"),
                    trace_summary=summary,
                ) from exc
            except TimeoutError as exc:
                process.kill()
                worker.healthy = False
                summary = collector.write_summary(
                    status="error",
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    error_code="AGENTCICD_RUNTIME_TIMEOUT",
                    error_message=f"Fixture invocation exceeded {timeout_seconds:g}s",
                    error_type="TimeoutError",
                    http_status=408,
                )
                raise InvocationTimeoutError(f"Fixture invocation exceeded {timeout_seconds:g}s", trace_summary=summary) from exc
            except Exception as exc:
                if process.poll() is not None:
                    stderr = self._collect_stderr(process)
                    detail = stderr or f"Worker exited with returncode={process.returncode}"
                else:
                    detail = str(exc)
                worker.healthy = False
                summary = collector.write_summary(
                    status="error",
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    error_code="invoke_failed",
                    error_message=detail,
                    error_type=type(exc).__name__,
                    http_status=400,
                )
                raise InvocationFailedError(detail, trace_summary=summary) from exc
        duration_ms = int((time.monotonic() - started_at) * 1000)
        for record in response_payload.get("trace_records") or []:
            collector.add_record(record)
        summary = collector.write_summary(status="ok", duration_ms=duration_ms)
        worker.invocation_count += 1
        worker.last_used_at = time.monotonic()
        payload_out: dict[str, Any] = {"result": response_payload.get("result")}
        if summary:
            payload_out["trace_summary"] = summary
        return payload_out

    def stop(self, worker: WorkerRecord, *, reason: str) -> None:
        self._stop_process(worker)
        worker.healthy = False
        worker.last_used_at = time.monotonic()

    def clear(self, worker: WorkerRecord, *, reason: str) -> None:
        self._stop_process(worker)
        self._attach_process(worker, self._start_process())
        worker.last_used_at = time.monotonic()

    def status(self, worker: WorkerRecord) -> dict[str, Any]:
        process = worker.process
        if process is not None and process.poll() is not None:
            worker.healthy = False
        return {"healthy": worker.healthy}

    def _healthy_process(self, worker: WorkerRecord) -> subprocess.Popen[str]:
        process = worker.process
        if process is None or process.poll() is not None:
            self._attach_process(worker, self._start_process())
            worker.healthy = True
        assert worker.process is not None
        return worker.process

    @classmethod
    def _attach_process(cls, worker: WorkerRecord, process_with_address: tuple[subprocess.Popen[str], str]) -> None:
        process, address = process_with_address
        worker.process = process
        worker.address = address

    @staticmethod
    def _start_process() -> tuple[subprocess.Popen[str], str]:
        port = _free_tcp_port()
        address = f"http://127.0.0.1:{port}"
        command = [sys_executable(), "-m", "agentcicd.sandbox.function_runner", "--port", str(port)]
        process = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        try:
            _wait_for_http_server(f"{address}/health", process, timeout_seconds=10.0)
        except Exception:
            if process.poll() is None:
                process.kill()
            raise
        return process, address

    @staticmethod
    def _collect_stderr(process: subprocess.Popen[str]) -> str:
        try:
            _stdout, stderr = process.communicate(timeout=1)
            return stderr.strip()
        except Exception:
            return ""

    @staticmethod
    def _stop_process(worker: WorkerRecord) -> None:
        process = worker.process
        worker.process = None
        worker.address = ""
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                process.kill()
        try:
            process.communicate(timeout=1)
        except Exception:
            pass


class DockerWorkerLifecycle:
    """Worker lifecycle backed by a pod-local Docker daemon.

    The expected local-Kubernetes deployment shape is a sandbox-manager container
    plus a Docker-in-Docker sidecar. This lifecycle talks to that sidecar through
    the Docker CLI and keeps service/session workers as long-lived containers.
    """

    def __init__(self, config: DockerWorkerConfig) -> None:
        if not config.image:
            raise ValueError("Docker worker image is required")
        self.config = config

    def create(self, slot_id: str, *, session_key: str = "", fixture_id: str = "", image: str = "") -> WorkerRecord:
        worker_id = _docker_container_name(slot_id)
        worker_image = str(image or self.config.image).strip()
        create_command = [
            "docker",
            "create",
            "--name",
            worker_id,
            "--label",
            "agentcicd.sandbox.worker=true",
            "--label",
            f"agentcicd.sandbox.slot={slot_id}",
        ]
        if self.config.network:
            create_command.extend(["--network", self.config.network])
        if self.config.cpu:
            create_command.extend(["--cpus", self.config.cpu])
        if self.config.memory:
            create_command.extend(["--memory", self.config.memory])
        if self.config.tmpfs_size:
            create_command.extend(["--tmpfs", f"/tmp:size={self.config.tmpfs_size}"])
        for name, value in self.config.extra_env:
            create_command.extend(["--env", f"{name}={value}"])
        create_command.extend([worker_image, "sleep", "infinity"])
        self._run_docker(create_command, timeout_seconds=self.config.create_timeout_seconds)
        try:
            source_pairs = _docker_source_copy_pairs(self.config)
            self._run_docker(["docker", "start", worker_id], timeout_seconds=self.config.start_timeout_seconds)
            for _source, worker_source in source_pairs:
                worker_dir = str(PurePosixPath(worker_source).parent)
                if worker_dir and worker_dir != ".":
                    self._run_docker(
                        ["docker", "exec", worker_id, "mkdir", "-p", worker_dir],
                        timeout_seconds=self.config.start_timeout_seconds,
                    )
            for source, worker_source in source_pairs:
                self._run_docker(
                    [
                        "docker",
                        "cp",
                        "-L",
                        source,
                        f"{worker_id}:{worker_source}",
                    ],
                    timeout_seconds=self.config.start_timeout_seconds,
                )
        except Exception:
            self._run_docker(["docker", "rm", "-f", worker_id], timeout_seconds=self.config.remove_timeout_seconds, check=False)
            raise
        return WorkerRecord(worker_id=worker_id, slot_id=slot_id, session_key=session_key, fixture_id=fixture_id, image=worker_image)

    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        payload = {
            "function_name": function_name,
            "args": arguments,
            "trace": trace,
            "secrets": secrets_payload,
        }
        command = [
            "docker",
            "exec",
            "-i",
            worker.worker_id,
            "python",
            "-m",
            "agentcicd.sandbox.function_runner",
            "--invoke-jsonl",
        ]
        response = _run_jsonl_worker_process(
            command,
            payload,
            trace=trace,
            timeout_seconds=max(0.1, float(os.getenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", "300"))),
            env=self._docker_env(),
        )
        worker.invocation_count += 1
        worker.last_used_at = time.monotonic()
        return response

    def stop(self, worker: WorkerRecord, *, reason: str) -> None:
        self._run_docker(["docker", "rm", "-f", worker.worker_id], timeout_seconds=self.config.remove_timeout_seconds, check=False)
        worker.healthy = False
        worker.last_used_at = time.monotonic()

    def clear(self, worker: WorkerRecord, *, reason: str) -> None:
        script = r"""
set -eu
for _ in 1 2 3; do
  pkill -TERM -P 1 || true
  sleep 0.1
done
pkill -KILL -P 1 || true
for dir in "${AGENTCICD_SESSION_WORKSPACE_DIR:-}" "${AGENTCICD_WORKSPACE_DIR:-}" "${AGENTCICD_FUNCTION_WORKSPACE_DIR:-}"; do
  if [ -n "$dir" ] && [ -d "$dir" ]; then
    find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  fi
done
find /tmp -maxdepth 1 -type d -name 'agentcicd-session-workspace-*' -exec rm -rf {} +
""".strip()
        self._run_docker(
            ["docker", "exec", worker.worker_id, "sh", "-lc", script],
            timeout_seconds=self.config.remove_timeout_seconds,
        )
        worker.session_key = ""
        worker.last_used_at = time.monotonic()

    def status(self, worker: WorkerRecord) -> dict[str, Any]:
        completed = self._run_docker(
            ["docker", "inspect", "--format", "{{.State.Running}}", worker.worker_id],
            timeout_seconds=15.0,
            check=False,
        )
        healthy = completed.returncode == 0 and completed.stdout.strip().lower() == "true"
        worker.healthy = healthy
        return {"healthy": healthy}

    def _run_docker(
        self,
        command: list[str],
        *,
        timeout_seconds: float,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=self._docker_env(),
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "docker command failed"
            raise RuntimeError(detail)
        return completed

    def _docker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.config.docker_host:
            env["DOCKER_HOST"] = self.config.docker_host
        return env


class EndpointHelperWorkerLifecycle:
    """Worker lifecycle backed by a pod-local sidecar HTTP helper."""

    def __init__(self, config: HelperConfig) -> None:
        if config.mode != "endpoint" or not config.endpoint:
            raise ValueError("Endpoint helper lifecycle requires an endpoint")
        self.config = config

    def create(self, slot_id: str, *, session_key: str = "", fixture_id: str = "", image: str = "") -> WorkerRecord:
        payload = self._post_helper({"action": "create", "slot_id": slot_id, "session_key": session_key, "fixture_id": fixture_id, "image": image})
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            raise RuntimeError("helper create response did not include worker_id")
        return WorkerRecord(worker_id=worker_id, slot_id=slot_id, session_key=session_key, fixture_id=fixture_id, image=image)

    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        payload = {
            "action": "invoke",
            "worker_id": worker.worker_id,
            "slot_id": worker.slot_id,
            "function_name": function_name,
            "args": arguments,
            "trace": trace,
            "secrets": secrets_payload,
        }
        response = _read_jsonl_response_from_url(
            self.config.endpoint,
            payload,
            trace=trace,
            timeout_seconds=max(0.1, float(os.getenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", "300"))),
        )
        worker.invocation_count += 1
        worker.last_used_at = time.monotonic()
        return response

    def stop(self, worker: WorkerRecord, *, reason: str) -> None:
        self._post_helper(
            {
                "action": "stop",
                "worker_id": worker.worker_id,
                "slot_id": worker.slot_id,
                "reason": reason,
            }
        )
        worker.healthy = False
        worker.last_used_at = time.monotonic()

    def clear(self, worker: WorkerRecord, *, reason: str) -> None:
        self._post_helper(
            {
                "action": "clear",
                "worker_id": worker.worker_id,
                "slot_id": worker.slot_id,
                "reason": reason,
            }
        )
        worker.session_key = ""
        worker.last_used_at = time.monotonic()

    def status(self, worker: WorkerRecord) -> dict[str, Any]:
        payload = self._post_helper(
            {
                "action": "status",
                "worker_id": worker.worker_id,
                "slot_id": worker.slot_id,
            }
        )
        healthy = payload.get("healthy")
        if isinstance(healthy, bool):
            worker.healthy = healthy
        return payload

    def _post_helper(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json(self.config.endpoint, payload, timeout_seconds=self.config.timeout_seconds)


class GVisorHelperWorkerLifecycle:
    """Worker lifecycle backed by a privileged local helper process.

    Create/status/stop use one-shot helper RPCs. Invoke uses JSONL frames so trace
    records can be streamed back to the manager before a long call finishes.
    This keeps the manager responsible for routing and lease validation while the helper
    owns the narrow create/exec/stop gVisor operations.
    """

    def __init__(self, helper_command: list[str]) -> None:
        if not helper_command:
            raise ValueError("gVisor helper command is required")
        self.helper_command = helper_command

    def create(self, slot_id: str, *, session_key: str = "", fixture_id: str = "", image: str = "") -> WorkerRecord:
        payload = self._call_helper({"action": "create", "slot_id": slot_id, "session_key": session_key, "fixture_id": fixture_id, "image": image})
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            raise RuntimeError("gVisor helper create response did not include worker_id")
        return WorkerRecord(worker_id=worker_id, slot_id=slot_id, session_key=session_key, fixture_id=fixture_id, image=image)

    def invoke(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        *,
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        payload = self._invoke_helper_jsonl(
            worker=worker,
            function_name=function_name,
            arguments=arguments,
            trace=trace,
            secrets_payload=secrets_payload,
        )
        worker.invocation_count += 1
        worker.last_used_at = time.monotonic()
        return payload

    def stop(self, worker: WorkerRecord, *, reason: str) -> None:
        self._call_helper(
            {
                "action": "stop",
                "worker_id": worker.worker_id,
                "slot_id": worker.slot_id,
                "reason": reason,
            }
        )
        worker.healthy = False
        worker.last_used_at = time.monotonic()

    def clear(self, worker: WorkerRecord, *, reason: str) -> None:
        self._call_helper(
            {
                "action": "clear",
                "worker_id": worker.worker_id,
                "slot_id": worker.slot_id,
                "reason": reason,
            }
        )
        worker.session_key = ""
        worker.last_used_at = time.monotonic()

    def status(self, worker: WorkerRecord) -> dict[str, Any]:
        payload = self._call_helper(
            {
                "action": "status",
                "worker_id": worker.worker_id,
                "slot_id": worker.slot_id,
            }
        )
        healthy = payload.get("healthy")
        if isinstance(healthy, bool):
            worker.healthy = healthy
        return payload

    def _call_helper(self, payload: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            self.helper_command,
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
            timeout=float(os.getenv("AGENTCICD_GVISOR_HELPER_TIMEOUT_SECONDS", "60")),
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "helper failed"
            raise RuntimeError(detail)
        response = json.loads(completed.stdout or "{}")
        if not isinstance(response, dict):
            raise RuntimeError("gVisor helper returned a non-object response")
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response

    def _invoke_helper_jsonl(
        self,
        *,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        trace: Any = None,
        secrets_payload: Any = None,
    ) -> dict[str, Any]:
        timeout_seconds = max(0.1, float(os.getenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", "300")))
        payload = {
            "action": "invoke",
            "worker_id": worker.worker_id,
            "slot_id": worker.slot_id,
            "function_name": function_name,
            "args": arguments,
            "trace": trace,
            "secrets": secrets_payload,
        }
        started_at = time.monotonic()
        process = subprocess.Popen(  # noqa: S603
            self.helper_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":"), default=str))
        process.stdin.close()
        collector = _TraceFrameCollector(trace)
        result_payload: dict[str, Any] | None = None
        error_payload: dict[str, Any] | None = None
        try:
            while True:
                if time.monotonic() - started_at > timeout_seconds:
                    process.kill()
                    summary = collector.write_summary(
                        status="error",
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                        error_code="AGENTCICD_RUNTIME_TIMEOUT",
                        error_message=f"Fixture invocation exceeded {timeout_seconds:g}s",
                        error_type="TimeoutError",
                        http_status=408,
                    )
                    raise InvocationTimeoutError(f"Fixture invocation exceeded {timeout_seconds:g}s", trace_summary=summary)
                ready, _, _ = select.select([process.stdout], [], [], 0.05)
                line = process.stdout.readline() if ready else ""
                if line:
                    frame = _parse_jsonl_frame(line)
                    frame_type = str(frame.get("type") or "")
                    if frame_type == "trace_record":
                        collector.add_record(frame.get("record"))
                    elif frame_type == "result":
                        result_payload = frame
                    elif frame_type == "error":
                        error_payload = frame
                    elif "result" in frame:
                        result_payload = frame
                    elif frame.get("error"):
                        error_payload = {"type": "error", "error": frame.get("error"), "detail": frame.get("detail") or frame.get("error")}
                    continue
                if process.poll() is not None:
                    break
                time.sleep(0.01)
        finally:
            if process.poll() is None:
                process.kill()
            stderr = ""
            try:
                _stdout, stderr = process.communicate(timeout=1)
            except Exception:
                stderr = ""

        duration_ms = int((time.monotonic() - started_at) * 1000)
        if result_payload is not None:
            for record in result_payload.get("trace_records") or []:
                collector.add_record(record)
            summary = collector.write_summary(status="ok", duration_ms=duration_ms)
            response: dict[str, Any] = {"result": result_payload.get("result")}
            if summary:
                response["trace_summary"] = summary
            return response
        if error_payload is not None:
            summary = collector.write_summary(
                status="error",
                duration_ms=duration_ms,
                error_code=str(error_payload.get("error") or "invoke_failed"),
                error_message=str(error_payload.get("detail") or "Worker invocation failed"),
                error_type="RuntimeError",
                http_status=400,
            )
            raise InvocationFailedError(
                str(error_payload.get("detail") or "Worker invocation failed"),
                code=str(error_payload.get("error") or "invoke_failed"),
                trace_summary=summary,
            )
        summary = collector.write_summary(
            status="error",
            duration_ms=duration_ms,
            error_code="invoke_failed",
            error_message=stderr.strip() or "Worker exited without result",
            error_type="RuntimeError",
            http_status=400,
        )
        raise InvocationFailedError(stderr.strip() or "Worker exited without result", trace_summary=summary)


class SandboxManager:
    def __init__(self, config: ManagerConfig, lifecycle: WorkerLifecycle | None = None) -> None:
        self.config = _normalize_manager_config(config)
        self.lifecycle = lifecycle or SubprocessFunctionWorkerLifecycle()
        self._lock = threading.RLock()
        self._service_workers: dict[str, WorkerRecord] = {}
        self._session_workers: dict[str, WorkerRecord] = {}
        self._active_workers: dict[str, WorkerRecord] = {}
        self._warm_workers: dict[str, WorkerRecord] = {}
        self._metrics = ManagerMetrics()
        self._last_worker_create_at = 0.0
        self._next_no_lease_slot = 0
        self._prepare_warm_workers()

    def register_capacity(self, *, driver_base_url: str) -> None:
        if not driver_base_url:
            return
        fixture_ids = list(self.config.fixture_ids or (self.config.fixture_id,))
        payload = {
            "pool_name": self.config.pool_name,
            "pool_kind": self.config.pool_kind,
            "node_id": self.config.manager_id,
            "address": self.config.address,
            "capacity": self.config.max_workers,
            "metadata": {
                "fixture_ids": fixture_ids,
                "function_names": list(self.config.function_names or (self.config.function_name,)),
                "fixture_worker_images": dict(self.config.fixture_worker_images),
                "function_name": self.config.function_name,
                "generation": self.config.generation,
                "runtime_provider": _runtime_provider_from_env(),
                "worker_substrate": _worker_substrate_from_env(),
                "runtime_protocol_version": "agentcicd.sandbox_runtime.v1",
                "fixture_manifest_schema_version": os.getenv(
                    "AGENTCICD_FIXTURE_MANIFEST_SCHEMA_VERSION",
                    "agentcicd.fixture_manifest.v1",
                ).strip(),
                "sandbox_runtime_image_version": os.getenv(
                    "AGENTCICD_SANDBOX_RUNTIME_IMAGE_VERSION",
                    "",
                ).strip(),
                "agentcicd_fixtures_version": os.getenv(
                    "AGENTCICD_FIXTURES_VERSION",
                    "",
                ).strip(),
                "warm_workers": self._warm_worker_count(),
            },
            "heartbeat_ttl_seconds": self.config.heartbeat_ttl_seconds,
        }
        _post_json(f"{driver_base_url.rstrip('/')}/pools/nodes/register", payload, timeout_seconds=10.0)

    def report_capacity_update(self) -> None:
        if not self.config.driver_base_url:
            return
        try:
            self.register_capacity(driver_base_url=self.config.driver_base_url)
        except Exception:
            return

    def start_capacity_heartbeat(self, *, driver_base_url: str, stop: threading.Event | None = None) -> threading.Thread:
        stop_event = stop or threading.Event()
        interval = max(1.0, min(15.0, self.config.heartbeat_ttl_seconds / 3.0))

        def _loop() -> None:
            while not stop_event.wait(interval):
                try:
                    _post_json(
                        f"{driver_base_url.rstrip('/')}/pools/nodes/{self.config.manager_id}/heartbeat",
                        {"heartbeat_ttl_seconds": self.config.heartbeat_ttl_seconds},
                        timeout_seconds=5.0,
                    )
                except Exception:
                    continue

        thread = threading.Thread(target=_loop, name=f"agentcicd-sandbox-manager-heartbeat-{self.config.manager_id}", daemon=True)
        thread.start()
        return thread

    def invoke(self, function_name: str, payload: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        allowed = set(self.config.function_names or (self.config.function_name,))
        if function_name not in allowed:
            return HTTPStatus.NOT_FOUND, {"error": "unknown_function", "name": function_name}
        arguments = payload.get("args")
        if not isinstance(arguments, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": "args must be an object"}
        parsed_lease = self._parse_lease(payload.get("lease"))
        lease_error = self._validate_lease(parsed_lease)
        if lease_error:
            return HTTPStatus.CONFLICT, {"error": "invalid_lease", "detail": lease_error}

        worker: WorkerRecord | None = None
        disposable = self.config.pool_kind == "sandbox"
        clear_after_invocation = self.config.pool_kind == "session"
        started_at = time.monotonic()
        try:
            worker = self._acquire_worker(parsed_lease, arguments)
            response = self._invoke_with_timeout(worker, function_name, arguments, payload)
            self._metrics.invocations += 1
            self._metrics.total_invocation_ms += int((time.monotonic() - started_at) * 1000)
            if self.config.debug:
                response["debug"] = self._debug_payload(worker, disposable=disposable, started_at=started_at)
            return HTTPStatus.OK, response
        except TimeoutError as exc:
            self._metrics.failures += 1
            self._metrics.timeouts += 1
            if worker is not None:
                worker.healthy = False
            response: dict[str, Any] = {"error": "invoke_timeout", "detail": str(exc)}
            trace_summary = exc.trace_summary if isinstance(exc, InvocationTimeoutError) else None
            if isinstance(trace_summary, dict) and trace_summary:
                response["trace_summary"] = trace_summary
            return HTTPStatus.REQUEST_TIMEOUT, response
        except InvocationFailedError as exc:
            self._metrics.failures += 1
            self._metrics.worker_failure_count += 1
            if worker is not None:
                worker.healthy = False
            response = {"error": exc.code, "detail": str(exc)}
            if exc.trace_summary:
                response["trace_summary"] = exc.trace_summary
            return HTTPStatus.BAD_REQUEST, response
        except Exception as exc:
            self._metrics.failures += 1
            self._metrics.worker_failure_count += 1
            if worker is not None:
                worker.healthy = False
            return HTTPStatus.BAD_REQUEST, {"error": "invoke_failed", "detail": str(exc)}
        finally:
            if worker is not None and disposable:
                self._stop_worker(worker, reason="sandbox_invocation_complete")
            elif worker is not None and clear_after_invocation:
                self._clear_worker(worker, reason="session_invocation_complete")

    def status(self) -> dict[str, Any]:
        self._expire_idle_sessions()
        with self._lock:
            service_workers = list(self._service_workers.values())
            session_workers = list(self._session_workers.values())
            active_workers = list(self._active_workers.values())
            warm_workers = list(self._warm_workers.values())
        workers = service_workers + session_workers + active_workers + warm_workers
        self._refresh_worker_health(workers)
        return {
            "fixture_id": self.config.fixture_id,
            "pool_name": self.config.pool_name,
            "pool_kind": self.config.pool_kind,
            "manager_id": self.config.manager_id,
            "generation": self.config.generation,
            "max_workers": self.config.max_workers,
            "warm_workers": len(warm_workers),
            "active_workers": len(active_workers),
            "metrics": self._metrics.to_dict(),
            "workers": [
                {
                    "worker_id": worker.worker_id,
                    "slot_id": worker.slot_id,
                    "session_key": worker.session_key,
                    "invocation_count": worker.invocation_count,
                    "healthy": worker.healthy,
                    "warm": worker.warm,
                }
                for worker in workers
            ],
            "invocations": self._metrics.invocations,
            "failures": self._metrics.failures,
            "cleanup_failures": self._metrics.cleanup_failures,
        }

    def _parse_lease(self, value: Any) -> InvocationLease | None:
        if value is None and not self.config.require_lease:
            return None
        if not isinstance(value, dict):
            return None
        return InvocationLease(
            lease_id=str(value.get("lease_id") or ""),
            pool_name=str(value.get("pool_name") or ""),
            pool_kind=str(value.get("pool_kind") or ""),
            manager_id=str(value.get("manager_id") or value.get("node_id") or ""),
            worker_slot_id=str(value.get("worker_slot_id") or ""),
            fixture_id=str(value.get("fixture_id") or ""),
            generation=int(value.get("generation") or 0),
            request_id=str(value.get("request_id") or ""),
        )

    def _validate_lease(self, lease: InvocationLease | None) -> str:
        if lease is None:
            return "lease is required" if self.config.require_lease else ""
        if not lease.lease_id:
            return "lease_id is required"
        if lease.manager_id != self.config.manager_id:
            return "lease targets a different manager"
        if lease.pool_name != self.config.pool_name:
            return "lease targets a different pool"
        if lease.pool_kind != self.config.pool_kind:
            return "lease targets a different pool kind"
        allowed_fixture_ids = set(self.config.fixture_ids or (self.config.fixture_id,))
        if lease.fixture_id and lease.fixture_id not in allowed_fixture_ids:
            return "lease targets a different fixture"
        if lease.generation != self.config.generation:
            return f"lease generation is stale: lease={lease.generation}, manager={self.config.generation}"
        if not lease.worker_slot_id:
            return "worker_slot_id is required"
        if not self._slot_belongs_to_manager(lease.worker_slot_id):
            return "lease targets a different worker slot"
        if self.config.driver_base_url:
            try:
                response = _post_json(
                    f"{self.config.driver_base_url.rstrip('/')}/pools/leases/validate",
                    {
                        "lease_id": lease.lease_id,
                        "pool_name": lease.pool_name,
                        "pool_kind": lease.pool_kind,
                        "fixture_id": lease.fixture_id,
                        "manager_id": lease.manager_id,
                        "worker_slot_id": lease.worker_slot_id,
                        "generation": lease.generation,
                    },
                    timeout_seconds=5.0,
                )
            except Exception as exc:
                return f"lease validation failed: {exc}"
            if not bool(response.get("valid")):
                return str(response.get("reason") or "lease is unknown or expired")
        return ""

    def _acquire_worker(self, lease: InvocationLease | None, arguments: dict[str, Any]) -> WorkerRecord:
        slot_id = lease.worker_slot_id if lease is not None else self._next_local_slot_id()
        fixture_id = lease.fixture_id if lease is not None else ""
        image = self._worker_image_for_fixture(fixture_id)
        if self.config.pool_kind == "service":
            return self._service_worker(slot_id, fixture_id=fixture_id, image=image)
        if self.config.pool_kind == "session":
            session_key = self._session_key(lease, arguments)
            return self._session_worker(slot_id, session_key, fixture_id=fixture_id, image=image)
        return self._sandbox_worker(slot_id, fixture_id=fixture_id, image=image)

    def _next_local_slot_id(self) -> str:
        with self._lock:
            slot_number = (self._next_no_lease_slot % max(1, self.config.max_workers)) + 1
            self._next_no_lease_slot += 1
        return f"{self.config.manager_id}.slot-{slot_number}"

    def _service_worker(self, slot_id: str, *, fixture_id: str = "", image: str = "") -> WorkerRecord:
        with self._lock:
            worker = self._service_workers.get(slot_id)
            replaced = False
            if worker is not None and worker.healthy and not self._worker_matches(worker, fixture_id=fixture_id, image=image):
                self._service_workers.pop(slot_id, None)
                self._stop_worker(worker, reason="service_fixture_image_replaced")
                worker = None
                replaced = True
            if worker is None or not worker.healthy:
                worker = self._create_worker(slot_id, fixture_id=fixture_id, image=image)
                self._service_workers[slot_id] = worker
                worker.acquire_decision = "replace_idle_incompatible" if replaced else "start_compatible"
            else:
                worker.acquire_decision = "reuse_compatible"
            return worker

    def _session_worker(self, slot_id: str, session_key: str, *, fixture_id: str = "", image: str = "") -> WorkerRecord:
        self._expire_idle_sessions()
        with self._lock:
            worker = self._session_workers.get(slot_id)
            replaced = False
            if worker is not None and worker.healthy and not self._worker_matches(worker, fixture_id=fixture_id, image=image):
                self._clear_worker(worker, reason="session_fixture_image_replaced")
                self._session_workers.pop(slot_id, None)
                self._stop_worker(worker, reason="session_fixture_image_replaced")
                worker = None
                replaced = True
            if worker is None or not worker.healthy:
                worker = self._create_worker(slot_id, session_key=session_key, fixture_id=fixture_id, image=image)
                self._session_workers[slot_id] = worker
                worker.acquire_decision = "replace_idle_incompatible" if replaced else "start_compatible"
            else:
                worker.session_key = session_key
                worker.acquire_decision = "reuse_compatible"
            return worker

    def _sandbox_worker(self, slot_id: str, *, fixture_id: str = "", image: str = "") -> WorkerRecord:
        replaced = False
        with self._lock:
            worker = self._warm_workers.pop(slot_id, None)
            if worker is not None and worker.healthy:
                if not self._worker_matches(worker, fixture_id=fixture_id, image=image):
                    self._stop_worker(worker, reason="sandbox_warm_fixture_image_replaced", replace_warm=False)
                    worker = None
                    replaced = True
            if worker is not None and worker.healthy:
                worker.warm = False
                worker.acquire_decision = "reuse_compatible"
                self._active_workers[worker.worker_id] = worker
                return worker
        worker = self._create_worker(slot_id, fixture_id=fixture_id, image=image)
        worker.acquire_decision = "replace_idle_incompatible" if replaced else "start_compatible"
        with self._lock:
            self._active_workers[worker.worker_id] = worker
        return worker

    def _worker_matches(self, worker: WorkerRecord, *, fixture_id: str = "", image: str = "") -> bool:
        if fixture_id and worker.fixture_id != fixture_id:
            if not image or not worker.image:
                return False
        if image and worker.image != image:
            return False
        return True

    def _worker_image_for_fixture(self, fixture_id: str) -> str:
        return str(self.config.fixture_worker_images.get(str(fixture_id or "").strip()) or "").strip()

    def _stop_worker(self, worker: WorkerRecord, *, reason: str, replace_warm: bool = True) -> None:
        started_at = time.monotonic()
        try:
            self.lifecycle.stop(worker, reason=reason)
        except Exception:
            self._metrics.cleanup_failures += 1
        finally:
            self._metrics.total_cleanup_ms += int((time.monotonic() - started_at) * 1000)
            with self._lock:
                self._active_workers.pop(worker.worker_id, None)
            if replace_warm and self.config.pool_kind == "sandbox" and self._slot_should_stay_warm(worker.slot_id):
                self._replace_warm_worker(worker.slot_id)

    def _clear_worker(self, worker: WorkerRecord, *, reason: str) -> None:
        started_at = time.monotonic()
        try:
            self.lifecycle.clear(worker, reason=reason)
        except Exception:
            self._metrics.cleanup_failures += 1
            with self._lock:
                self._session_workers.pop(worker.slot_id, None)
            self._stop_worker(worker, reason=f"{reason}_clear_failed")
        finally:
            self._metrics.total_cleanup_ms += int((time.monotonic() - started_at) * 1000)

    def _session_key(self, lease: InvocationLease | None, arguments: dict[str, Any]) -> str:
        for key in ("session_key", "session_id", "conversation_id"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if lease is not None and lease.request_id:
            return lease.request_id
        return ""

    def _expire_idle_sessions(self) -> None:
        if self.config.pool_kind != "session":
            return
        ttl_seconds = max(0.001, float(self.config.session_idle_ttl_seconds or 900.0))
        now = time.monotonic()
        expired: list[WorkerRecord] = []
        with self._lock:
            for slot_id, worker in list(self._session_workers.items()):
                if now - worker.last_used_at <= ttl_seconds:
                    continue
                expired.append(worker)
                self._session_workers.pop(slot_id, None)
        for worker in expired:
            self._stop_worker(worker, reason="session_idle_expired")

    def _refresh_worker_health(self, workers: list[WorkerRecord]) -> None:
        for worker in workers:
            try:
                status = self.lifecycle.status(worker)
            except Exception:
                worker.healthy = False
                self._metrics.worker_failure_count += 1
                continue
            healthy = status.get("healthy")
            if isinstance(healthy, bool):
                worker.healthy = healthy

    def _invoke_with_timeout(
        self,
        worker: WorkerRecord,
        function_name: str,
        arguments: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        timeout_seconds = max(0.1, float(self.config.call_timeout_seconds or 300.0))
        old_timeout_env = os.environ.get("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS")
        os.environ["AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS"] = str(timeout_seconds)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self.lifecycle.invoke,
            worker,
            function_name,
            arguments,
            trace=payload.get("trace"),
            secrets_payload=payload.get("secrets"),
        )
        wait_timeout_seconds = (
            timeout_seconds + 5.0
            if isinstance(self.lifecycle, (SubprocessFunctionWorkerLifecycle, GVisorHelperWorkerLifecycle))
            else timeout_seconds
        )
        try:
            return future.result(timeout=wait_timeout_seconds)
        except InvocationTimeoutError:
            raise
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            collector = _TraceFrameCollector(payload.get("trace"))
            summary = collector.write_summary(
                status="error",
                duration_ms=int(timeout_seconds * 1000),
                error_code="AGENTCICD_RUNTIME_TIMEOUT",
                error_message=f"Fixture invocation exceeded {timeout_seconds:g}s",
                error_type="TimeoutError",
                http_status=408,
            )
            raise InvocationTimeoutError(f"Fixture invocation exceeded {timeout_seconds:g}s", trace_summary=summary) from exc
        finally:
            if future.done():
                executor.shutdown(wait=True)
            if old_timeout_env is None:
                os.environ.pop("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", None)
            else:
                os.environ["AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS"] = old_timeout_env

    def _create_worker(self, slot_id: str, *, session_key: str = "", warm: bool = False, fixture_id: str = "", image: str = "") -> WorkerRecord:
        self._throttle_worker_create()
        started_at = time.monotonic()
        worker = self.lifecycle.create(slot_id, session_key=session_key, fixture_id=fixture_id, image=image)
        worker.warm = warm
        self._metrics.worker_create_count += 1
        self._metrics.total_worker_startup_ms += int((time.monotonic() - started_at) * 1000)
        return worker

    def _throttle_worker_create(self) -> None:
        rate = float(self.config.worker_create_rate_limit_per_second or 0.0)
        if rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, (1.0 / rate) - (now - self._last_worker_create_at))
            if wait_seconds:
                time.sleep(wait_seconds)
                now = time.monotonic()
            self._last_worker_create_at = now

    def _prepare_warm_workers(self) -> None:
        if self.config.pool_kind != "sandbox" or self.config.min_warm <= 0:
            return
        for index in range(min(self.config.min_warm, self.config.max_workers)):
            slot_id = f"{self.config.manager_id}.slot-{index + 1}"
            self._replace_warm_worker(slot_id)

    def _replace_warm_worker(self, slot_id: str) -> None:
        with self._lock:
            if slot_id in self._warm_workers:
                return
        try:
            fixture_id, image = self._default_warm_worker_target()
            worker = self._create_worker(slot_id, warm=True, fixture_id=fixture_id, image=image)
        except Exception:
            self._metrics.worker_failure_count += 1
            return
        with self._lock:
            self._warm_workers[slot_id] = worker
        self.report_capacity_update()

    def _default_warm_worker_target(self) -> tuple[str, str]:
        fixture_ids = tuple(item for item in self.config.fixture_ids if item)
        if len(fixture_ids) != 1:
            return "", ""
        fixture_id = fixture_ids[0]
        return fixture_id, self._worker_image_for_fixture(fixture_id)

    def _warm_worker_count(self) -> int:
        with self._lock:
            return sum(1 for worker in self._warm_workers.values() if worker.healthy)

    def _slot_should_stay_warm(self, slot_id: str) -> bool:
        if self.config.min_warm <= 0:
            return False
        warm_slots = {f"{self.config.manager_id}.slot-{index + 1}" for index in range(min(self.config.min_warm, self.config.max_workers))}
        return slot_id in warm_slots

    def _slot_belongs_to_manager(self, slot_id: str) -> bool:
        prefix = f"{self.config.manager_id}.slot-"
        if not slot_id.startswith(prefix):
            return False
        suffix = slot_id[len(prefix):]
        if not suffix.isdigit():
            return False
        return 1 <= int(suffix) <= self.config.max_workers

    def _debug_payload(self, worker: WorkerRecord, *, disposable: bool, started_at: float) -> dict[str, Any]:
        return {
            "pool_kind": self.config.pool_kind,
            "worker_id": worker.worker_id,
            "worker_slot_id": worker.slot_id,
            "reused": worker.invocation_count > 1,
            "disposable": disposable,
            "lease_decision": worker.acquire_decision,
            "fixture_id": worker.fixture_id,
            "worker_image": worker.image,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "cleanup_result": "pending" if disposable else "reused",
        }


class _TraceFrameCollector:
    def __init__(self, context: Any) -> None:
        self.context = dict(context) if isinstance(context, dict) else {}
        self.trace_id = str(self.context.get("trace_id") or "")
        self.root_span_id = str(self.context.get("parent_span_id") or "") or secrets.token_hex(8)
        self.root_call_id = str(self.context.get("parent_call_id") or "") or f"rtcall_{secrets.token_hex(12)}"
        self.started_at = str(self.context.get("started_at") or _utc_now())
        self.records: list[dict[str, Any]] = []
        self.record_index: dict[str, int] = {}
        if self.trace_id:
            self.add_record(
                {
                    "record_type": "span",
                    "trace_id": self.trace_id,
                    "span_id": self.root_span_id,
                    "call_id": self.root_call_id,
                    "name": "agentcicd.fixture.call",
                    "kind": "call",
                    "status": "running",
                    "started_at": self.started_at,
                    "attributes": {
                        "function_name": self.context.get("function_name"),
                        "runtime_alias": self.context.get("runtime_alias"),
                        "backend": self.context.get("backend"),
                        "execution_runtime": self.context.get("execution_runtime"),
                        "cache_hit": self.context.get("cache_hit"),
                        "limiter_key": self.context.get("limiter_key"),
                        "max_in_flight": self.context.get("max_in_flight"),
                        "pool_name": self.context.get("pool_name"),
                        "pool_kind": self.context.get("pool_kind"),
                        "fixture_id": self.context.get("fixture_id"),
                        "image_id": self.context.get("image_id"),
                    },
                }
            )

    def add_record(self, record: Any) -> None:
        if not isinstance(record, dict):
            return
        if self.trace_id and str(record.get("trace_id") or "") != self.trace_id:
            return
        cleaned = _drop_none(dict(record))
        span_id = str(cleaned.get("span_id") or "")
        if span_id and span_id in self.record_index:
            self.records[self.record_index[span_id]] = cleaned
            return
        if span_id:
            self.record_index[span_id] = len(self.records)
        self.records.append(cleaned)

    def write_summary(
        self,
        *,
        status: str,
        duration_ms: int,
        error_code: str | None = None,
        error_message: str | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
    ) -> dict[str, Any] | None:
        if not self.trace_id:
            return None
        finished_at = _utc_now()
        root = self._root_record()
        root.update(
            _drop_none(
                {
                    "status": status,
                    "duration_ms": duration_ms,
                    "finished_at": finished_at,
                    "error_code": error_code,
                    "error_message": error_message,
                    "error_type": error_type,
                    "http_status": http_status,
                }
            )
        )
        self.add_record(root)
        error_records = [record for record in self.records if str(record.get("status") or "") == "error"]
        top_error = error_message or (str(error_records[0].get("error_message") or "") if error_records else None)
        trace_dir = f"debug/fixture_traces/{self.trace_id}"
        summary_path = f"{trace_dir}/summary.json"
        spans_path = f"{trace_dir}/spans.jsonl"
        summary = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "root_call_id": self.root_call_id,
            "function_name": self.context.get("function_name"),
            "runtime_alias": self.context.get("runtime_alias"),
            "backend": self.context.get("backend"),
            "execution_runtime": self.context.get("execution_runtime"),
            "status": status,
            "duration_ms": duration_ms,
            "span_count": sum(1 for record in self.records if record.get("record_type") == "span"),
            "error_count": len(error_records) + (1 if status == "error" and not error_records else 0),
            "top_error": top_error,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "spans_path": spans_path,
        }
        self._write_artifacts(summary_path, spans_path, summary)
        return _drop_none(
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "call_id": self.root_call_id,
                "parent_call_id": self.context.get("parent_call_id"),
                "trace_id": self.trace_id,
                "span_id": self.root_span_id,
                "parent_span_id": self.context.get("parent_span_id"),
                "function_name": self.context.get("function_name"),
                "runtime_alias": self.context.get("runtime_alias"),
                "backend": self.context.get("backend"),
                "fixture_id": self.context.get("fixture_id"),
                "image_id": self.context.get("image_id"),
                "execution_runtime": self.context.get("execution_runtime"),
                "status": status,
                "duration_ms": duration_ms,
                "cache_hit": self.context.get("cache_hit"),
                "limiter_key": self.context.get("limiter_key"),
                "max_in_flight": self.context.get("max_in_flight"),
                "pool_name": self.context.get("pool_name"),
                "pool_kind": self.context.get("pool_kind"),
                "http_status": http_status,
                "error_code": error_code,
                "error_message": error_message,
                "error_type": error_type,
                "summary": "Fixture failed" if status == "error" else "Fixture completed",
                "top_error": top_error,
                "span_count": summary["span_count"],
                "error_count": summary["error_count"],
                "trace_summary_path": summary_path,
                "trace_spans_path": spans_path,
            }
        )

    def _root_record(self) -> dict[str, Any]:
        existing = self.record_index.get(self.root_span_id)
        if existing is not None:
            return dict(self.records[existing])
        return {
            "record_type": "span",
            "trace_id": self.trace_id,
            "span_id": self.root_span_id,
            "call_id": self.root_call_id,
            "name": "agentcicd.fixture.call",
            "kind": "call",
            "started_at": self.started_at,
        }

    def _write_artifacts(self, summary_path: str, spans_path: str, summary: dict[str, Any]) -> None:
        run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
        if not run_object_uri or object_store_from_env is None:
            return
        records = sorted(self.records, key=_record_sort_key)
        payload = "\n".join(json.dumps(_drop_none(record), sort_keys=True, separators=(",", ":"), default=str) for record in records)
        if payload:
            payload += "\n"
        try:
            store = object_store_from_env()
            store.put_json(f"{run_object_uri.rstrip('/')}/{summary_path}", _drop_none(summary))
            store.put_text(
                f"{run_object_uri.rstrip('/')}/{spans_path}",
                payload,
                content_type="application/x-ndjson",
            )
        except Exception:
            return


def _parse_jsonl_frame(line: str) -> dict[str, Any]:
    try:
        frame = json.loads(line)
    except json.JSONDecodeError:
        return {"type": "log", "message": line.rstrip("\n")}
    return frame if isinstance(frame, dict) else {"type": "log", "message": line.rstrip("\n")}


def _record_sort_key(record: dict[str, Any]) -> tuple[int, int, str, str]:
    depth = 0 if record.get("name") == "agentcicd.fixture.call" else 1
    record_kind = 0 if record.get("record_type") == "span" else 1
    return (
        depth,
        record_kind,
        str(record.get("started_at") or record.get("timestamp") or ""),
        str(record.get("span_id") or record.get("call_id") or ""),
    )


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sys_executable() -> str:
    return sys.executable or "python"


def manager_config_from_env() -> ManagerConfig:
    pool_kind = os.getenv("AGENTCICD_FUNCTION_POOL_KIND", "service").strip().lower() or "service"
    fixture_id = os.getenv("AGENTCICD_FUNCTION_ID", "").strip()
    fixture_ids = _json_string_tuple(os.getenv("AGENTCICD_FUNCTION_GROUP_FIXTURE_IDS", ""))
    fixture_worker_images = _json_string_map(os.getenv("AGENTCICD_FUNCTION_GROUP_WORKER_IMAGES", ""))
    function_name = (
        os.getenv("AGENTCICD_FUNCTION_BUILTIN_ENTRYPOINT", "").strip()
        or os.getenv("AGENTCICD_FUNCTION_ENTRYPOINT_NAME", "").strip()
        or os.getenv("AGENTCICD_FUNCTION_CALL_NAME", "").strip().split(".")[-1]
        or os.getenv("AGENTCICD_FUNCTION_RUNTIME_ALIAS", "").strip()
    )
    function_names = _json_string_tuple(os.getenv("AGENTCICD_FUNCTION_GROUP_FUNCTION_NAMES", ""))
    manager_id = (
        os.getenv("AGENTCICD_SANDBOX_MANAGER_ID", "").strip()
        or os.getenv("AGENTCICD_POD_NAME", "").strip()
        or f"manager.{secrets.token_hex(6)}"
    )
    max_workers = int(os.getenv("AGENTCICD_SANDBOX_MANAGER_MAX_WORKERS", "1"))
    generation = int(os.getenv("AGENTCICD_SANDBOX_MANAGER_GENERATION", "1"))
    port = int(os.getenv("AGENTCICD_FUNCTION_RUNTIME_PORT", "8080"))
    address = os.getenv("AGENTCICD_SANDBOX_MANAGER_ADDRESS", "").strip() or f"http://{manager_id}:{port}"
    require_lease = os.getenv("AGENTCICD_SANDBOX_MANAGER_REQUIRE_LEASE", "true").strip().lower() not in {"0", "false", "no"}
    return ManagerConfig(
        fixture_id=fixture_id,
        function_name=function_name,
        pool_name=os.getenv("AGENTCICD_FUNCTION_POOL_NAME", "").strip(),
        pool_kind=pool_kind,
        manager_id=manager_id,
        generation=generation,
        address=address,
        max_workers=max(1, max_workers),
        require_lease=require_lease,
        debug=os.getenv("AGENTCICD_FIXTURE_DEBUG", "").strip().lower() in {"1", "true", "yes"},
        fixture_ids=fixture_ids or ((fixture_id,) if fixture_id else ()),
        function_names=function_names or ((function_name,) if function_name else ()),
        fixture_worker_images=fixture_worker_images,
        driver_base_url=os.getenv("AGENTCICD_RATE_LIMITER_BASE_URL", "").strip(),
        heartbeat_ttl_seconds=float(os.getenv("AGENTCICD_SANDBOX_MANAGER_HEARTBEAT_TTL_SECONDS", "60")),
        min_warm=max(0, int(os.getenv("AGENTCICD_SANDBOX_MANAGER_MIN_WARM", "0"))),
        call_timeout_seconds=float(os.getenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS", "300")),
        session_idle_ttl_seconds=float(os.getenv("AGENTCICD_SANDBOX_SESSION_IDLE_TTL_SECONDS", "900")),
        worker_create_rate_limit_per_second=float(os.getenv("AGENTCICD_SANDBOX_WORKER_CREATE_RATE_LIMIT_PER_SECOND", "0")),
    )


def _json_string_tuple(raw: str) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except Exception:
        return tuple(item.strip() for item in text.split(",") if item.strip())
    if isinstance(parsed, list):
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    return ()


def _json_string_map(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in parsed.items()
        if str(key).strip() and str(value).strip()
    }


def _normalize_manager_config(config: ManagerConfig) -> ManagerConfig:
    fixture_ids = tuple(str(item).strip() for item in config.fixture_ids if str(item).strip())
    if not fixture_ids and str(config.fixture_id or "").strip():
        fixture_ids = (str(config.fixture_id).strip(),)
    function_names = tuple(str(item).strip() for item in config.function_names if str(item).strip())
    if not function_names and str(config.function_name or "").strip():
        function_names = (str(config.function_name).strip(),)
    fixture_worker_images = {
        str(key).strip(): str(value).strip()
        for key, value in config.fixture_worker_images.items()
        if str(key).strip() and str(value).strip()
    }
    if (
        fixture_ids == config.fixture_ids
        and function_names == config.function_names
        and fixture_worker_images == config.fixture_worker_images
    ):
        return config
    return replace(
        config,
        fixture_ids=fixture_ids,
        function_names=function_names,
        fixture_worker_images=fixture_worker_images,
    )


def worker_lifecycle_from_env() -> WorkerLifecycle:
    substrate = os.getenv("AGENTCICD_SANDBOX_WORKER_SUBSTRATE", "local").strip().lower()
    if substrate == "docker":
        image = os.getenv("AGENTCICD_SANDBOX_WORKER_IMAGE", "").strip()
        if not image:
            raise ValueError("AGENTCICD_SANDBOX_WORKER_IMAGE is required when AGENTCICD_SANDBOX_WORKER_SUBSTRATE=docker")
        return DockerWorkerLifecycle(
            DockerWorkerConfig(
                image=image,
                docker_host=os.getenv("AGENTCICD_SANDBOX_DOCKER_HOST", "").strip(),
                network=os.getenv("AGENTCICD_SANDBOX_WORKER_NETWORK", "").strip(),
                cpu=os.getenv("AGENTCICD_SANDBOX_WORKER_CPU", "").strip(),
                memory=os.getenv("AGENTCICD_SANDBOX_WORKER_MEMORY", "").strip(),
                tmpfs_size=os.getenv("AGENTCICD_SANDBOX_WORKER_TMPFS_SIZE", "").strip(),
                source_path=os.getenv("AGENTCICD_FUNCTION_SOURCE_PATH", "").strip(),
                worker_source_path=os.getenv("AGENTCICD_SANDBOX_WORKER_SOURCE_PATH", "").strip()
                or os.getenv("AGENTCICD_FUNCTION_SOURCE_PATH", "").strip(),
                source_paths=_json_string_tuple(os.getenv("AGENTCICD_FUNCTION_SOURCE_PATHS", "")),
                worker_source_paths=_json_string_tuple(os.getenv("AGENTCICD_SANDBOX_WORKER_SOURCE_PATHS", "")),
                create_timeout_seconds=float(
                    os.getenv("AGENTCICD_SANDBOX_WORKER_CREATE_TIMEOUT_SECONDS")
                    or os.getenv("AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS")
                    or "300"
                ),
                start_timeout_seconds=float(os.getenv("AGENTCICD_SANDBOX_WORKER_START_TIMEOUT_SECONDS", "60")),
                remove_timeout_seconds=float(os.getenv("AGENTCICD_SANDBOX_WORKER_REMOVE_TIMEOUT_SECONDS", "30")),
                extra_env=_worker_env_from_current_process(),
            )
        )
    if substrate == "gvisor":
        endpoint = os.getenv("AGENTCICD_GVISOR_HELPER_ENDPOINT", "").strip()
        if endpoint:
            return EndpointHelperWorkerLifecycle(
                HelperConfig(
                    mode="endpoint",
                    endpoint=endpoint,
                    timeout_seconds=float(os.getenv("AGENTCICD_GVISOR_HELPER_TIMEOUT_SECONDS", "60")),
                )
            )
        helper = os.getenv("AGENTCICD_GVISOR_HELPER_COMMAND", "").strip()
        if not helper:
            raise ValueError(
                "AGENTCICD_GVISOR_HELPER_ENDPOINT or AGENTCICD_GVISOR_HELPER_COMMAND is required "
                "when AGENTCICD_SANDBOX_WORKER_SUBSTRATE=gvisor"
            )
        return GVisorHelperWorkerLifecycle(shlex.split(helper))
    return SubprocessFunctionWorkerLifecycle()


def _runtime_provider_from_env() -> str:
    provider = os.getenv("AGENTCICD_FIXTURE_RUNTIME_PROVIDER", "").strip()
    if provider == "sandbox_manager_local_process":
        return "sandbox_manager_dev_subprocess"
    if provider:
        return provider
    substrate = _worker_substrate_from_env()
    if substrate == "docker":
        return "sandbox_manager_docker"
    if substrate == "gvisor":
        return "sandbox_manager_gvisor"
    return "sandbox_manager_dev_subprocess"


def _worker_substrate_from_env() -> str:
    substrate = os.getenv("AGENTCICD_SANDBOX_WORKER_SUBSTRATE", "").strip().lower()
    return substrate or "local"


def create_server(manager: SandboxManager, *, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    handler_cls = _handler_for(manager)
    return ThreadingHTTPServer((host, port), handler_cls)


def serve(manager: SandboxManager, *, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    httpd = create_server(manager, host=host, port=port)
    httpd.serve_forever()
    return httpd


def _handler_for(manager: SandboxManager) -> type[BaseHTTPRequestHandler]:
    class SandboxManagerRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentCICDSandboxManager/0.1"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.rstrip("/")
            if path in {"", "/health"}:
                self._write_json({"status": "ok", "manager_id": manager.config.manager_id})
                return
            if path == "/pool/status":
                self._write_json(manager.status())
                return
            if path == "/manifest":
                from agentcicd.sandbox import function_runner

                self._write_json(function_runner.build_manifest())
                return
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/invoke/"):
                self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            function_name = self.path.rsplit("/", 1)[-1]
            payload = self._read_json()
            status, response_payload = manager.invoke(function_name, payload)
            self._write_json(response_payload, status)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            return payload if isinstance(payload, dict) else {}

        def _write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return SandboxManagerRequestHandler


def _post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw_payload = response.read().decode("utf-8") or "{}"
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Sandbox manager control request failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError("Driver runtime-control server is unavailable") from exc
    response_payload = json.loads(raw_payload)
    return response_payload if isinstance(response_payload, dict) else {}


def _read_http_error_payload(exc: HTTPError) -> dict[str, Any]:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {"error": "invoke_failed", "detail": body}
    return parsed if isinstance(parsed, dict) else {"error": "invoke_failed", "detail": body}


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_server(url: str, process: subprocess.Popen[str], *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = ""
            try:
                _stdout, stderr = process.communicate(timeout=1)
            except Exception:
                pass
            raise RuntimeError(stderr.strip() or f"Worker exited with returncode={process.returncode}")
        try:
            with urlopen(url, timeout=0.25) as response:  # noqa: S310
                if 200 <= int(response.status) < 500:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for worker HTTP server at {url}: {last_error}")


def _run_jsonl_worker_process(
    command: list[str],
    payload: dict[str, Any],
    *,
    trace: Any,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    process = subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env or os.environ.copy(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":"), default=str))
    process.stdin.close()
    collector = _TraceFrameCollector(trace)
    result_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    try:
        while True:
            if time.monotonic() - started_at > timeout_seconds:
                process.kill()
                summary = collector.write_summary(
                    status="error",
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    error_code="AGENTCICD_RUNTIME_TIMEOUT",
                    error_message=f"Fixture invocation exceeded {timeout_seconds:g}s",
                    error_type="TimeoutError",
                    http_status=408,
                )
                raise InvocationTimeoutError(f"Fixture invocation exceeded {timeout_seconds:g}s", trace_summary=summary)
            ready, _, _ = select.select([process.stdout], [], [], 0.05)
            line = process.stdout.readline() if ready else ""
            if line:
                frame = _parse_jsonl_frame(line)
                result_payload, error_payload = _record_jsonl_frame(
                    frame,
                    collector,
                    result_payload=result_payload,
                    error_payload=error_payload,
                )
                continue
            if process.poll() is not None:
                break
            time.sleep(0.01)
    finally:
        if process.poll() is None:
            process.kill()
        stderr = ""
        stdout = ""
        try:
            stdout, stderr = process.communicate(timeout=1)
        except Exception:
            stderr = ""
            stdout = ""
    return _jsonl_payload_result(
        result_payload=result_payload,
        error_payload=error_payload,
        collector=collector,
        started_at=started_at,
        fallback_error=_worker_exit_fallback_error(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )


def _worker_exit_fallback_error(*, returncode: int | None, stdout: str, stderr: str) -> str:
    detail_parts = [f"Worker exited without result (returncode={returncode})"]
    stderr_tail = _tail_text(stderr)
    stdout_tail = _tail_text(stdout)
    if stderr_tail:
        detail_parts.append(f"stderr={stderr_tail}")
    if stdout_tail:
        detail_parts.append(f"stdout={stdout_tail}")
    return "; ".join(detail_parts)


def _tail_text(value: str, *, limit: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _read_jsonl_response_from_url(
    url: str,
    payload: dict[str, Any],
    *,
    trace: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    started_at = time.monotonic()
    collector = _TraceFrameCollector(trace)
    result_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                frame = _parse_jsonl_frame(raw_line.decode("utf-8"))
                result_payload, error_payload = _record_jsonl_frame(
                    frame,
                    collector,
                    result_payload=result_payload,
                    error_payload=error_payload,
                )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") or f"helper returned HTTP {exc.code}"
        summary = collector.write_summary(
            status="error",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error_code="helper_http_error",
            error_message=detail,
            error_type="HTTPError",
            http_status=exc.code,
        )
        raise InvocationFailedError(detail, code="helper_http_error", trace_summary=summary) from exc
    except URLError as exc:
        summary = collector.write_summary(
            status="error",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error_code="helper_unavailable",
            error_message="Helper sidecar is unavailable",
            error_type="URLError",
            http_status=503,
        )
        raise InvocationFailedError("Helper sidecar is unavailable", code="helper_unavailable", trace_summary=summary) from exc
    return _jsonl_payload_result(
        result_payload=result_payload,
        error_payload=error_payload,
        collector=collector,
        started_at=started_at,
        fallback_error="Helper exited without result",
    )


def _record_jsonl_frame(
    frame: dict[str, Any],
    collector: "_TraceFrameCollector",
    *,
    result_payload: dict[str, Any] | None,
    error_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    frame_type = str(frame.get("type") or "")
    if frame_type == "trace_record":
        collector.add_record(frame.get("record"))
        return result_payload, error_payload
    if frame_type == "result" or "result" in frame:
        return frame, error_payload
    if frame_type == "error":
        return result_payload, frame
    if frame.get("error"):
        return result_payload, {
            "type": "error",
            "error": frame.get("error"),
            "detail": frame.get("detail") or frame.get("error"),
        }
    return result_payload, error_payload


def _jsonl_payload_result(
    *,
    result_payload: dict[str, Any] | None,
    error_payload: dict[str, Any] | None,
    collector: "_TraceFrameCollector",
    started_at: float,
    fallback_error: str,
) -> dict[str, Any]:
    duration_ms = int((time.monotonic() - started_at) * 1000)
    if result_payload is not None:
        for record in result_payload.get("trace_records") or []:
            collector.add_record(record)
        summary = collector.write_summary(status="ok", duration_ms=duration_ms)
        response: dict[str, Any] = {"result": result_payload.get("result")}
        if summary:
            response["trace_summary"] = summary
        return response
    if error_payload is not None:
        summary = collector.write_summary(
            status="error",
            duration_ms=duration_ms,
            error_code=str(error_payload.get("error") or "invoke_failed"),
            error_message=str(error_payload.get("detail") or "Worker invocation failed"),
            error_type="RuntimeError",
            http_status=400,
        )
        raise InvocationFailedError(
            str(error_payload.get("detail") or "Worker invocation failed"),
            code=str(error_payload.get("error") or "invoke_failed"),
            trace_summary=summary,
        )
    summary = collector.write_summary(
        status="error",
        duration_ms=duration_ms,
        error_code="invoke_failed",
        error_message=fallback_error,
        error_type="RuntimeError",
        http_status=400,
    )
    raise InvocationFailedError(fallback_error, trace_summary=summary)


def _docker_container_name(slot_id: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in slot_id.lower()).strip("-")
    suffix = secrets.token_hex(4)
    return f"agentcicd-worker-{safe[:48]}-{suffix}" if safe else f"agentcicd-worker-{suffix}"


def _docker_source_copy_pairs(config: DockerWorkerConfig) -> tuple[tuple[str, str], ...]:
    source_paths = tuple(str(item).strip() for item in config.source_paths if str(item).strip())
    worker_source_paths = tuple(str(item).strip() for item in config.worker_source_paths if str(item).strip())
    if source_paths:
        if worker_source_paths and len(worker_source_paths) != len(source_paths):
            raise ValueError("AGENTCICD_SANDBOX_WORKER_SOURCE_PATHS must match AGENTCICD_FUNCTION_SOURCE_PATHS")
        if not worker_source_paths:
            worker_source_paths = source_paths
        return tuple(zip(source_paths, worker_source_paths, strict=True))
    if config.source_path and config.worker_source_path:
        return ((config.source_path, config.worker_source_path),)
    return ()


def _worker_env_from_current_process() -> tuple[tuple[str, str], ...]:
    allowed_exact = {
        "AGENTCICD_FUNCTION_ID",
        "AGENTCICD_FUNCTION_TYPE",
        "AGENTCICD_FUNCTION_CALL_NAME",
        "AGENTCICD_FUNCTION_RUNTIME_ALIAS",
        "AGENTCICD_FUNCTION_SOURCE_PATH",
        "AGENTCICD_FUNCTION_SOURCE_PATHS",
        "AGENTCICD_FUNCTION_ENTRYPOINT_NAME",
        "AGENTCICD_FUNCTION_BUILTIN_CALL_NAME",
        "AGENTCICD_FUNCTION_BUILTIN_ENTRYPOINT",
        "AGENTCICD_FUNCTION_BUILTINS_JSON",
        "AGENTCICD_RUN_OBJECT_URI",
    }
    allowed_prefixes = (
        "AGENTCICD_OBJECT_STORE_",
        "AGENTCICD_S3_",
        "AGENTCICD_MINIO_",
        "AWS_",
    )
    copied: list[tuple[str, str]] = []
    for name, value in sorted(os.environ.items()):
        if name in allowed_exact or any(name.startswith(prefix) for prefix in allowed_prefixes):
            copied.append((name, value))
    return tuple(copied)


def main() -> None:
    from agentcicd.sandbox import function_runner

    function_runner.load_user_source()
    function_runner.load_builtin_function()
    manager = SandboxManager(manager_config_from_env(), worker_lifecycle_from_env())
    driver_base_url = os.getenv("AGENTCICD_RATE_LIMITER_BASE_URL", "").strip()
    if driver_base_url:
        manager.register_capacity(driver_base_url=driver_base_url)
        manager.start_capacity_heartbeat(driver_base_url=driver_base_url)
    serve(manager, port=int(os.getenv("AGENTCICD_FUNCTION_RUNTIME_PORT", "8080")))


if __name__ == "__main__":
    main()
