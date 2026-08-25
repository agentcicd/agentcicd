from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from .harness import ARTIFACT_ROOT, E2EArtifact, run_e2e_artifact

ROOT = Path(__file__).resolve().parents[4]
for package in ("agentcicd_dp_api", "agentcicd_dp_common", "agentcicd_contacts"):
    package_src = ROOT / package / "src"
    if str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))

pytest.importorskip("fastapi")

from agentcicd_dp_api.config import DpApiSettings
from agentcicd_dp_api.run_artifacts import ResolvedRunArtifacts, RunMetadata
from agentcicd_dp_api.table_readers import TableReadRequest, resolve_jsonl_sidecar_parts, stream_jsonl_page


def test_debug_row_stream_sidecar_can_be_streamed_by_dp_reader(local_spark, tmp_path) -> None:
    artifact_root = ARTIFACT_ROOT / "e2e-21-debug-row-stream-sidecars"
    artifact = E2EArtifact(
        root=artifact_root,
        metadata=json.loads(json.dumps(yaml.safe_load((artifact_root / "recipe.yaml").read_text()))),
    )
    run_e2e_artifact(artifact, spark=local_spark, tmp_path=tmp_path)

    stage_manifest = json.loads((tmp_path / "outputs" / "stage_evaluated.json").read_text(encoding="utf-8"))
    resolved = ResolvedRunArtifacts(
        run=RunMetadata(
            run_id="run.debug-sidecar",
            status="running",
            organization_id="org.test",
            workspace_id="ws.test",
            current_attempt=1,
            cluster_id="cluster.test",
            run_dir=str(tmp_path),
            working_dir=str(tmp_path),
            results_path=str(tmp_path / "tables"),
            payload={},
        ),
        source="live",
        attempt=1,
        live_run_dir=tmp_path,
    )
    request = TableReadRequest(table_name="evaluated", page=1, page_size=2)
    sidecar = resolve_jsonl_sidecar_parts(
        DpApiSettings(),
        resolved,
        request,
        table_metadata_payloads=[stage_manifest],
    )

    iterator = stream_jsonl_page(DpApiSettings(), resolved, sidecar, request)
    first = next(iterator)
    second = next(iterator)
    rows = [json.loads(first), json.loads(second)]

    assert first.endswith(b"\n")
    assert second.endswith(b"\n")
    assert all(isinstance(row, dict) for row in rows)
    assert {row["case_id"]["value"] for row in rows}.issubset({"case-001", "case-002", "case-003"})
    assert all(row["case_id"]["__agentcicd_cell"] is True for row in rows)
    assert all(row["case_id"]["metadata"]["errors"] == [] for row in rows)
    assert sidecar.metadata.total_rows == 3
