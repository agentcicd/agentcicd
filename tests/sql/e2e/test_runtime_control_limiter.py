from __future__ import annotations

import json
from pathlib import Path

import yaml
import pytest

from .fixtures import read_table_cells
from .harness import ARTIFACT_ROOT, E2EArtifact, run_e2e_artifact


pytestmark = [pytest.mark.e2e, pytest.mark.e2e_smoke, pytest.mark.spark]


def _load_sidecar_rows(tmp_path: Path, table_name: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((tmp_path / "debug" / "tables" / table_name / "rows").glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return rows


def test_runtime_control_limiter_is_stripped_and_debug_rows_are_inspectable(local_spark, tmp_path) -> None:
    artifact_root = ARTIFACT_ROOT / "e2e-22-runtime-control-limiter"
    artifact = E2EArtifact(
        root=artifact_root,
        metadata=json.loads(json.dumps(yaml.safe_load((artifact_root / "recipe.yaml").read_text()))),
    )

    run_e2e_artifact(artifact, spark=local_spark, tmp_path=tmp_path)

    table_rows = read_table_cells(local_spark, tmp_path, "echoed")
    values_by_case = {row["case_id"]["value"]: row["echoed_text"] for row in table_rows}

    assert values_by_case["case-001"]["value"] == "echo:alpha"
    assert values_by_case["case-002"]["value"] == "echo:beta"
    assert values_by_case["case-003"]["value"] == "echo:gamma"
    assert all(cell["metadata"]["errors"] == [] for cell in values_by_case.values())

    stream_rows = _load_sidecar_rows(tmp_path, "echoed")

    assert {row["echoed_text"]["value"] for row in stream_rows} == {"echo:alpha", "echo:beta", "echo:gamma"}
    assert all(row["echoed_text"]["metadata"]["errors"] == [] for row in stream_rows)
