from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentcicd.sql.engine.interfaces import BackendLayout, PublicationStore
from agentcicd.sql.integration import validate_label_studio_template_xml


@dataclass(frozen=True)
class PublicationResponse:
    error: str | None = None
    request_id: str | None = None

    @classmethod
    def from_json(cls, raw_response: str, *, path: str) -> "PublicationResponse":
        if not raw_response.strip():
            return cls()
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Publication request to '{path}' returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Publication request to '{path}' returned an invalid payload shape")
        error = payload.get("error")
        request_id = payload.get("request_id")
        return cls(
            error=str(error) if isinstance(error, str) and error else None,
            request_id=str(request_id) if isinstance(request_id, str) and request_id else None,
        )


def _unwrap_cell(candidate: Any) -> tuple[Any, dict[str, Any]]:
    if isinstance(candidate, dict) and "value" in candidate:
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            error = _cell_error(metadata)
            if error is not None:
                return error, metadata
            return candidate.get("value"), metadata
        return candidate.get("value"), {}
    return candidate, {}


def _cell_error(metadata: dict[str, Any]) -> Any:
    if metadata.get("error") is not None:
        return metadata.get("error")
    errors = metadata.get("errors")
    if isinstance(errors, list) and errors:
        return errors[0]
    return None


def _stringify_tag_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(value)


def _json_safe_report_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, bytearray):
        return _json_safe_report_value(bytes(value))
    if isinstance(value, memoryview):
        return _json_safe_report_value(value.tobytes())
    if isinstance(value, dict):
        return {str(key): _json_safe_report_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_report_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_report_value(item) for item in value]
    return value


