from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

import pytest

from agentcicd.inspection.local import LocalInspectionStore
from agentcicd.ui_server import start_local_inspection_server


def test_local_inspection_store_exposes_project_resources_and_redacts_secrets(tmp_path: Path) -> None:
    project, run_dir = _write_project_with_run(tmp_path)

    store = LocalInspectionStore(project)
    payload = store.project()
    summary = store.run_summary(run_dir.name)
    graph = store.graph(run_dir.name)
    report = store.report(run_dir.name)
    logs = store.logs(run_dir.name)

    assert payload["schema_version"] == "inspection-v1"
    assert payload["project"]["name"] == "project"
    assert payload["resources"]["recipes"][0]["id"] == "recipe.sql"
    assert payload["resources"]["fixtures"][0]["name"] == "echo"
    assert len(payload["resources"]["fixtures"]) == 1
    secret_input = next(item for item in payload["resources"]["inputs"] if item["name"] == "api_secret")
    assert secret_input["value_preview"] == "[redacted]"
    assert payload["resources"]["secrets"] == [
        {"reference": "secret.openai", "type": "api_key", "configured": True, "description": None}
    ]
    graph_nodes = {(item["type"], item["label"]) for item in graph["nodes"]}
    graph_node_ids = {item["id"] for item in graph["nodes"]}
    graph_edges = {(item["from_id"], item["to_id"], item["relation"]) for item in graph["edges"]}
    assert ("input", "api_secret") in graph_nodes
    assert ("secret", "secret.openai") in graph_nodes
    assert ("function_reference", "local.echo") in graph_nodes
    assert "table:result" in graph_node_ids
    assert "table:0" not in graph_node_ids
    assert ("input:name", "table:result", "uses_input") in graph_edges
    assert ("secret:openai", "input:api_secret", "provided_to") in graph_edges
    secret_node = next(item for item in graph["nodes"] if item["type"] == "secret")
    assert secret_node["status"] == "available"
    assert any(item["relation"] == "provided_to" for item in graph["edges"])
    assert summary["run"]["status"] == "success"
    assert summary["execution_summary"]["completed_stage_count"] == 1
    assert report["metrics"][0]["value"] == 2
    assert "sk-local-secret" not in json.dumps(report)
    assert "sk-local-secret" not in logs["text"]
    assert any(item["path"] == "logs/run.log" for item in logs["files"])
    assert any(item["path"] == "logs/debug.log" for item in logs["files"])
    assert any(item["path"] == "debug/fixture_traces/trace-local/spans.jsonl" for item in logs["files"])
    assert not any(item["path"] == "logs/engine_execution_report.json" for item in logs["files"])
    assert not any(item["path"] == "logs/engine_plan.json" for item in logs["files"])
    assert "driver debug details" in logs["text"]
    assert "fixture span details" in logs["text"]


def test_local_inspection_server_serves_protocol_routes_and_rejects_traversal(tmp_path: Path) -> None:
    project, run_dir = _write_project_with_run(tmp_path)

    with start_local_inspection_server(project) as server:
        viewer_html = _get_text(f"{server.base_url}/runs/{run_dir.name}/")
        asset_path = _viewer_asset_path(viewer_html)
        asset_body = _get_text(f"{server.base_url}{asset_path}")
        project_payload = _get_json(f"{server.base_url}/inspection/v1/projects/{server.store.project_id}")
        graph_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/graph")
        progress_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/progress")
        logs_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/logs")
        report_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/report")
        public_runs_payload = _get_json(f"{server.base_url}/runs")
        public_run_payload = _get_json(f"{server.base_url}/runs/{run_dir.name}")
        public_progress_payload = _get_json(f"{server.base_url}/runs/{run_dir.name}/progress")
        public_recipes_payload = _get_json(f"{server.base_url}/recipes")
        recipe_segments_payload = _get_json(f"{server.base_url}/recipes/recipe.sql/segments")
        recipe_analysis_payload = _post_json(
            f"{server.base_url}/recipes/analysis",
            {"organization_id": server.store.project_id, "source_text": (project / "recipe.sql").read_text(encoding="utf-8")},
        )

        assert "AgentCICD inspection" in viewer_html
        assert "import" in asset_body
        assert project_payload["schema_version"] == "inspection-v1"
        assert graph_payload["schema_version"] == "inspection-v1"
        assert project_payload["resources"]["runs"][0]["id"] == run_dir.name
        assert graph_payload["nodes"]
        assert {"from_id": "input:name", "to_id": "table:result", "relation": "uses_input"} in graph_payload["edges"]
        assert progress_payload["completed_steps"] == 1
        assert any(item["path"] == "logs/run.log" for item in logs_payload["files"])
        assert any(item["path"] == "logs/debug.log" for item in logs_payload["files"])
        assert any(item["path"] == "debug/fixture_traces/trace-local/spans.jsonl" for item in logs_payload["files"])
        assert not any(item["path"] == "logs/engine_execution_report.json" for item in logs_payload["files"])
        assert not any(item["path"] == "logs/engine_plan.json" for item in logs_payload["files"])
        assert "driver debug details" in logs_payload["text"]
        assert "fixture span details" in logs_payload["text"]
        assert "sk-local-secret" not in logs_payload["text"]
        assert report_payload["issues"] == []
        assert public_runs_payload[0]["id"] == run_dir.name
        assert "schema_version" not in public_runs_payload[0]
        assert public_run_payload["payload"]["source"] == "local"
        assert public_run_payload["recipe_id"] == "recipe.sql"
        assert public_run_payload["aisystem_environment_bindings"] == {}
        assert "schema_version" not in public_progress_payload
        assert public_progress_payload["steps"][0]["created_at"]
        assert public_recipes_payload["items"][0]["id"] == "recipe.sql"
        assert "schema_version" not in public_recipes_payload
        assert any(item["id"] == "table:result" for item in recipe_segments_payload["nodes"])
        assert recipe_segments_payload["schema_version"] == "recipe_segmentation.v1"
        assert {"from": "input:name", "to": "table:result", "relation": "uses_input"} in recipe_analysis_payload["graph"]
        assert recipe_analysis_payload["schema_version"] == "recipe_analysis.v2"

        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/artifacts/%2E%2E%2Frecipe.sql")
        assert exc_info.value.code == 404


