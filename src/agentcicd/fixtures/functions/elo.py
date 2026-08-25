from __future__ import annotations

from typing import Callable, Dict, Tuple

from agentcicd.fixtures.core.function import Function, RowFunction
from agentcicd.fixtures.core.types import ArrayType, DType, FType, FloatType, StringType
from agentcicd.fixtures.core.udf import Udf

"""Elo rating UDFs used by SQL ranking pipelines.

This module registers three Python UDFs:

- ``ranking.elo_expected_score(rating_a, rating_b)``
- ``ranking.elo_rating_delta(rating_a, rating_b, outcome_a, k_factor)``
- ``ranking.elo_final_rating(target_model, model_a_seq, model_b_seq,
  outcome_a_seq, k_factor, initial_rating)``

The main use case is the Chatbot Arena-style SQL benchmark, which:
1. Produces pairwise battle outcomes.
2. Encodes each battle as model A, model B, and outcome for A.
3. Replays the ordered battle sequence to compute a final Elo rating per model.

Example with ``k_factor=32`` and ``initial_rating=1500``:

- Battle 1: ``gpt-x`` vs ``baseline``, outcome for A = ``1.0``
- Battle 2: ``gpt-x`` vs ``baseline``, outcome for A = ``0.5``

Step 1:
- Both start at 1500.
- Expected score for A is 0.5.
- Delta for A is ``32 * (1.0 - 0.5) = 16``.
- Ratings become ``gpt-x=1516``, ``baseline=1484``.

Step 2:
- Expected score for ``gpt-x`` is about 0.546.
- Delta for A is ``32 * (0.5 - 0.546) ~= -1.47``.
- Final ratings become ``gpt-x~=1514.53``, ``baseline~=1485.47``.

``elo_final_rating("gpt-x", ...)`` therefore returns about ``1514.53`` for this
ordered sequence. Because Elo updates are applied iteratively, the result is
order-dependent if the battle order changes.
"""


def _to_float(value: object, default: float) -> float:
    """Convert arbitrary input to float, falling back to ``default``."""
    if value is None:
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class EloExpectedScoreRowFunction(RowFunction):
    """Return the expected Elo score for player/model A against B."""

    def transform(self, rating_a: object, rating_b: object) -> float:
        a = _to_float(rating_a, 1500.0)
        b = _to_float(rating_b, 1500.0)
        exponent = (b - a) / 400.0
        return float(1.0 / (1.0 + (10.0 ** exponent)))


class EloExpectedScoreUdf(Udf, name="ranking.elo_expected_score"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (FloatType(), FloatType())

    def input_args(self) -> Tuple[str, ...]:
        return ("rating_a", "rating_b")

    def output_schema(self) -> DType:
        return FloatType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return EloExpectedScoreRowFunction()


class EloRatingDeltaRowFunction(RowFunction):
    """Return A's Elo rating delta for a single match outcome."""

    def transform(
        self,
        rating_a: object,
        rating_b: object,
        outcome_a: object,
        k_factor: object,
    ) -> float:
        a = _to_float(rating_a, 1500.0)
        b = _to_float(rating_b, 1500.0)
        outcome = _to_float(outcome_a, 0.5)
        k = _to_float(k_factor, 32.0)
        expected_a = 1.0 / (1.0 + (10.0 ** ((b - a) / 400.0)))
        return float(k * (outcome - expected_a))


class EloRatingDeltaUdf(Udf, name="ranking.elo_rating_delta"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (FloatType(), FloatType(), FloatType(), FloatType())

    def input_args(self) -> Tuple[str, ...]:
        return ("rating_a", "rating_b", "outcome_a", "k_factor")

    def output_schema(self) -> DType:
        return FloatType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return EloRatingDeltaRowFunction()


class EloFinalRatingRowFunction(RowFunction):
    """Replay an ordered battle sequence and return one model's final Elo."""

    def transform(
        self,
        target_model: object,
        model_a_seq: object,
        model_b_seq: object,
        outcome_a_seq: object,
        k_factor: object,
        initial_rating: object,
    ) -> float:
        target = "" if target_model is None else str(target_model)
        if not target:
            return 1500.0

        seq_a = model_a_seq if isinstance(model_a_seq, list) else []
        seq_b = model_b_seq if isinstance(model_b_seq, list) else []
        seq_outcome = outcome_a_seq if isinstance(outcome_a_seq, list) else []

        k = _to_float(k_factor, 32.0)
        default_rating = _to_float(initial_rating, 1500.0)
        ratings: Dict[str, float] = {}

        steps = min(len(seq_a), len(seq_b), len(seq_outcome))
        for idx in range(steps):
            a = str(seq_a[idx]) if seq_a[idx] is not None else ""
            b = str(seq_b[idx]) if seq_b[idx] is not None else ""
            if not a or not b:
                continue

            ra = ratings.get(a, default_rating)
            rb = ratings.get(b, default_rating)
            outcome_a = _to_float(seq_outcome[idx], 0.5)

            expected_a = 1.0 / (1.0 + (10.0 ** ((rb - ra) / 400.0)))
            expected_b = 1.0 - expected_a
            outcome_b = 1.0 - outcome_a

            ratings[a] = ra + k * (outcome_a - expected_a)
            ratings[b] = rb + k * (outcome_b - expected_b)

        return float(ratings.get(target, default_rating))


class EloFinalRatingUdf(Udf, name="ranking.elo_final_rating"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), ArrayType(), ArrayType(), ArrayType(), FloatType(), FloatType())

    def input_args(self) -> Tuple[str, ...]:
        return (
            "target_model",
            "model_a_seq",
            "model_b_seq",
            "outcome_a_seq",
            "k_factor",
            "initial_rating",
        )

    def output_schema(self) -> DType:
        return FloatType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return EloFinalRatingRowFunction()