def _coerce_metric_tags(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        tags: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return {"tags": _stringify_tag_value(value)}
            key, tag_value = item
            tags[str(key)] = tag_value
        return tags
    return {"tags": _stringify_tag_value(value)}


def _table_rows_from_dir(table_dir: Path, name: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not table_dir.exists():
        raise FileNotFoundError(f"PUBLISH table '{name}' not found at '{table_dir}'")

    try:
        import pyarrow.parquet as pq
    except Exception as exc:  # pragma: no cover - optional dependency guard
        raise RuntimeError("pyarrow is required to publish reports from local tables") from exc

    parquet_files = sorted(
        path for path in table_dir.rglob("*.parquet") if path.is_file()
    )
    if not parquet_files and table_dir.is_file() and table_dir.suffix == ".parquet":
        parquet_files = [table_dir]

    rows: list[dict[str, Any]] = []
    schema_names: list[str] = []
    seen_schema_names: set[str] = set()
    for parquet_file in parquet_files:
        table = pq.read_table(parquet_file)
        for field_name in table.schema.names:
            if field_name not in seen_schema_names:
                seen_schema_names.add(field_name)
                schema_names.append(field_name)
        rows.extend(row for row in table.to_pylist() if isinstance(row, dict))
    return rows, schema_names


def _table_rows(layout: BackendLayout, name: str) -> tuple[list[dict[str, Any]], list[str]]:
    return _table_rows_from_dir(Path(layout.tables_root) / name, name)


def _metric_rows_from_table(layout: BackendLayout, name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, schema_names = _table_rows(layout, name)
    column_names = {column.lower(): column for column in schema_names}
    required_columns = {"metric", "value"}
    missing_columns = required_columns - set(column_names)
    if missing_columns:
        raise ValueError(
            f"PUBLISH table '{name}' missing required columns: {missing_columns}. "
            f"Available columns: {schema_names}"
        )

    metric_column = column_names["metric"]
    value_column = column_names["value"]
    tags_column = column_names.get("tags")

    scores: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for row_data in rows:
        if not isinstance(row_data, dict):
            continue

        metric_raw = row_data.get(metric_column)
        metric_value, _ = _unwrap_cell(metric_raw)
        if metric_value is None:
            issues.append(_metric_publication_issue(name, row_data, "Metric row has null metric name"))
            continue

        value_raw = row_data.get(value_column)
        value_unwrapped, value_metadata = _unwrap_cell(value_raw)
        value_error = _cell_error(value_metadata)
        if value_error is not None:
            issues.append(_metric_publication_issue(name, row_data, "Metric row value has cell errors", value_error))
            continue
        try:
            numeric_value = float(value_unwrapped)
        except (TypeError, ValueError):
            issues.append(
                _metric_publication_issue(
                    name,
                    row_data,
                    f"Metric row value is not numeric: {_stringify_tag_value(value_unwrapped)}",
                )
            )
            continue

        if tags_column and tags_column in row_data:
            tags, _ = _unwrap_cell(row_data.get(tags_column))
            tags = _coerce_metric_tags(tags)
        else:
            tags = {}
            for column_name, column_value in row_data.items():
                if column_name.lower() in required_columns:
                    continue
                if column_name.startswith("__agentcicd_"):
                    continue
                if column_value is not None:
                    tag_value, _ = _unwrap_cell(column_value)
                    tags[column_name] = _stringify_tag_value(tag_value)

        scores.append(
            {
                "metric": _json_safe_report_value(metric_raw if metric_raw is not None else str(metric_value)),
                "value": _json_safe_report_value(value_raw if value_raw is not None else numeric_value),
                "tags": _json_safe_report_value(tags),
            }
        )
    return scores, issues


def _metric_publication_issue(
    table_name: str,
    row_data: dict[str, Any],
    description: str,
    error: Any | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "title": "Metric row not published",
        "severity": "medium",
        "description": description,
        "table": table_name,
        "row": _json_safe_report_value(
            {key: _unwrap_cells(value) for key, value in row_data.items() if not str(key).startswith("__agentcicd_")}
        ),
    }
    if error is not None:
        issue["error"] = _json_safe_report_value(error)
    return issue


def _generic_rows_from_table(layout: BackendLayout, name: str) -> list[dict[str, Any]]:
    rows, _ = _table_rows(layout, name)
    return [
        _json_safe_report_value(
            {key: _unwrap_cells(value) for key, value in row.items() if not str(key).startswith("__agentcicd_")}
        )
        for row in rows
    ]


def _unwrap_cells(value: Any) -> Any:
    if isinstance(value, dict) and "value" not in value:
        return {str(key): _unwrap_cells(item) for key, item in value.items()}
    if isinstance(value, dict) and "value" in value and "metadata" not in value and "__agentcicd_cell" not in value:
        return {str(key): _unwrap_cells(item) for key, item in value.items()}
    unwrapped, _ = _unwrap_cell(value)
    if isinstance(unwrapped, dict):
        return {str(key): _unwrap_cells(item) for key, item in unwrapped.items()}
    if isinstance(unwrapped, list):
        return [_unwrap_cells(item) for item in unwrapped]
    if isinstance(unwrapped, tuple):
        return [_unwrap_cells(item) for item in unwrapped]
    return unwrapped


def _annotation_rows_from_table(layout: BackendLayout, name: str) -> list[dict[str, Any]]:
    table_dir = Path(layout.tables_root) / name
    source_dir = Path(layout.sources_root) / name
    if table_dir.exists():
        rows, _ = _table_rows_from_dir(table_dir, name)
    elif source_dir.exists():
        rows, _ = _table_rows_from_dir(source_dir, name)
    else:
        raise FileNotFoundError(f"PUBLISH table '{name}' not found at '{table_dir}' or '{source_dir}'")
    return [_json_safe_report_value(row) for row in rows]


def _option(options: Mapping[str, object] | None, name: str, default: Any = None) -> Any:
    normalized_name = name.strip().lower()
    for key, value in dict(options or {}).items():
        if str(key).strip().lower() == normalized_name:
            return value
    return default


def _annotation_policy(options: Mapping[str, object] | None) -> dict[str, Any]:
    reviewers_per_task = int(_option(options, "reviewers_per_task", 1) or 1)
    reservation_minutes = int(_option(options, "reservation_minutes", 30) or 30)
    consensus = str(_option(options, "consensus", "none") or "none").strip().lower()
    if reviewers_per_task < 1 or reviewers_per_task > 10:
        raise ValueError("REVIEWERS_PER_TASK must be between 1 and 10")
    if reservation_minutes < 5 or reservation_minutes > 1440:
        raise ValueError("RESERVATION_MINUTES must be between 5 and 1440")
    if consensus not in {"none", "majority", "unanimous"}:
        raise ValueError("CONSENSUS must be one of: none, majority, unanimous")
    return {
        "reviewers_per_task": reviewers_per_task,
        "reservation_minutes": reservation_minutes,
        "consensus": consensus,
    }


def _generated_annotation_request_id() -> str:
    return f"annreq.{secrets.token_hex(8)}"


def _safe_annotation_ref(value: str) -> str:
    normalized = str(value or "").strip().replace("/", "_")
    if not normalized:
        raise ValueError("Annotation reference must not be empty")
    return normalized


def _report_file_for_component(layout: BackendLayout, component: str) -> Path:
    reports_dir = Path(layout.working_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    if component == "metric":
        return reports_dir / "metrics.json"
    if component == "chart":
        return reports_dir / "charts.json"
    if component == "issue":
        return reports_dir / "issues.json"
    raise ValueError(f"Unsupported report component '{component}'")


def _append_json_list(path: Path, rows: list[dict[str, Any]]) -> None:
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Existing report file '{path}' is invalid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError(f"Existing report file '{path}' must contain a JSON list")
        existing = [item for item in payload if isinstance(item, dict)]
    path.write_text(json.dumps([*existing, *rows], indent=2), encoding="utf-8")


_ISSUE_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_CHART_AGGREGATIONS = {"sum", "count", "avg", "min", "max"}


def _slug(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    return "_".join(part for part in normalized.split("_") if part) or "chart"


def _bool_option(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean chart option value '{value}'")


def _int_option(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer chart option value '{value}'") from exc
    if parsed < 1:
        raise ValueError("Chart LIMIT must be greater than zero")
    return parsed


def _chart_definition(name: str, chart_type: str | None, report_options: dict[str, str] | None, data: list[dict[str, Any]]) -> dict[str, Any]:
    options = {str(key).strip().lower(): str(value).strip() for key, value in (report_options or {}).items()}
    normalized_type = (chart_type or options.get("chart_type") or "").strip().lower()
    if not normalized_type:
        raise ValueError("PUBLISH chart reports require CHART_TYPE")
    x_axis = options.get("x_axis") or options.get("x")
    y_axis = options.get("y_axis") or options.get("y")
    if not x_axis or not y_axis:
        raise ValueError("PUBLISH chart reports require X_AXIS and Y_AXIS")
    aggregation = (options.get("aggregation") or "avg").strip().lower()
    if aggregation not in _CHART_AGGREGATIONS:
        raise ValueError(f"Unsupported chart aggregation '{aggregation}'")
    title = options.get("title") or f"{y_axis} by {x_axis}"
    chart: dict[str, Any] = {
        "id": options.get("id") or _slug(title or name),
        "title": title,
        "chart_type": normalized_type,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "aggregation": aggregation,
        "data": data,
    }
    for source, target in (
        ("x_axis_label", "x_axis_label"),
        ("y_axis_label", "y_axis_label"),
        ("group_by", "group_by"),
    ):
        if options.get(source):
            chart[target] = options[source]
    stacked = _bool_option(options.get("stacked"))
    if stacked is not None:
        chart["stacked"] = stacked
    limit = _int_option(options.get("limit"))
    if limit is not None:
        chart["limit"] = limit
    sort_field = options.get("sort") or options.get("sort_by") or options.get("sort_field")
    if sort_field:
        direction = (options.get("sort_direction") or options.get("direction") or "desc").strip().lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("Chart sort direction must be ASC or DESC")
        chart["sort"] = {"field": sort_field, "direction": direction}
    return chart


def _validate_issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for row in rows:
        next_row = {
            key: _unwrap_cell(value)[0]
            for key, value in row.items()
        }
        severity = next_row.get("severity")
        if severity is not None:
            normalized = str(severity).strip().lower()
            if normalized not in _ISSUE_SEVERITIES:
                raise ValueError(
                    "Issue severity must be one of: critical, high, medium, low, info"
                )
            next_row["severity"] = normalized
        validated.append(next_row)
    return validated


class LocalManifestPublicationStore(PublicationStore):
    def publish_report(
        self,
        layout: BackendLayout,
        name: str,
        component: str,
        chart_type: str | None = None,
        report_options: dict[str, str] | None = None,
    ) -> None:
        normalized_component = component.strip().lower()
        metric_issues: list[dict[str, Any]] = []
        if normalized_component == "metric":
            rows, metric_issues = _metric_rows_from_table(layout, name)
        elif normalized_component == "chart":
            rows = [_chart_definition(name, chart_type, report_options, _generic_rows_from_table(layout, name))]
        elif normalized_component == "issue":
            rows = _validate_issue_rows(_generic_rows_from_table(layout, name))
        else:
            raise ValueError(f"Unsupported report component '{component}'")
        report_path = _report_file_for_component(layout, normalized_component)
        _append_json_list(report_path, rows)
        if metric_issues:
            _append_json_list(_report_file_for_component(layout, "issue"), metric_issues)
        self._write_manifest(
            layout,
            f"reports_{normalized_component}",
            name,
            {
                "component": normalized_component,
                "chart_type": chart_type,
                "report_options": report_options or {},
                "rows": rows,
                "issues": metric_issues,
            },
        )

    def publish_dataset(self, layout: BackendLayout, name: str, dataset_name: str | None) -> None:
        self._write_manifest(
            layout,
            "dataset",
            name,
            {
                "dataset_name": dataset_name,
                "data_path": f"published_datasets/{name}",
                "format": "parquet",
            },
        )

    def publish_annotation(
        self,
        layout: BackendLayout,
        name: str,
        queue_name: str,
        *,
        alias: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> None:
        options_payload = dict(options or {})
        template = str(_option(options_payload, "template", "") or "").strip()
        if not template:
            raise ValueError("TEMPLATE is required for local annotation publication")
        validate_label_studio_template_xml(template, context="TEMPLATE")
        policy = _annotation_policy(options_payload)
        request_id = _generated_annotation_request_id()
        source_ref = alias or name
        request_root = Path(layout.annotation_tasks_root) / request_id
        request_root.mkdir(parents=True, exist_ok=False)
        rows = _annotation_rows_from_table(layout, name)
        task_lines = []
        for index, row in enumerate(rows):
            task_lines.append(
                json.dumps(
                    {
                        "task_id": f"task_{index:06d}",
                        "data": _json_safe_report_value(_unwrap_cells(row)),
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            )
        data_path = request_root / "tasks.jsonl"
        reviews_path = request_root / "reviews"
        results_path = request_root / "results.jsonl"
        manifest_path = request_root / "manifest.json"
        reviews_path.mkdir(parents=True, exist_ok=True)
        data_path.write_text("\n".join(task_lines) + ("\n" if task_lines else ""), encoding="utf-8")
        manifest = {
            "queue_name": queue_name,
            "source_table": name,
            "publish_alias": alias,
            "instructions": _option(options_payload, "instructions", None),
            "template": template,
            "review_policy": policy,
            "request_id": request_id,
            "data_path": data_path.as_posix(),
            "reviews_path": reviews_path.as_posix(),
            "results_path": results_path.as_posix(),
            "manifest_path": manifest_path.as_posix(),
            "status": "pending",
            "total_tasks": len(task_lines),
            "completed_tasks": 0,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        alias_root = Path(layout.annotation_tasks_root) / _safe_annotation_ref(source_ref)
        alias_root.mkdir(parents=True, exist_ok=True)
        (alias_root / "request.json").write_text(
            json.dumps({"request_id": request_id, "alias": alias, "table": name}, indent=2),
            encoding="utf-8",
        )

        self._write_manifest(
            layout,
            "annotation",
            alias or name,
            {
                "table": name,
                "queue_name": queue_name,
                "alias": alias,
                "options": dict(options or {}),
                "request_id": request_id,
                "tasks_path": data_path.as_posix(),
                "manifest_path": manifest_path.as_posix(),
            },
        )

    @staticmethod
    def _write_manifest(layout: BackendLayout, kind: str, name: str, payload: dict) -> None:
        publish_root = Path(layout.publish_root)
        publish_root.mkdir(parents=True, exist_ok=True)
        manifest_path = publish_root / f"{kind}_{name}.json"
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class DriverArtifactPublicationStore(PublicationStore):
    """Publish reports/datasets as driver artifacts; publish annotations through DP API."""

    def __init__(self, remote_store: "HttpPublicationStore") -> None:
        self._local_store = LocalManifestPublicationStore()
        self._remote_store = remote_store

    def publish_report(
        self,
        layout: BackendLayout,
        name: str,
        component: str,
        chart_type: str | None = None,
        report_options: dict[str, str] | None = None,
    ) -> None:
        self._local_store.publish_report(layout, name, component, chart_type, report_options)

    def publish_dataset(self, layout: BackendLayout, name: str, dataset_name: str | None) -> None:
        self._local_store.publish_dataset(layout, name, dataset_name)

    def publish_annotation(
        self,
        layout: BackendLayout,
        name: str,
        queue_name: str,
        *,
        alias: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> None:
        self._remote_store.publish_annotation(layout, name, queue_name, alias=alias, options=options)


class HttpPublicationStore(PublicationStore):
    def __init__(
        self,
        *,
        base_url: str,
        reports_path: str = "/publish/reports",
        dataset_path: str = "/publish/dataset",
        annotation_path: str = "/publish/annotation",
        timeout_seconds: int = 30,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._reports_path = reports_path
        self._dataset_path = dataset_path
        self._annotation_path = annotation_path
        self._timeout_seconds = timeout_seconds
        self._headers = {str(key): str(value) for key, value in (headers or {}).items() if value}

    def publish_report(
        self,
        layout: BackendLayout,
        name: str,
        component: str,
        chart_type: str | None = None,
        report_options: dict[str, str] | None = None,
    ) -> None:
        self._post(
            self._reports_path,
            {
                "table": name,
                "component": component,
                "chart_type": chart_type,
                "report_options": report_options or {},
                "working_dir": layout.working_dir,
                **self._runtime_context(),
            },
        )

    def publish_dataset(self, layout: BackendLayout, name: str, dataset_name: str | None) -> None:
        self._post(
            self._dataset_path,
            {"table": name, "dataset_name": dataset_name, "working_dir": layout.working_dir, **self._runtime_context()},
        )

    def publish_annotation(
        self,
        layout: BackendLayout,
        name: str,
        queue_name: str,
        *,
        alias: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> None:
        response = self._post(
            self._annotation_path,
            {
                "table": name,
                "queue_name": queue_name,
                "alias": alias,
                "options": dict(options or {}),
                "rows": _annotation_rows_from_table(layout, name),
                "working_dir": layout.working_dir,
                **self._runtime_context(),
            },
        )
        if response.request_id:
            source_ref = alias or name
            alias_root = Path(layout.annotation_tasks_root) / source_ref
            alias_root.mkdir(parents=True, exist_ok=True)
            (alias_root / "request.json").write_text(
                json.dumps({"request_id": response.request_id, "alias": alias, "table": name}, indent=2),
                encoding="utf-8",
            )

    @staticmethod
    def _runtime_context() -> dict[str, str]:
        keys = {
            "organization_id": "AGENTCICD_ORGANIZATION_ID",
            "run_id": "AGENTCICD_RUN_ID",
            "recipe_id": "AGENTCICD_RECIPE_ID",
            "cluster_id": "AGENTCICD_CLUSTER_ID",
        }
        return {
            payload_key: value
            for payload_key, env_key in keys.items()
            if (value := os.getenv(env_key, "").strip())
        }

    def _post(self, path: str, payload: dict) -> PublicationResponse:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **self._headers},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw_response = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Publication request to '{path}' failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Publication request to '{path}' could not be reached") from exc

        response = PublicationResponse.from_json(raw_response, path=path)
        if response.error:
            raise RuntimeError(f"Publication request to '{path}' failed: {response.error}")
        return response
