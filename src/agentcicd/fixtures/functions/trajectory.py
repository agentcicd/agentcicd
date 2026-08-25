from __future__ import annotations

from typing import Any, Callable, Literal, Tuple

from pydantic import Field, ValidationError

from agentcicd.fixtures.core.function import Function, RowFunction
from agentcicd.fixtures.core.types import DType, FType, JsonEncodedPydanticType, JsonType, AgentCICDModel, StringType
from agentcicd.fixtures.core.udf import Udf


class ExpectedStep(AgentCICDModel):
    id: str
    text: str
    required: bool = True


class ObservedStep(AgentCICDModel):
    id: str
    text: str
    matches_expected_ids: list[str] = Field(default_factory=list)


class StepAlignment(AgentCICDModel):
    expected_steps: list[ExpectedStep] = Field(default_factory=list)
    observed_steps: list[ObservedStep] = Field(default_factory=list)


class StepAdherenceResponse(AgentCICDModel):
    score: float
    label: Literal["pass", "review", "fail"]
    ordering_policy: str
    matched_steps: list[str] = Field(default_factory=list)
    missing_steps: list[str] = Field(default_factory=list)
    out_of_order_steps: list[str] = Field(default_factory=list)
    extra_steps: list[str] = Field(default_factory=list)
    reason: str


def _as_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _validate_alignment(alignment: object) -> StepAlignment:
    if isinstance(alignment, StepAlignment):
        return alignment
    try:
        return StepAlignment.model_validate(alignment)
    except ValidationError as exc:
        raise ValueError(f"Invalid step alignment: {exc}") from exc


def _normalized_policy(ordering_policy: object) -> str:
    policy = str(ordering_policy or "in_order").strip().lower()
    if policy not in {"in_order", "exact_order", "any_order", "prefix"}:
        return "in_order"
    return policy


def _first_match_positions(alignment: StepAlignment) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, observed in enumerate(alignment.observed_steps):
        for expected_id in observed.matches_expected_ids:
            positions.setdefault(str(expected_id), index)
    return positions


def _matched_expected_ids(alignment: StepAlignment) -> set[str]:
    expected_ids = {step.id for step in alignment.expected_steps}
    matched: set[str] = set()
    for observed in alignment.observed_steps:
        for expected_id in observed.matches_expected_ids:
            if expected_id in expected_ids:
                matched.add(expected_id)
    return matched


def _extra_observed_steps(alignment: StepAlignment) -> list[str]:
    expected_ids = {step.id for step in alignment.expected_steps}
    extra: list[str] = []
    for observed in alignment.observed_steps:
        if not any(expected_id in expected_ids for expected_id in observed.matches_expected_ids):
            extra.append(observed.id)
    return extra


def _out_of_order_expected_ids(alignment: StepAlignment, positions: dict[str, int]) -> list[str]:
    out_of_order: list[str] = []
    last_position = -1
    for expected in [step for step in alignment.expected_steps if step.required]:
        position = positions.get(expected.id)
        if position is None:
            continue
        if position < last_position:
            out_of_order.append(expected.id)
        else:
            last_position = position
    return out_of_order


def _prefix_missing(alignment: StepAlignment, positions: dict[str, int]) -> list[str]:
    missing: list[str] = []
    for expected in [step for step in alignment.expected_steps if step.required]:
        if expected.id in positions:
            continue
        missing.append(expected.id)
        break
    return missing


class StepAdherenceRowFunction(RowFunction):
    """Deterministically score normalized step alignment under an ordering policy."""

    def transform(
        self,
        alignment: object,
        ordering_policy: object,
    ) -> StepAdherenceResponse:
        parsed = _validate_alignment(_as_mapping(alignment))
        policy = _normalized_policy(ordering_policy)

        required_expected = [step for step in parsed.expected_steps if step.required]
        required_ids = {step.id for step in required_expected}
        matched_ids = _matched_expected_ids(parsed)
        positions = _first_match_positions(parsed)

        if policy == "prefix":
            missing = _prefix_missing(parsed, positions)
        else:
            missing = [step.id for step in required_expected if step.id not in matched_ids]

        out_of_order = [] if policy == "any_order" else _out_of_order_expected_ids(parsed, positions)
        extra = _extra_observed_steps(parsed)

        exact_order_extra_failure = policy == "exact_order" and bool(extra)
        passed = not missing and not out_of_order and not exact_order_extra_failure

        if policy == "prefix":
            passed = not out_of_order and not missing

        score = 1.0 if passed else 0.0
        label: Literal["pass", "review", "fail"] = "pass" if passed else "fail"

        if not required_ids:
            score = 0.0
            label = "review"
            reason = "No required expected steps were provided for deterministic adherence checking."
        elif passed:
            reason = f"Observed steps satisfy the {policy} expected-step policy."
        else:
            reason = f"Observed steps do not satisfy the {policy} expected-step policy."

        return StepAdherenceResponse(
            score=score,
            label=label,
            ordering_policy=policy,
            matched_steps=sorted(matched_ids & required_ids),
            missing_steps=missing,
            out_of_order_steps=out_of_order,
            extra_steps=extra,
            reason=reason,
        )


class StepAdherenceUdf(Udf, name="agent.trajectory.step_adherence"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (JsonType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("alignment", "ordering_policy")

    def output_schema(self) -> DType:
        return JsonEncodedPydanticType(StepAdherenceResponse)

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return StepAdherenceRowFunction()