def test_local_inspection_annotation_api_uses_request_task_review_dtos(tmp_path: Path) -> None:
    project, run_dir = _write_project_with_run(tmp_path)
    request_root = _write_annotation_request(run_dir)

    store = LocalInspectionStore(project)
    requests_payload = store.annotation_requests(run_dir.name)
    tasks_payload = store.annotation_tasks(run_dir.name, request_root.name)

    assert requests_payload["items"][0]["id"] == "annreq.localtest"
    assert requests_payload["items"][0]["local_project_id"] == store.project_id
    assert requests_payload["items"][0]["source_table"] == "judged"
    assert tasks_payload["tasks"][0]["task_id"] == "task_000000"
    assert tasks_payload["tasks"][0]["status"] == "unlabeled"

    review_payload = store.submit_annotation_review(
        run_dir.name,
        request_root.name,
        "task_000000",
        {"reviewer_id": "reviewer.local", "result": {"label": "pass"}},
    )
    assert review_payload["review"]["task_id"] == "task_000000"
    assert review_payload["progress"]["completed_tasks"] == 1

    final_payload = store.finalize_annotation_request(run_dir.name, request_root.name)
    assert final_payload["request_id"] == "annreq.localtest"
    assert final_payload["completed_tasks"] == 1
    result_line = json.loads((request_root / "results.jsonl").read_text(encoding="utf-8").strip())
    assert result_line == {
        "task_id": "task_000000",
        "data": {"case_id": "case-1", "answer": "hello"},
        "result": {"label": "pass"},
        "reviews": [review_payload["review"]],
    }


def test_local_inspection_table_rows_hide_internal_row_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, run_dir = _write_project_with_run(tmp_path)
    table_dir = run_dir / "tables" / "result"
    table_dir.mkdir(parents=True)
    store = LocalInspectionStore(project)

    monkeypatch.setattr(
        store,
        "_read_parquet_rows",
        lambda _path, *, offset, limit: [
            {"__agentcicd_row_id": "row-1", "value": "ok"},
        ],
    )

    payload = store.table_rows(run_dir.name, "result", page=1, page_size=25)

    assert payload["columns"] == ["value"]
    assert payload["rows"] == [{"value": "ok"}]


def test_local_inspection_graph_synthesizes_publish_source_table(tmp_path: Path) -> None:
    project = tmp_path / "annotation_project"
    project.mkdir()
    (project / "recipe.sql").write_text(
        "DECLARE INPUT input_text STRING DEFAULT \"hello\";\n"
        "CREATE TABLE cases AS SELECT \"Is this answer grounded?\" AS prompt;\n"
        "PUBLISH cases TO ANNOTATION QUEUE review_queue AS review_queue;\n",
        encoding="utf-8",
    )
    run_dir = project / ".agentcicd" / "runs" / "run-annotated"
    (run_dir / "progress").mkdir(parents=True)
    (run_dir / "reports").mkdir()
    (run_dir / "logs").mkdir()

    graph = LocalInspectionStore(project).graph(run_dir.name)
    graph_nodes = {(item["type"], item["label"]) for item in graph["nodes"]}
    graph_edges = {(item["from_id"], item["to_id"], item["relation"]) for item in graph["edges"]}

    assert ("table", "cases") in graph_nodes
    assert ("publish_annotation", "cases") in graph_nodes
    assert ("table:cases", "publish:review_queue:annotation", "publish_annotation") in graph_edges


