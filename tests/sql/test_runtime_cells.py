from __future__ import annotations

import pytest

from agentcicd.sql.runtime.cells import clean_cell, errored_cell, merged_cell_errors
from agentcicd.sql.runtime.errors import runtime_error_info


pytestmark = pytest.mark.smoke


def test_error_cell_preserves_null_value_and_errors():
    error = runtime_error_info("AGENTCICD_RUNTIME_HTTP_ERROR", "bad request: detail", "fixture")

    cell = errored_cell([error])

    assert cell["value"] is None
    assert cell["__agentcicd_cell"] is True
    assert cell["metadata"]["errors"] == [error]
    assert cell["metadata"]["latency_ms"] is None


def test_merged_cell_errors():
    first = clean_cell("ok")
    second = errored_cell([{"code": "ERR", "message": "failed", "source": "fixture"}])

    assert merged_cell_errors((first, second)) == [{"code": "ERR", "message": "failed", "source": "fixture"}]


def test_merged_cell_errors_accepts_array_like_empty_errors():
    np = pytest.importorskip("numpy")
    cell = {"value": "ok", "metadata": {"errors": np.array([])}, "__agentcicd_cell": True}

    assert merged_cell_errors((cell,)) == []
