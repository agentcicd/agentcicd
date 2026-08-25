from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from agentcicd.inspection.local import LocalInspectionStore
from agentcicd.ui_server import start_local_inspection_server


def test_local_inspection_store_exposes_project_resources_and_redacts_secrets(tmp_path: Path) -> None:
    project, run_dir = _write_project_with_run(tmp_path)

    store = LocalInspectionStore(project)
    payload = store.project()
    summary = store.run_summary(run_dir.name)
    report = store.report(run_dir.name)

    assert payload["schema_version"] == "inspection-v1"
    assert payload["project"]["name"] == "project"
    assert payload["resources"]["recipes"][0]["id"] == "recipe.sql"
    assert payload["resources"]["fixtures"][0]["name"] == "echo"
    secret_input = next(item for item in payload["resources"]["inputs"] if item["name"] == "api_secret")
    assert secret_input["value_preview"] == "[redacted]"
    assert payload["resources"]["secrets"] == [
        {"reference": "secret.openai", "type": "api_key", "configured": True, "description": None}
    ]
    assert summary["run"]["status"] == "success"
    assert summary["execution_summary"]["completed_stage_count"] == 1
    assert report["metrics"][0]["value"] == 2
    assert "sk-local-secret" not in json.dumps(report)


def test_local_inspection_server_serves_protocol_routes_and_rejects_traversal(tmp_path: Path) -> None:
    project, run_dir = _write_project_with_run(tmp_path)

    with start_local_inspection_server(project) as server:
        viewer_html = _get_text(f"{server.base_url}/runs/{run_dir.name}/")
        asset_path = _viewer_asset_path(viewer_html)
        asset_body = _get_text(f"{server.base_url}{asset_path}")
        project_payload = _get_json(f"{server.base_url}/inspection/v1/projects/{server.store.project_id}")
        progress_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/progress")
        report_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/report")

        assert "AgentCICD inspection" in viewer_html
        assert "import" in asset_body
        assert project_payload["resources"]["runs"][0]["id"] == run_dir.name
        assert progress_payload["completed_steps"] == 1
        assert report_payload["issues"] == []

        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/artifacts/%2E%2E%2Frecipe.sql")
        assert exc_info.value.code == 404


def _write_project_with_run(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text(
        "DECLARE INPUT name STRING;\nDECLARE INPUT api_secret SECRET;\nCREATE BATCH TABLE result SELECT 1 AS value;\n",
        encoding="utf-8",
    )
    (project / "inputs.yaml").write_text("name: checked\napi_secret: secret.openai\n", encoding="utf-8")
    (project / "secrets.yaml").write_text("openai:\n  type: api_key\n  api_key: sk-local-secret\n", encoding="utf-8")
    (project / "fixture_echo.py").write_text("def echo(value):\n    return value\n", encoding="utf-8")
    run_dir = project / ".agentcicd" / "runs" / "run-demo"
    (run_dir / "progress").mkdir(parents=True)
    (run_dir / "reports").mkdir()
    (run_dir / "logs").mkdir()
    (run_dir / "progress" / "progress.jsonl").write_text(
        json.dumps(
            {
                "step_name": "result",
                "step_type": "create_batch_table",
                "status": "completed",
                "started_at": "2026-08-20T00:00:00Z",
                "finished_at": "2026-08-20T00:00:01Z",
                "row_count": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "reports" / "metrics.json").write_text('[{"metric":"quality","value":2}]', encoding="utf-8")
    (run_dir / "reports" / "issues.json").write_text("[]", encoding="utf-8")
    (run_dir / "reports" / "charts.json").write_text("[]", encoding="utf-8")
    (run_dir / "logs" / "engine_execution_report.json").write_text("{}", encoding="utf-8")
    return project, run_dir


def _get_json(url: str) -> dict[str, object]:
    with urlopen(url) as response:  # noqa: S310 - loopback server under test
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> str:
    with urlopen(url) as response:  # noqa: S310 - loopback server under test
        return response.read().decode("utf-8")


def _viewer_asset_path(html: str) -> str:
    marker = 'src="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    path = html[start:end]
    assert path.startswith("/assets/")
    return path
