from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentcicd.sql.observability.redaction import redacted_preview


@dataclass(frozen=True)
class ReportSummary:
    metrics_count: int
    issues_count: int
    charts_count: int


def render_local_report(run_dir: Path, *, secret_values: tuple[str, ...] = ()) -> ReportSummary:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    metrics = _redact(_read_json_list(reports_dir / "metrics.json"), secret_values)
    issues = _redact(_read_json_list(reports_dir / "issues.json"), secret_values)
    charts = _redact(_read_json_list(reports_dir / "charts.json"), secret_values)
    summary = ReportSummary(
        metrics_count=len(metrics),
        issues_count=len(issues),
        charts_count=len(charts),
    )
    _write_json(reports_dir / "metrics.json", metrics)
    _write_json(reports_dir / "issues.json", issues)
    _write_json(reports_dir / "charts.json", charts)
    (reports_dir / "report.md").write_text(_markdown_report(metrics, issues, charts), encoding="utf-8")
    (reports_dir / "report.html").write_text(_html_report(metrics, issues, charts), encoding="utf-8")
    redact_local_artifacts(run_dir, secret_values=secret_values)
    return summary


def redact_local_artifacts(run_dir: Path, *, secret_values: tuple[str, ...]) -> None:
    if not secret_values:
        return
    for root_name in ("progress", "logs", "reports", "debug"):
        root = run_dir / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl", ".md", ".html", ".txt"}:
                continue
            _redact_file(path, secret_values)


def _read_json_list(path: Path) -> list[Any]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8") or "[]")
    return payload if isinstance(payload, list) else []


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _redact(value: Any, secret_values: tuple[str, ...]) -> Any:
    return redacted_preview(_redact_secret_values(value, secret_values), max_preview_bytes=65536)


def _redact_secret_values(value: Any, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_secret_values(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_values(item, secret_values) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret_value in secret_values:
            if secret_value:
                redacted = redacted.replace(secret_value, "[redacted]")
        return redacted
    return value


def _redact_file(path: Path, secret_values: tuple[str, ...]) -> None:
    raw_text = path.read_text(encoding="utf-8")
    redacted_text = raw_text
    for secret_value in secret_values:
        if secret_value:
            redacted_text = redacted_text.replace(secret_value, "[redacted]")
    if redacted_text != raw_text:
        path.write_text(redacted_text, encoding="utf-8")


def _markdown_report(metrics: list[Any], issues: list[Any], charts: list[Any]) -> str:
    lines = [
        "# AgentCICD Report",
        "",
        f"- Metrics: {len(metrics)}",
        f"- Issues: {len(issues)}",
        f"- Charts: {len(charts)}",
        "",
    ]
    if metrics:
        lines.extend(["## Metrics", ""])
        for metric in metrics:
            lines.append(f"- `{_safe_metric_name(metric)}`: {_safe_metric_value(metric)}")
        lines.append("")
    if issues:
        lines.extend(["## Issues", ""])
        for issue in issues:
            lines.append(f"- {_safe_issue_title(issue)}")
        lines.append("")
    return "\n".join(lines)


def _html_report(metrics: list[Any], issues: list[Any], charts: list[Any]) -> str:
    body = [
        "<!doctype html>",
        "<html><head><meta charset=\"utf-8\"><title>AgentCICD Report</title></head><body>",
        "<h1>AgentCICD Report</h1>",
        f"<p>Metrics: {len(metrics)} | Issues: {len(issues)} | Charts: {len(charts)}</p>",
    ]
    if metrics:
        body.append("<h2>Metrics</h2><ul>")
        for metric in metrics:
            body.append(f"<li><code>{html.escape(_safe_metric_name(metric))}</code>: {html.escape(_safe_metric_value(metric))}</li>")
        body.append("</ul>")
    if issues:
        body.append("<h2>Issues</h2><ul>")
        for issue in issues:
            body.append(f"<li>{html.escape(_safe_issue_title(issue))}</li>")
        body.append("</ul>")
    body.append("</body></html>")
    return "\n".join(body)


def _safe_metric_name(metric: Any) -> str:
    if isinstance(metric, dict):
        value = metric.get("metric")
        if isinstance(value, dict) and "value" in value:
            return str(value["value"])
        if value is not None:
            return str(value)
    return "metric"


def _safe_metric_value(metric: Any) -> str:
    if isinstance(metric, dict):
        value = metric.get("value")
        if isinstance(value, dict) and "value" in value:
            return str(value["value"])
        if value is not None:
            return str(value)
    return ""


def _safe_issue_title(issue: Any) -> str:
    if isinstance(issue, dict):
        title = issue.get("title")
        if title is not None:
            return str(title)
        description = issue.get("description")
        if description is not None:
            return str(description)
    return "Issue"
