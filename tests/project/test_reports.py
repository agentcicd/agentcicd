from __future__ import annotations

import json
from pathlib import Path

from agentcicd.reports import render_local_report


def test_render_local_report_redacts_secret_values(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    reports_dir = run_dir / "reports"
    progress_dir = run_dir / "progress"
    reports_dir.mkdir(parents=True)
    progress_dir.mkdir()
    (reports_dir / "issues.json").write_text(
        json.dumps([{"title": "leaked sk-test-value", "api_key": "sk-test-value"}]),
        encoding="utf-8",
    )
    (reports_dir / "metrics.json").write_text("[]", encoding="utf-8")
    (reports_dir / "charts.json").write_text("[]", encoding="utf-8")
    (progress_dir / "progress.jsonl").write_text('{"message":"sk-test-value"}\n', encoding="utf-8")

    summary = render_local_report(run_dir, secret_values=("sk-test-value",))

    assert summary.issues_count == 1
    assert "sk-test-value" not in (reports_dir / "issues.json").read_text(encoding="utf-8")
    assert "sk-test-value" not in (reports_dir / "report.md").read_text(encoding="utf-8")
    assert "sk-test-value" not in (progress_dir / "progress.jsonl").read_text(encoding="utf-8")
    issue_payload = json.loads((reports_dir / "issues.json").read_text(encoding="utf-8"))
    assert issue_payload[0]["api_key"] == "[redacted]"