def test_local_inspection_server_annotation_post_routes(tmp_path: Path) -> None:
    project, run_dir = _write_project_with_run(tmp_path)
    _write_annotation_request(run_dir)

    with start_local_inspection_server(project) as server:
        tasks_payload = _get_json(f"{server.base_url}/inspection/v1/runs/{run_dir.name}/annotations/requests/annreq.localtest/tasks")
        assert tasks_payload["tasks"][0]["task_id"] == "task_000000"

        review_payload = _post_json(
            f"{server.base_url}/inspection/v1/runs/{run_dir.name}/annotations/requests/annreq.localtest/tasks/task_000000/reviews",
            {"reviewer_id": "reviewer.local", "result": {"label": "pass"}},
        )
        assert review_payload["review"]["result"] == {"label": "pass"}

        final_payload = _post_json(
            f"{server.base_url}/inspection/v1/runs/{run_dir.name}/annotations/requests/annreq.localtest/finalize",
            {},
        )
        assert final_payload["results_path"].endswith("/results.jsonl")


def _write_project_with_run(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "recipe.sql").write_text(
        "DECLARE INPUT name STRING;\nDECLARE INPUT api_secret SECRET;\nCREATE BATCH TABLE result AS SELECT local.echo(name) AS value, api_secret AS secret_ref;\n",
        encoding="utf-8",
    )
    (project / "inputs.yaml").write_text("name: checked\napi_secret: secret.openai\n", encoding="utf-8")
    (project / "secrets.yaml").write_text("openai:\n  type: api_key\n  api_key: sk-local-secret\nunused:\n  type: api_key\n  api_key: sk-unused-secret\n", encoding="utf-8")
    (project / "fixture_echo.py").write_text("def echo(value):\n    return value\n", encoding="utf-8")
    (project / "fixture_unused.py").write_text("def unused(value):\n    return value\n", encoding="utf-8")
    run_dir = project / ".agentcicd" / "runs" / "run-demo"
    (run_dir / "progress").mkdir(parents=True)
    (run_dir / "reports").mkdir()
    (run_dir / "logs").mkdir()
    (run_dir / "debug" / "fixture_traces" / "trace-local").mkdir(parents=True)
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
    (run_dir / "logs" / "run.log").write_text("using sk-local-secret for local smoke\n", encoding="utf-8")
    (run_dir / "logs" / "debug.log").write_text("driver debug details\n", encoding="utf-8")
    (run_dir / "logs" / "engine_execution_report.json").write_text("{}", encoding="utf-8")
    (run_dir / "logs" / "engine_plan.json").write_text("{}", encoding="utf-8")
    (run_dir / "debug" / "fixture_traces" / "trace-local" / "spans.jsonl").write_text(
        '{"name":"fixture span details","secret":"sk-local-secret"}\n',
        encoding="utf-8",
    )
    return project, run_dir


def _write_annotation_request(run_dir: Path) -> Path:
    request_root = run_dir / "annotation_tasks" / "annreq.localtest"
    reviews_root = request_root / "reviews"
    reviews_root.mkdir(parents=True)
    (request_root / "tasks.jsonl").write_text(
        json.dumps({"task_id": "task_000000", "data": {"case_id": "case-1", "answer": "hello"}}) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "queue_name": "policy_review",
        "source_table": "judged",
        "publish_alias": "annotation_review",
        "instructions": "Review policy adherence.",
        "template": "<View><Text name='answer' value='$answer'/></View>",
        "review_policy": {"reviewers_per_task": 1, "reservation_minutes": 30, "consensus": "none"},
        "data_path": (request_root / "tasks.jsonl").as_posix(),
        "reviews_path": reviews_root.as_posix(),
        "results_path": (request_root / "results.jsonl").as_posix(),
        "manifest_path": (request_root / "manifest.json").as_posix(),
    }
    (request_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    alias_root = run_dir / "annotation_tasks" / "annotation_review"
    alias_root.mkdir()
    (alias_root / "request.json").write_text(json.dumps({"request_id": request_root.name}), encoding="utf-8")
    return request_root


def _get_json(url: str) -> dict[str, object]:
    with urlopen(url) as response:  # noqa: S310 - loopback server under test
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> str:
    with urlopen(url) as response:  # noqa: S310 - loopback server under test
        return response.read().decode("utf-8")


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:  # noqa: S310 - loopback server under test
        return json.loads(response.read().decode("utf-8"))


def _viewer_asset_path(html: str) -> str:
    marker = 'src="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    path = html[start:end]
    assert path.startswith("/assets/")
    return path
