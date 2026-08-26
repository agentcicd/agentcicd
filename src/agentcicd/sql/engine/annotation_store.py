from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentcicd.sql.engine.interfaces import AnnotationStore, BackendLayout


@dataclass(frozen=True)
class AnnotationRowsResponse:
    rows: object


@dataclass(frozen=True)
class AnnotationParquetResponse:
    data_hex: str


AnnotationResponse = AnnotationRowsResponse | AnnotationParquetResponse


class AnnotationResultsPending(RuntimeError):
    def __init__(self, annotation_id: str, *, status_code: int | None = None) -> None:
        self.annotation_id = annotation_id
        self.status_code = status_code
        suffix = f" HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"Annotation results for '{annotation_id}' are not ready{suffix}")


class LocalAnnotationStore(AnnotationStore):
    def load_annotation_dataframe(self, spark_session, layout: BackendLayout, annotation_id: str):
        annotation_id = self._resolve_annotation_id(layout, annotation_id)
        annotation_root = Path(layout.annotation_tasks_root) / annotation_id
        if not annotation_root.exists():
            raise FileNotFoundError(f"Annotation task directory not found: {annotation_root}")
        candidate_paths = [
            annotation_root / "results.parquet",
            annotation_root / "results.json",
            annotation_root / "results.jsonl",
        ]
        candidate = next((path for path in candidate_paths if path.exists()), None)
        if candidate is None:
            raise AnnotationResultsPending(annotation_id)
        if candidate.suffix == ".parquet":
            return spark_session.read.format("parquet").load(str(candidate))
        return spark_session.read.json(str(candidate))

    @staticmethod
    def _resolve_annotation_id(layout: BackendLayout, annotation_id: str) -> str:
        alias_path = Path(layout.annotation_tasks_root) / annotation_id / "request.json"
        if not alias_path.exists():
            return annotation_id
        try:
            payload = json.loads(alias_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return annotation_id
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        return request_id if isinstance(request_id, str) and request_id else annotation_id


class HttpAnnotationStore(AnnotationStore):
    def __init__(
        self,
        *,
        base_url: str,
        results_path_template: str = "/annotations/requests/{annotation_id}/results",
        timeout_seconds: int = 30,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._results_path_template = results_path_template
        self._timeout_seconds = timeout_seconds
        self._headers = {str(key): str(value) for key, value in (headers or {}).items() if value}

    def load_annotation_dataframe(self, spark_session, layout: BackendLayout, annotation_id: str):
        annotation_id = self._resolve_annotation_id(layout, annotation_id)
        payload = self._fetch(annotation_id)
        target_root = Path(layout.annotation_tasks_root) / annotation_id
        target_root.mkdir(parents=True, exist_ok=True)

        if isinstance(payload, AnnotationParquetResponse):
            target = target_root / "results.parquet"
            target.write_bytes(bytes.fromhex(payload.data_hex))
            return spark_session.read.format("parquet").load(str(target))

        target = target_root / "results.json"
        target.write_text(json.dumps(payload.rows), encoding="utf-8")
        return spark_session.read.json(str(target))

    def _fetch(self, annotation_id: str) -> AnnotationResponse:
        path = self._results_path_template.format(annotation_id=annotation_id)
        try:
            request = Request(f"{self._base_url}{path}", headers=self._headers)
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw_response = response.read().decode("utf-8") or "null"
        except HTTPError as exc:
            if exc.code == 404:
                raise AnnotationResultsPending(annotation_id, status_code=exc.code) from exc
            raise RuntimeError(
                f"Annotation retrieval for '{annotation_id}' failed with HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Annotation retrieval for '{annotation_id}' could not be reached") from exc
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Annotation retrieval for '{annotation_id}' returned invalid JSON") from exc
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, str) and error:
                raise RuntimeError(f"Annotation retrieval for '{annotation_id}' failed: {error}")
            if payload.get("format") == "parquet":
                encoded = payload.get("data")
                if not isinstance(encoded, str):
                    raise ValueError("Parquet annotation response requires string 'data'")
                return AnnotationParquetResponse(data_hex=encoded)
            return AnnotationRowsResponse(rows=payload.get("rows"))
        return AnnotationRowsResponse(rows=payload)

    @staticmethod
    def _resolve_annotation_id(layout: BackendLayout, annotation_id: str) -> str:
        alias_path = Path(layout.annotation_tasks_root) / annotation_id / "request.json"
        if not alias_path.exists():
            return annotation_id
        try:
            payload = json.loads(alias_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return annotation_id
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        return request_id if isinstance(request_id, str) and request_id else annotation_id
