from __future__ import annotations

import pytest

from agentcicd.sql.cell_semantics.errors import runtime_error_info
from agentcicd.sql.cell_semantics.types import clean_cell, errored_cell
from agentcicd.sql.engine.cell_metadata import ERROR_ARRAY_SQL_TYPE


pytestmark = pytest.mark.smoke


def test_error_sql_type_is_canonical():
    assert "recoverable:BOOLEAN" in ERROR_ARRAY_SQL_TYPE


def test_runtime_error_cell_contract_preserves_null_value_field():
    error = runtime_error_info("AGENTCICD_RUNTIME_HTTP_ERROR", "remote 400", "fixture", cause=ValueError("bad"))
    cell = errored_cell([error])

    assert "value" in cell
    assert cell["value"] is None
    assert cell["metadata"]["errors"][0]["cause_code"] == "ValueError"


def test_clean_cell_uses_same_schema_as_error_cell():
    cell = clean_cell("ok")

    assert set(cell) == {"cell_id", "value", "metadata", "__agentcicd_cell"}
    assert cell["metadata"] == {
        "errors": [],
        "latency_ms": None,
        "fixture_trace": None,
    }
