import json
from pathlib import Path

from agentcicd.sql.engine.progress_reporter import ProgressReporter


def _read_events(progress_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in progress_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_progress_reporter_emits_failure_metadata(tmp_path):
    progress_path = tmp_path / "progress.jsonl"
    reporter = ProgressReporter(progress_path)

    reporter.emit("table", "predictions", "started", None)
    reporter.emit(
        "table",
        "predictions",
        "failed",
        "AnalysisException: column `foo` not found",
        {
            "error_category": "sql_analysis_error",
            "error_type": "AnalysisException",
            "error_summary": "AnalysisException: column `foo` not found",
            "error_traceback": "Traceback excerpt",
            "debug_log_path": "/tmp/debug.log",
        },
    )

    events = _read_events(progress_path)

    assert len(events) == 2
    assert events[1]["status"] == "failed"
    assert events[1]["error"] == "AnalysisException: column `foo` not found"
    assert events[1]["error_category"] == "sql_analysis_error"
    assert events[1]["error_type"] == "AnalysisException"
    assert events[1]["error_summary"] == "AnalysisException: column `foo` not found"
    assert events[1]["error_traceback"] == "Traceback excerpt"
    assert events[1]["debug_log_path"] == "/tmp/debug.log"
    assert "duration_ms" in events[1]
