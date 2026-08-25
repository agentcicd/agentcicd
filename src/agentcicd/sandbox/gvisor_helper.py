from __future__ import annotations

import json
import os
import secrets
import select
import subprocess
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class HelperWorker:
    worker_id: str
    slot_id: str
    session_key: str = ""
    healthy: bool = True
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)


class WorkerStore:
    def __init__(self) -> None:
        self._workers: dict[str, HelperWorker] = {}

    def create(self, slot_id: str, session_key: str = "") -> HelperWorker:
        worker = HelperWorker(
            worker_id=f"gvisor-worker-{secrets.token_hex(8)}",
            slot_id=slot_id,
            session_key=session_key,
        )
        self._workers[worker.worker_id] = worker
        return worker

    def require(self, worker_id: str) -> HelperWorker:
        worker = self._workers.get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
        return worker

    def stop(self, worker_id: str) -> None:
        worker = self.require(worker_id)
        worker.healthy = False
        worker.last_used_at = time.monotonic()

    def clear(self, worker_id: str) -> None:
        worker = self.require(worker_id)
        worker.session_key = ""
        worker.last_used_at = time.monotonic()


class GVisorHelper:
    def __init__(self, store: WorkerStore) -> None:
        self.store = store

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self.store.create(
            slot_id=str(payload.get("slot_id") or ""),
            session_key=str(payload.get("session_key") or ""),
        )
        return {
            "worker_id": worker.worker_id,
            "slot_id": worker.slot_id,
            "healthy": worker.healthy,
        }

    def status(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker = self.store.require(str(payload.get("worker_id") or ""))
        return {
            "worker_id": worker.worker_id,
            "slot_id": worker.slot_id,
            "healthy": worker.healthy,
        }

    def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_id = str(payload.get("worker_id") or "")
        self.store.stop(worker_id)
        return {"worker_id": worker_id, "healthy": False}

    def clear(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_id = str(payload.get("worker_id") or "")
        self.store.clear(worker_id)
        return {"worker_id": worker_id, "cleared": True}

    def invoke(self, payload: dict[str, Any]) -> int:
        worker = self.store.require(str(payload.get("worker_id") or ""))
        worker.last_used_at = time.monotonic()
        command = _worker_invoke_command()
        process_payload = {
            "function_name": payload.get("function_name"),
            "args": payload.get("args"),
            "trace": payload.get("trace"),
            "secrets": payload.get("secrets"),
        }
        return _proxy_jsonl_process(command, process_payload)


def _worker_invoke_command() -> list[str]:
    configured = os.getenv("AGENTCICD_GVISOR_WORKER_INVOKE_COMMAND", "").strip()
    if configured:
        return configured.split()
    return ["python", "-m", "agentcicd.sandbox.function_runner", "--invoke-jsonl"]


def _proxy_jsonl_process(command: list[str], payload: dict[str, Any]) -> int:
    timeout_seconds = max(0.1, float(os.getenv("AGENTCICD_GVISOR_HELPER_INVOKE_TIMEOUT_SECONDS", "300")))
    started_at = time.monotonic()
    process = subprocess.Popen(  # noqa: S603
        command,
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
    try:
        while True:
            if time.monotonic() - started_at > timeout_seconds:
                process.kill()
                print(
                    json.dumps(
                        {
                            "type": "error",
                            "error": "invoke_timeout",
                            "detail": f"Fixture invocation exceeded {timeout_seconds:g}s",
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                return 1
            ready, _, _ = select.select([process.stdout], [], [], 0.05)
            line = process.stdout.readline() if ready else ""
            if line:
                print(line.rstrip("\n"), flush=True)
                continue
            if process.poll() is not None:
                break
            time.sleep(0.01)
    finally:
        if process.poll() is None:
            process.kill()
    return int(process.returncode or 0)


def _handler_for(helper: GVisorHelper) -> type[BaseHTTPRequestHandler]:
    class HelperRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentCICDGVisorHelper/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._write_json({"status": "ok"})
                return
            self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/worker":
                self._write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            payload = self._read_json()
            action = str(payload.get("action") or "")
            try:
                if action == "create":
                    self._write_json(helper.create(payload))
                    return
                if action == "status":
                    self._write_json(helper.status(payload))
                    return
                if action == "stop":
                    self._write_json(helper.stop(payload))
                    return
                if action == "clear":
                    self._write_json(helper.clear(payload))
                    return
                if action == "invoke":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    helper.invoke(payload)
                    return
            except KeyError as exc:
                self._write_json({"error": "unknown_worker", "detail": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:  # pragma: no cover
                self._write_json({"error": "helper_failed", "detail": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._write_json({"error": "unknown_action", "action": action}, HTTPStatus.BAD_REQUEST)

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

    return HelperRequestHandler


def serve() -> None:
    host = os.getenv("AGENTCICD_GVISOR_HELPER_HOST", "127.0.0.1")
    port = int(os.getenv("AGENTCICD_GVISOR_HELPER_PORT", "18081"))
    server = ThreadingHTTPServer((host, port), _handler_for(GVisorHelper(WorkerStore())))
    server.serve_forever()


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
