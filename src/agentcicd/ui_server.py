from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from agentcicd.inspection.local import LocalInspectionStore


@dataclass(frozen=True, slots=True)
class LocalInspectionServer:
    store: LocalInspectionStore
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def project_url(self) -> str:
        return f"{self.base_url}/projects/{self.store.project_id}/"

    def run_url(self, run_id: str) -> str:
        return f"{self.base_url}/runs/{run_id}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def __enter__(self) -> "LocalInspectionServer":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def start_local_inspection_server(project_dir: str | Path, *, port: int = 0) -> LocalInspectionServer:
    store = LocalInspectionStore(project_dir)
    handler = _handler_for(store)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, name="agentcicd-local-inspection", daemon=True)
    thread.start()
    return LocalInspectionServer(store=store, server=server, thread=thread)


def serve_local_inspection(project_dir: str | Path, *, port: int = 0) -> None:
    with start_local_inspection_server(project_dir, port=port) as server:
        print(f"AgentCICD UI: {server.project_url()}", flush=True)
        try:
            server.thread.join()
        except KeyboardInterrupt:
            return


def _handler_for(store: LocalInspectionStore) -> type[BaseHTTPRequestHandler]:
    class InspectionRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentCICDInspection/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                self._handle_get(unquote(parsed.path), parse_qs(parsed.query))
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Inspection resource not found"})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"detail": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                self._handle_post(unquote(parsed.path), self._read_json_body())
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "Inspection resource not found"})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"detail": str(exc)})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

        def _handle_get(self, path: str, query: dict[str, list[str]]) -> None:
            if path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            if path.startswith("/inspection/v1/"):
                self._handle_api(path.removeprefix("/inspection/v1"), query)
                return
            if path == "/runs" or (path.startswith("/runs/") and not path.endswith("/")):
                self._handle_public_run_get(path.removeprefix("/runs"), query)
                return
            if path == "/recipes" or path.startswith("/recipes/"):
                self._handle_public_recipe_get(path.removeprefix("/recipes"))
                return
            self._handle_viewer(path)

        def _handle_post(self, path: str, payload: dict[str, Any]) -> None:
            if path.startswith("/inspection/v1/"):
                segments = [segment for segment in path.removeprefix("/inspection/v1").split("/") if segment]
                if segments[:1] == ["runs"] and len(segments) >= 2:
                    self._handle_run_post_api(segments[1], segments[2:], payload)
                    return
            if path == "/recipes/analysis":
                self._send_json(HTTPStatus.OK, store.recipe_analysis(payload))
                return
            raise KeyError(path)

        def _handle_api(self, path: str, query: dict[str, list[str]]) -> None:
            segments = [segment for segment in path.split("/") if segment]
            if segments[:2] == ["projects", store.project_id]:
                self._handle_project_api(segments[2:])
                return
            if segments[:1] == ["runs"] and len(segments) >= 2:
                self._handle_run_api(segments[1], segments[2:], query)
                return
            raise KeyError(path)

        def _handle_project_api(self, tail: list[str]) -> None:
            if not tail:
                self._send_json(HTTPStatus.OK, store.project())
                return
            if tail == ["recipes"]:
                self._send_json(HTTPStatus.OK, store.recipes())
                return
            if tail[:1] == ["recipes"] and len(tail) == 2:
                self._send_json(HTTPStatus.OK, store.recipe(tail[1]))
                return
            if tail == ["fixtures"]:
                self._send_json(HTTPStatus.OK, store.fixtures())
                return
            if tail[:1] == ["fixtures"] and len(tail) == 2:
                self._send_json(HTTPStatus.OK, store.fixture(tail[1]))
                return
            if tail == ["inputs"]:
                self._send_json(HTTPStatus.OK, store.inputs())
                return
            if tail == ["secrets"]:
                self._send_json(HTTPStatus.OK, store.secrets())
                return
            if tail == ["runs"]:
                self._send_json(HTTPStatus.OK, store.runs())
                return
            raise KeyError("/".join(tail))

        def _handle_run_api(self, run_id: str, tail: list[str], query: dict[str, list[str]]) -> None:
            if not tail:
                self._send_json(HTTPStatus.OK, store.run_summary(run_id))
                return
            if tail == ["progress"]:
                self._send_json(HTTPStatus.OK, store.progress(run_id))
                return
            if tail == ["logs"]:
                self._send_json(HTTPStatus.OK, store.logs(run_id))
                return
            if tail == ["graph"]:
                self._send_json(HTTPStatus.OK, store.graph(run_id))
                return
            if tail == ["report"]:
                self._send_json(HTTPStatus.OK, store.report(run_id))
                return
            if tail == ["tables"]:
                self._send_json(HTTPStatus.OK, store.tables(run_id))
                return
            if tail[:1] == ["tables"] and len(tail) >= 3:
                table_name = tail[1]
                if tail[2:] == ["schema"]:
                    self._send_json(HTTPStatus.OK, store.table_schema(run_id, table_name))
                    return
                if tail[2:] == ["rows"]:
                    self._send_json(
                        HTTPStatus.OK,
                        store.table_rows(run_id, table_name, page=_query_int(query, "page", 1), page_size=_query_int(query, "page_size", 25)),
                    )
                    return
            if tail == ["traces"]:
                self._send_json(HTTPStatus.OK, store.traces(run_id))
                return
            if tail[:1] == ["traces"] and len(tail) >= 2:
                trace_id = tail[1]
                if len(tail) == 2:
                    self._send_json(HTTPStatus.OK, store.trace(run_id, trace_id))
                    return
                if tail[2:] == ["spans"]:
                    self._send_json(
                        HTTPStatus.OK,
                        store.trace_spans(run_id, trace_id, page=_query_int(query, "page", 1), page_size=_query_int(query, "page_size", 100)),
                    )
                    return
            if tail[:1] == ["artifacts"] and len(tail) >= 2:
                content_type, data = store.artifact(run_id, "/".join(tail[1:]))
                self._send_bytes(HTTPStatus.OK, content_type, data)
                return
            if tail == ["annotations", "requests"]:
                self._send_json(HTTPStatus.OK, store.annotation_requests(run_id))
                return
            if tail[:2] == ["annotations", "requests"] and len(tail) >= 3:
                request_id = tail[2]
                if len(tail) == 3:
                    self._send_json(HTTPStatus.OK, store.annotation_request(run_id, request_id))
                    return
                if tail[3:] == ["tasks"]:
                    self._send_json(HTTPStatus.OK, store.annotation_tasks(run_id, request_id))
                    return
                if tail[3:4] == ["tasks"] and len(tail) == 5:
                    self._send_json(HTTPStatus.OK, store.annotation_task(run_id, request_id, tail[4]))
                    return
            if tail == ["runtime", "pools"]:
                self._send_json(HTTPStatus.OK, store.runtime_pools(run_id))
                return
            if tail == ["runtime", "rate-limits"]:
                self._send_json(HTTPStatus.OK, store.runtime_rate_limits(run_id))
                return
            raise KeyError("/".join(tail))

        def _handle_public_run_get(self, path: str, query: dict[str, list[str]]) -> None:
            tail = [segment for segment in path.split("/") if segment]
            if not tail:
                runs = store.public_runs()
                status_filter = (query.get("status") or [None])[0]
                if status_filter:
                    runs = [item for item in runs if item.get("status") == status_filter]
                self._send_json(HTTPStatus.OK, runs)
                return
            run_id = tail[0]
            if len(tail) == 1:
                self._send_json(HTTPStatus.OK, store.public_run(run_id))
                return
            if tail[1:] == ["progress"]:
                self._send_json(HTTPStatus.OK, store.public_progress(run_id))
                return
            raise KeyError("/".join(tail))

        def _handle_public_recipe_get(self, path: str) -> None:
            tail = [segment for segment in path.split("/") if segment]
            if not tail:
                self._send_json(HTTPStatus.OK, store.public_recipes())
                return
            recipe_id = tail[0]
            if len(tail) == 1:
                self._send_json(HTTPStatus.OK, store.public_recipe(recipe_id))
                return
            if tail[1:] == ["segments"]:
                self._send_json(HTTPStatus.OK, store.recipe_segments(recipe_id))
                return
            raise KeyError("/".join(tail))

        def _handle_run_post_api(self, run_id: str, tail: list[str], payload: dict[str, Any]) -> None:
            if tail[:2] == ["annotations", "requests"] and len(tail) >= 3:
                request_id = tail[2]
                if tail[3:4] == ["tasks"] and len(tail) == 6 and tail[5] == "reviews":
                    self._send_json(HTTPStatus.OK, store.submit_annotation_review(run_id, request_id, tail[4], payload))
                    return
                if tail[3:] == ["finalize"]:
                    self._send_json(HTTPStatus.OK, store.finalize_annotation_request(run_id, request_id))
                    return
            raise KeyError("/".join(tail))

        def _handle_viewer(self, path: str) -> None:
            asset = _static_asset(path) or _static_asset("/")
            if asset is None:
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", _fallback_viewer(store.project_id).encode("utf-8"))
                return
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            self._send_bytes(HTTPStatus.OK, content_type, asset.read_bytes())

        def _send_json(self, status_code: HTTPStatus, payload: dict[str, Any]) -> None:
            self._send_bytes(status_code, "application/json; charset=utf-8", json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))

        def _send_bytes(self, status_code: HTTPStatus, content_type: str, data: bytes) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("Request body must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            return payload

    return InspectionRequestHandler


def _query_int(query: dict[str, list[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _static_asset(path: str) -> Path | None:
    relative = path.lstrip("/") or "index.html"
    if any(part in {"", ".", ".."} for part in Path(relative).parts):
        return None
    root = Path(__file__).with_name("ui_static")
    candidate = root / relative
    return candidate if candidate.is_file() else None


def _fallback_viewer(project_id: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>AgentCICD Inspector</title></head>
<body><main><h1>AgentCICD Inspector</h1><p id=\"status\">Loading project...</p><pre id=\"payload\"></pre></main>
<script>
const endpoint = '/inspection/v1/projects/{project_id}';
fetch(endpoint).then((response) => response.json()).then((payload) => {{
  document.getElementById('status').textContent = payload.project.name;
  document.getElementById('payload').textContent = JSON.stringify(payload, null, 2);
}}).catch((error) => {{ document.getElementById('status').textContent = error.message; }});
</script></body></html>"""
