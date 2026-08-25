from __future__ import annotations

from pathlib import Path

import pytest

from .expected_artifact_schema import validate_expected_artifacts


pytestmark = pytest.mark.smoke


def test_e2e_expected_artifacts_have_valid_reviewable_shapes():
    root = Path(__file__).resolve().parent / "artifacts"

    assert validate_expected_artifacts(root) == []
