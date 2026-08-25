from __future__ import annotations

from agentcicd.fixtures.core.types import FType, JsonEncodedPydanticType
from agentcicd.fixtures.functions.ragas import (
    RagasAgentGoalAccuracyRowFunction,
    RagasAgentGoalAccuracyUdf,
    RagasAspectCriticRowFunction,
    RagasAspectCriticUdf,
    RagasContextEntityRecallRowFunction,
    RagasContextEntitiesRecallUdf,
    RagasContextPrecisionRowFunction,
    RagasContextPrecisionUdf,
    RagasContextRecallRowFunction,
    RagasContextRecallUdf,
    RagasExecutionBasedDatacompyScoreRowFunction,
    RagasExecutionBasedDatacompyScoreUdf,
    RagasFaithfulnessRowFunction,
    RagasFaithfulnessUdf,
    RagasInstanceSpecificRubricsScoringRowFunction,
    RagasInstanceSpecificRubricsScoringUdf,
    RagasMultimodalFaithfulnessRowFunction,
    RagasMultimodalFaithfulnessUdf,
    RagasMultimodalRelevanceRowFunction,
    RagasMultimodalRelevanceUdf,
    RagasNoiseSensitivityRowFunction,
    RagasNoiseSensitivityUdf,
    RagasResponseRelevancyRowFunction,
    RagasResponseRelevancyUdf,
    RagasRubricsBasedScoringRowFunction,
    RagasRubricsBasedScoringUdf,
    RagasSQLQueryEquivalenceRowFunction,
    RagasSQLQueryEquivalenceUdf,
    RagasSimpleCriteriaScoringRowFunction,
    RagasSimpleCriteriaScoringUdf,
    RagasSummarizationRowFunction,
    RagasSummarizationUdf,
    RagasToolCallAccuracyRowFunction,
    RagasToolCallAccuracyUdf,
    RagasToolCallF1RowFunction,
    RagasToolCallF1Udf,
    RagasTopicAdherenceRowFunction,
    RagasTopicAdherenceUdf,
)


def _assert_udf_metadata(
    udf: object,
    row_function_type: type[object],
    expected_args: tuple[str, ...],
) -> None:
    assert len(udf.input_schema()) == len(expected_args)
    assert udf.input_args() == expected_args
    assert isinstance(udf.output_schema(), JsonEncodedPydanticType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), row_function_type)


def test_ragas_context_precision_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasContextPrecisionUdf(),
        RagasContextPrecisionRowFunction,
        RagasContextPrecisionUdf().input_args(),
    )


def test_ragas_context_recall_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasContextRecallUdf(),
        RagasContextRecallRowFunction,
        RagasContextRecallUdf().input_args(),
    )


def test_ragas_context_entities_recall_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasContextEntitiesRecallUdf(),
        RagasContextEntityRecallRowFunction,
        RagasContextEntitiesRecallUdf().input_args(),
    )


def test_ragas_noise_sensitivity_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasNoiseSensitivityUdf(),
        RagasNoiseSensitivityRowFunction,
        RagasNoiseSensitivityUdf().input_args(),
    )


def test_ragas_response_relevancy_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasResponseRelevancyUdf(),
        RagasResponseRelevancyRowFunction,
        RagasResponseRelevancyUdf().input_args(),
    )


def test_ragas_faithfulness_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasFaithfulnessUdf(),
        RagasFaithfulnessRowFunction,
        RagasFaithfulnessUdf().input_args(),
    )


def test_ragas_multimodal_faithfulness_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasMultimodalFaithfulnessUdf(),
        RagasMultimodalFaithfulnessRowFunction,
        RagasMultimodalFaithfulnessUdf().input_args(),
    )


def test_ragas_multimodal_relevance_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasMultimodalRelevanceUdf(),
        RagasMultimodalRelevanceRowFunction,
        RagasMultimodalRelevanceUdf().input_args(),
    )


def test_ragas_topic_adherence_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasTopicAdherenceUdf(),
        RagasTopicAdherenceRowFunction,
        RagasTopicAdherenceUdf().input_args(),
    )


def test_ragas_tool_call_accuracy_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasToolCallAccuracyUdf(),
        RagasToolCallAccuracyRowFunction,
        RagasToolCallAccuracyUdf().input_args(),
    )


def test_ragas_tool_call_f1_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasToolCallF1Udf(),
        RagasToolCallF1RowFunction,
        RagasToolCallF1Udf().input_args(),
    )


def test_ragas_agent_goal_accuracy_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasAgentGoalAccuracyUdf(),
        RagasAgentGoalAccuracyRowFunction,
        RagasAgentGoalAccuracyUdf().input_args(),
    )


def test_ragas_aspect_critic_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasAspectCriticUdf(),
        RagasAspectCriticRowFunction,
        RagasAspectCriticUdf().input_args(),
    )


def test_ragas_simple_criteria_scoring_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasSimpleCriteriaScoringUdf(),
        RagasSimpleCriteriaScoringRowFunction,
        RagasSimpleCriteriaScoringUdf().input_args(),
    )


def test_ragas_rubrics_based_scoring_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasRubricsBasedScoringUdf(),
        RagasRubricsBasedScoringRowFunction,
        RagasRubricsBasedScoringUdf().input_args(),
    )


def test_ragas_instance_specific_rubrics_scoring_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasInstanceSpecificRubricsScoringUdf(),
        RagasInstanceSpecificRubricsScoringRowFunction,
        RagasInstanceSpecificRubricsScoringUdf().input_args(),
    )


def test_ragas_summarization_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasSummarizationUdf(),
        RagasSummarizationRowFunction,
        RagasSummarizationUdf().input_args(),
    )


def test_ragas_execution_based_datacompy_score_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasExecutionBasedDatacompyScoreUdf(),
        RagasExecutionBasedDatacompyScoreRowFunction,
        RagasExecutionBasedDatacompyScoreUdf().input_args(),
    )


def test_ragas_sql_query_equivalence_udf_metadata() -> None:
    _assert_udf_metadata(
        RagasSQLQueryEquivalenceUdf(),
        RagasSQLQueryEquivalenceRowFunction,
        RagasSQLQueryEquivalenceUdf().input_args(),
    )
