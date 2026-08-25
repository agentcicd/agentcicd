from __future__ import annotations

from agentcicd.fixtures.core.types import FType, JsonEncodedPydanticType
from agentcicd.fixtures.functions.trajectory import (
    StepAdherenceResponse,
    StepAdherenceRowFunction,
    StepAdherenceUdf,
)


def test_step_adherence_udf_metadata() -> None:
    udf = StepAdherenceUdf()

    assert udf.input_args() == ("alignment", "ordering_policy")
    assert len(udf.input_schema()) == 2
    assert isinstance(udf.output_schema(), JsonEncodedPydanticType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), StepAdherenceRowFunction)


def test_step_adherence_passes_in_order_alignment() -> None:
    alignment = {
        "expected_steps": [
            {"id": "e1", "text": "Read file", "required": True},
            {"id": "e2", "text": "Edit file", "required": True},
            {"id": "e3", "text": "Run tests", "required": True},
        ],
        "observed_steps": [
            {"id": "o1", "text": "Opened src/app.py", "matches_expected_ids": ["e1"]},
            {"id": "o2", "text": "Patched src/app.py", "matches_expected_ids": ["e2"]},
            {"id": "o3", "text": "Ran pytest", "matches_expected_ids": ["e3"]},
        ],
    }

    result = StepAdherenceRowFunction().transform(alignment, "in_order")

    assert isinstance(result, StepAdherenceResponse)
    assert result.score == 1.0
    assert result.label == "pass"
    assert result.missing_steps == []
    assert result.out_of_order_steps == []


def test_step_adherence_fails_missing_required_step() -> None:
    alignment = {
        "expected_steps": [
            {"id": "e1", "text": "Read file", "required": True},
            {"id": "e2", "text": "Run tests", "required": True},
        ],
        "observed_steps": [
            {"id": "o1", "text": "Opened src/app.py", "matches_expected_ids": ["e1"]},
        ],
    }

    result = StepAdherenceRowFunction().transform(alignment, "any_order")

    assert result.score == 0.0
    assert result.label == "fail"
    assert result.missing_steps == ["e2"]


def test_step_adherence_fails_out_of_order_for_in_order_policy() -> None:
    alignment = {
        "expected_steps": [
            {"id": "e1", "text": "Read file", "required": True},
            {"id": "e2", "text": "Edit file", "required": True},
        ],
        "observed_steps": [
            {"id": "o1", "text": "Patched src/app.py", "matches_expected_ids": ["e2"]},
            {"id": "o2", "text": "Opened src/app.py", "matches_expected_ids": ["e1"]},
        ],
    }

    result = StepAdherenceRowFunction().transform(alignment, "in_order")

    assert result.score == 0.0
    assert result.out_of_order_steps == ["e2"]


def test_step_adherence_exact_order_fails_extra_step() -> None:
    alignment = {
        "expected_steps": [
            {"id": "e1", "text": "Read file", "required": True},
        ],
        "observed_steps": [
            {"id": "o1", "text": "Opened src/app.py", "matches_expected_ids": ["e1"]},
            {"id": "o2", "text": "Changed unrelated config", "matches_expected_ids": []},
        ],
    }

    result = StepAdherenceRowFunction().transform(alignment, "exact_order")

    assert result.score == 0.0
    assert result.extra_steps == ["o2"]
