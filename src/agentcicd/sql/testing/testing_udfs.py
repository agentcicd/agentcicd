import json
from typing import Any

from agentcicd.sql.runtime.udf_compat.function import BatchFunction
from agentcicd.sql.runtime.udf_compat.types import FType, StringType
from agentcicd.sql.runtime.udf_compat.udf import Udf


def mock_agent_runcode(
    fixture_id: str,
    prompt: Any,
    init_script: Any,
    model: str,
    secret_id: str,
    options: dict[str, str],
) -> str:
    def _extract_value(v: Any) -> str:
        if hasattr(v, "asDict"):
            v = v.asDict(recursive=True)
        if isinstance(v, dict) and "value" in v:
            return str(v.get("value") or "")
        return str(v or "")

    prompt_value = _extract_value(prompt)
    init_script_value = _extract_value(init_script)

    # Keep output schema aligned with real downstream parsing in benchmark SQL.
    payload = {
        "status": "completed",
        "final_answer": f"ok:{prompt_value[:32]}",
        "fixture_id": fixture_id,
        "model": model,
        "secret_id": secret_id,
        "init_script": init_script_value[:32],
        "max_steps": options.get("max_steps"),
        "stop_on_nonempty_stdout": options.get("stop_on_nonempty_stdout"),
    }
    return json.dumps(payload)


def mock_llm_cell(cell: Any) -> dict[str, Any]:
    if hasattr(cell, "asDict"):
        cell = cell.asDict(recursive=True)
    if not isinstance(cell, dict):
        cell = {"value": cell, "metadata": {}}
    metadata = cell.get("metadata") or {}
    return {
        "value": f"llm:{cell.get('value')}",
        "metadata": {
            "error": metadata.get("error"),
            "subdatatype": None,
        },
        "__agentcicd_cell": True,
    }


class MockLlmBatchFunction(BatchFunction):
    def input_schema(self):
        return (StringType(),)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def transform(self, values):
        return [mock_llm_cell({"value": value, "metadata": {}})["value"] for value in values]


class MockLlmPythonUdf(Udf, name="mock.llm_call"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("text",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return MockLlmBatchFunction
