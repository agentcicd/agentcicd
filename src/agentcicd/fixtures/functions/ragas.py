from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import Field

from agentcicd.fixtures._attrs import read_attr
from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import (
    BooleanType,
    DType,
    FType,
    FloatType,
    IntType,
    JsonEncodedPydanticType,
    JsonType,
    AgentCICDModel,
    StringType,
)
from agentcicd.fixtures.core.udf import Param, Udf

from .utils.runtime_context import (
    merge_litellm_payload_with_secret,
    resolve_litellm_payload_from_aisystem,
)

"""Ragas-backed metric UDFs resolved from AI system bindings plus parameter maps."""


class RagasMetricResponse(AgentCICDModel):
    """Normalized score payload returned by all Ragas UDFs."""

    value: Optional[float] = None
    reason: Optional[str] = None


class RagasLLMConfig(AgentCICDModel):
    """Resolved LLM configuration after explicit args and secret wiring."""

    model: str
    provider: str = "openai"
    adapter: str = "auto"
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    api_version: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    model_kwargs: Dict[str, Any] = Field(default_factory=dict)


class RagasEmbeddingConfig(AgentCICDModel):
    """Resolved embedding configuration after explicit args and secret wiring."""

    model: str
    provider: str = "openai"
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    api_version: Optional[str] = None
    embedding_kwargs: Dict[str, Any] = Field(default_factory=dict)


class RagasToolCall(AgentCICDModel):
    """Serializable tool call payload for agent/tool metrics."""

    name: str
    args: Dict[str, Any] = Field(default_factory=dict)


class RagasMessage(AgentCICDModel):
    """Serializable message payload for multi-turn metrics."""

    type: Literal["human", "ai", "tool"]
    content: str
    metadata: Optional[Dict[str, Any]] = None
    tool_calls: Optional[List[RagasToolCall]] = None


AISYSTEM_ARG_NAMES: Tuple[str, ...] = (
    "aisystem_id",
    "aisystem_parameters",
)

AISYSTEM_SCHEMA: Tuple[DType, ...] = (
    StringType(),
    JsonType(),
)


def _clean_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _json_map(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return list(parsed)
    return []


def _str_list(value: object) -> list[str]:
    return [str(item) for item in _json_list(value) if item is not None]


def _string_map(value: object) -> dict[str, str]:
    return {
        str(key): str(item)
        for key, item in _json_map(value).items()
        if key is not None and item is not None
    }


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bool_value(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _float_value(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _metric_response(result: Any) -> RagasMetricResponse:
    value = read_attr(result, "value", result)
    if isinstance(value, bool):
        value = float(value)
    elif isinstance(value, (int, float)):
        value = float(value)
    else:
        value = None
    return RagasMetricResponse(value=value, reason=read_attr(result, "reason", None))


def _build_llm_kwargs(config: RagasLLMConfig) -> Dict[str, Any]:
    kwargs = dict(config.model_kwargs)
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.system_prompt is not None:
        kwargs["system_prompt"] = config.system_prompt
    return kwargs


def _resolve_llm_config(
    aisystem_id: object,
    aisystem_parameters: object,
) -> RagasLLMConfig:
    payload = _json_map(aisystem_parameters)
    resolved_aisystem_id = _clean_str(aisystem_id)
    override_secret_id = _clean_str(payload.get("secret_id"))

    if resolved_aisystem_id is not None:
        resolved = resolve_litellm_payload_from_aisystem(
            aisystem_id=resolved_aisystem_id,
            expected_interface_type="llm.responses",
            secret_id_or_key=override_secret_id,
            options=payload,
        )
        payload.setdefault("model", resolved["model"])
        payload.setdefault("secret_id", resolved["secret_id"])

    resolved_model = _clean_str(payload.get("model"))
    if resolved_model is None:
        raise ValueError("model is required")

    payload["model"] = resolved_model
    if payload.get("temperature") is not None:
        payload["temperature"] = _float_value(payload.get("temperature"))
    if payload.get("top_p") is not None:
        payload["top_p"] = _float_value(payload.get("top_p"))
    if payload.get("max_tokens") is not None:
        payload["max_tokens"] = _int_value(payload.get("max_tokens"), 0)

    payload = merge_litellm_payload_with_secret(payload, _clean_str(payload.get("secret_id")), payload)
    extra_kwargs = _json_map(payload.pop("model_kwargs", {}))

    return RagasLLMConfig(
        model=str(payload.pop("model")),
        provider=str(payload.pop("provider", "openai")),
        adapter=str(payload.pop("adapter", "auto")),
        api_key=_clean_str(payload.pop("api_key", None)),
        api_base=_clean_str(payload.pop("api_base", None)),
        api_version=_clean_str(payload.pop("api_version", None)),
        temperature=_float_value(payload.pop("temperature", None)),
        top_p=_float_value(payload.pop("top_p", None)),
        max_tokens=_int_value(payload.pop("max_tokens", None), 0)
        if payload.get("max_tokens") is not None
        else None,
        system_prompt=_clean_str(payload.pop("system_prompt", None)),
        model_kwargs={**extra_kwargs, **payload},
    )


def _resolve_embedding_config(
    aisystem_id: object,
    aisystem_parameters: object,
) -> RagasEmbeddingConfig:
    payload = _json_map(aisystem_parameters)
    embedding_payload = _json_map(payload.get("embedding"))
    if not embedding_payload:
        for key in (
            "embedding_model",
            "embedding_provider",
            "embedding_api_key",
            "embedding_api_base",
            "embedding_api_version",
            "embedding_kwargs",
            "embedding_secret_id",
            "embedding_aisystem_id",
            "embedding_options",
        ):
            if key in payload:
                embedding_payload[key] = payload.get(key)

    embedding_aisystem_id = _clean_str(embedding_payload.get("embedding_aisystem_id"))
    override_secret_id = _clean_str(embedding_payload.get("embedding_secret_id"))
    if embedding_aisystem_id is None:
        embedding_aisystem_id = _clean_str(aisystem_id)

    if embedding_aisystem_id is not None:
        try:
            resolved = resolve_litellm_payload_from_aisystem(
                aisystem_id=embedding_aisystem_id,
                expected_interface_type="llm.responses",
                secret_id_or_key=override_secret_id,
                options=embedding_payload,
            )
            embedding_payload.setdefault("embedding_model", resolved["model"])
            embedding_payload.setdefault("embedding_secret_id", resolved["secret_id"])
        except Exception:
            pass

    resolved_model = _clean_str(embedding_payload.get("embedding_model"))
    if resolved_model is None:
        raise ValueError("embedding_model is required")

    payload = _json_map(embedding_payload.get("embedding_options"))
    payload["model"] = resolved_model

    if (value := _clean_str(embedding_payload.get("embedding_provider"))) is not None:
        payload["provider"] = value
    if (value := _clean_str(embedding_payload.get("embedding_api_key"))) is not None:
        payload["api_key"] = value
    if (value := _clean_str(embedding_payload.get("embedding_api_base"))) is not None:
        payload["api_base"] = value
    if (value := _clean_str(embedding_payload.get("embedding_api_version"))) is not None:
        payload["api_version"] = value
    kwargs = _json_map(embedding_payload.get("embedding_kwargs"))
    if kwargs:
        payload["embedding_kwargs"] = kwargs

    payload = merge_litellm_payload_with_secret(
        payload,
        _clean_str(embedding_payload.get("embedding_secret_id")),
        payload,
    )
    extra_kwargs = _json_map(payload.pop("embedding_kwargs", {}))

    return RagasEmbeddingConfig(
        model=str(payload.pop("model")),
        provider=str(payload.pop("provider", "openai")),
        api_key=_clean_str(payload.pop("api_key", None)),
        api_base=_clean_str(payload.pop("api_base", None)),
        api_version=_clean_str(payload.pop("api_version", None)),
        embedding_kwargs={**extra_kwargs, **payload},
    )


def _build_llm(config: RagasLLMConfig) -> Any:
    from ragas.llms import llm_factory

    if config.provider == "openai":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.api_base,
        )
        return llm_factory(
            model=config.model,
            provider=config.provider,
            client=client,
            adapter=config.adapter,
            **_build_llm_kwargs(config),
        )

    if config.provider == "litellm":
        import instructor
        import litellm

        client = instructor.from_litellm(litellm, mode=instructor.Mode.JSON)
        kwargs = _build_llm_kwargs(config)
        if config.api_key is not None:
            kwargs.setdefault("api_key", config.api_key)
        if config.api_base is not None:
            kwargs.setdefault("api_base", config.api_base)
        if config.api_version is not None:
            kwargs.setdefault("api_version", config.api_version)
        return llm_factory(
            model=config.model,
            provider=config.provider,
            client=client,
            adapter="litellm" if config.adapter == "auto" else config.adapter,
            **kwargs,
        )

    raise ValueError(f"Unsupported LLM provider '{config.provider}'")


def _build_embeddings(config: RagasEmbeddingConfig) -> Any:
    if config.provider == "openai":
        from openai import AsyncOpenAI
        from ragas.embeddings import OpenAIEmbeddings

        client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.api_base,
        )
        return OpenAIEmbeddings(client=client, model=config.model)

    if config.provider == "litellm":
        from ragas.embeddings import LiteLLMEmbeddings

        return LiteLLMEmbeddings(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            api_version=config.api_version,
            **config.embedding_kwargs,
        )

    raise ValueError(f"Unsupported embedding provider '{config.provider}'")


def _build_tool_call(value: object) -> Any:
    from ragas.messages import ToolCall

    tool_call = RagasToolCall.model_validate(value)
    return ToolCall(name=tool_call.name, args=tool_call.args)


def _build_messages(value: object) -> list[Any]:
    from ragas.messages import AIMessage, HumanMessage, ToolMessage

    messages: list[Any] = []
    for item in _json_list(value):
        message = RagasMessage.model_validate(item)
        if message.type == "human":
            messages.append(HumanMessage(content=message.content, metadata=message.metadata))
        elif message.type == "tool":
            messages.append(ToolMessage(content=message.content, metadata=message.metadata))
        else:
            messages.append(
                AIMessage(
                    content=message.content,
                    metadata=message.metadata,
                    tool_calls=[_build_tool_call(tool_call) for tool_call in message.tool_calls or []],
                )
            )
    return messages


def _single_turn_sample(
    user_input: object = None,
    response: object = None,
    reference: object = None,
    retrieved_contexts: object = None,
    reference_contexts: object = None,
    rubrics: object = None,
) -> Any:
    from ragas.dataset_schema import SingleTurnSample

    return SingleTurnSample(
        user_input=_clean_str(user_input),
        response=_clean_str(response),
        reference=_clean_str(reference),
        retrieved_contexts=_str_list(retrieved_contexts) or None,
        reference_contexts=_str_list(reference_contexts) or None,
        rubrics=_string_map(rubrics) or None,
    )


class RagasMetricRowFunction(AsyncRowFunction):
    """Shared base with client caches for LLMs and embeddings."""

    def __init__(self) -> None:
        super().__init__()
        self._llm_cache: Dict[str, Any] = {}
        self._embedding_cache: Dict[str, Any] = {}

    def _get_llm(self, config: RagasLLMConfig) -> Any:
        key = config.model_dump_json()
        if key not in self._llm_cache:
            self._llm_cache[key] = _build_llm(config)
        return self._llm_cache[key]

    def _get_embeddings(self, config: RagasEmbeddingConfig) -> Any:
        key = config.model_dump_json()
        if key not in self._embedding_cache:
            self._embedding_cache[key] = _build_embeddings(config)
        return self._embedding_cache[key]


class RagasContextPrecisionRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        reference: Optional[str],
        retrieved_contexts: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import ContextPrecision

        metric = ContextPrecision(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            user_input=user_input or "",
            reference=reference or "",
            retrieved_contexts=_str_list(retrieved_contexts),
        )
        return _metric_response(result)


class RagasContextRecallRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        retrieved_contexts: object,
        reference: Optional[str],
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import ContextRecall

        metric = ContextRecall(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            user_input=user_input or "",
            retrieved_contexts=_str_list(retrieved_contexts),
            reference=reference or "",
        )
        return _metric_response(result)


class RagasContextEntityRecallRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        reference: Optional[str],
        retrieved_contexts: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import ContextEntityRecall

        metric = ContextEntityRecall(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            reference=reference or "",
            retrieved_contexts=_str_list(retrieved_contexts),
        )
        return _metric_response(result)


class RagasNoiseSensitivityRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        reference: Optional[str],
        retrieved_contexts: object,
        mode: Optional[str],
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import NoiseSensitivity

        metric = NoiseSensitivity(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            mode=str(mode or "relevant"),
        )
        result = await metric.ascore(
            user_input=user_input or "",
            response=response or "",
            reference=reference or "",
            retrieved_contexts=_str_list(retrieved_contexts),
        )
        return _metric_response(result)


class RagasResponseRelevancyRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        strictness: Optional[int],
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = retry
        from ragas.metrics import ResponseRelevancy

        metric = ResponseRelevancy(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            embeddings=self._get_embeddings(
                _resolve_embedding_config(aisystem_id, aisystem_parameters)
            ),
            strictness=_int_value(strictness, 3),
        )
        result = await metric.single_turn_ascore(
            _single_turn_sample(user_input=user_input, response=response),
            timeout=timeout.timeout_s if timeout else None,
        )
        return _metric_response(result)


class RagasFaithfulnessRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        retrieved_contexts: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import Faithfulness

        metric = Faithfulness(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            user_input=user_input or "",
            response=response or "",
            retrieved_contexts=_str_list(retrieved_contexts),
        )
        return _metric_response(result)


class RagasMultimodalFaithfulnessRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        response: Optional[str],
        retrieved_contexts: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import MultiModalFaithfulness

        metric = MultiModalFaithfulness(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            response=response or "",
            retrieved_contexts=_str_list(retrieved_contexts),
        )
        return _metric_response(result)


class RagasMultimodalRelevanceRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        retrieved_contexts: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import MultiModalRelevance

        metric = MultiModalRelevance(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            user_input=user_input or "",
            response=response or "",
            retrieved_contexts=_str_list(retrieved_contexts),
        )
        return _metric_response(result)


class RagasTopicAdherenceRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: object,
        reference_topics: object,
        mode: Optional[str],
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import TopicAdherence

        metric = TopicAdherence(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            mode=str(mode or "f1"),
        )
        result = await metric.ascore(
            user_input=_build_messages(user_input),
            reference_topics=_str_list(reference_topics),
        )
        return _metric_response(result)


class RagasToolCallAccuracyRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: object,
        reference_tool_calls: object,
        strict_order: Optional[bool],
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import ToolCallAccuracy

        metric = ToolCallAccuracy(strict_order=_bool_value(strict_order, True))
        result = await metric.ascore(
            user_input=_build_messages(user_input),
            reference_tool_calls=[_build_tool_call(item) for item in _json_list(reference_tool_calls)],
        )
        return _metric_response(result)


class RagasToolCallF1RowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: object,
        reference_tool_calls: object,
        batch_size: Optional[int],
        is_multi_turn: Optional[bool],
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import ToolCallF1

        metric = ToolCallF1(
            batch_size=_int_value(batch_size, 1),
            is_multi_turn=_bool_value(is_multi_turn, True),
        )
        result = await metric.ascore(
            user_input=_build_messages(user_input),
            reference_tool_calls=[_build_tool_call(item) for item in _json_list(reference_tool_calls)],
        )
        return _metric_response(result)


class RagasAgentGoalAccuracyRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: object,
        reference: Optional[str],
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import AgentGoalAccuracy

        metric = AgentGoalAccuracy(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            user_input=_build_messages(user_input),
            reference=reference or "",
        )
        return _metric_response(result)


class RagasAspectCriticRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        reference: Optional[str],
        retrieved_contexts: object,
        reference_contexts: object,
        definition: Optional[str],
        strictness: Optional[int],
        rubrics: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = retry
        from ragas.metrics import AspectCritic

        metric = AspectCritic(
            name="aspect_critic",
            definition=definition or "",
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            strictness=_int_value(strictness, 1),
        )
        result = await metric.single_turn_ascore(
            _single_turn_sample(
                user_input=user_input,
                response=response,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
                reference_contexts=reference_contexts,
                rubrics=rubrics,
            ),
            timeout=timeout.timeout_s if timeout else None,
        )
        return _metric_response(result)


class RagasSimpleCriteriaScoringRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        reference: Optional[str],
        retrieved_contexts: object,
        reference_contexts: object,
        definition: Optional[str],
        strictness: Optional[int],
        rubrics: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = retry
        from ragas.metrics import SimpleCriteriaScore

        metric = SimpleCriteriaScore(
            name="simple_criteria_scoring",
            definition=definition or "",
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            strictness=_int_value(strictness, 1),
        )
        result = await metric.single_turn_ascore(
            _single_turn_sample(
                user_input=user_input,
                response=response,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
                reference_contexts=reference_contexts,
                rubrics=rubrics,
            ),
            timeout=timeout.timeout_s if timeout else None,
        )
        return _metric_response(result)


class RagasRubricsBasedScoringRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        reference: Optional[str],
        retrieved_contexts: object,
        reference_contexts: object,
        rubrics: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = retry
        from ragas.metrics.collections import DomainSpecificRubrics

        metric = DomainSpecificRubrics(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            rubrics=_string_map(rubrics),
            name="rubrics_based_scoring",
        )
        result = await metric.single_turn_ascore(
            _single_turn_sample(
                user_input=user_input,
                response=response,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
                reference_contexts=reference_contexts,
                rubrics=rubrics,
            ),
            timeout=timeout.timeout_s if timeout else None,
        )
        return _metric_response(result)


class RagasInstanceSpecificRubricsScoringRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        user_input: Optional[str],
        response: Optional[str],
        reference: Optional[str],
        retrieved_contexts: object,
        reference_contexts: object,
        rubrics: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = retry
        from ragas.metrics.collections import InstanceSpecificRubrics

        metric = InstanceSpecificRubrics(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            name="instance_specific_rubrics_scoring",
        )
        result = await metric.single_turn_ascore(
            _single_turn_sample(
                user_input=user_input,
                response=response,
                reference=reference,
                retrieved_contexts=retrieved_contexts,
                reference_contexts=reference_contexts,
                rubrics=rubrics,
            ),
            timeout=timeout.timeout_s if timeout else None,
        )
        return _metric_response(result)


class RagasSummarizationRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        response: Optional[str],
        reference: Optional[str],
        length_penalty: Optional[bool],
        coeff: Optional[float],
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = retry
        from ragas.metrics.collections import SummaryScore

        metric = SummaryScore(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            ),
            length_penalty=_bool_value(length_penalty, True),
            coeff=_float_value(coeff) if _float_value(coeff) is not None else 0.5,
            name="summarization",
        )
        result = await metric.single_turn_ascore(
            _single_turn_sample(response=response, reference=reference),
            timeout=timeout.timeout_s if timeout else None,
        )
        return _metric_response(result)


class RagasExecutionBasedDatacompyScoreRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        reference: Optional[str],
        response: Optional[str],
        mode: Optional[str],
        metric_name: Optional[str],
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import DataCompyScore

        metric = DataCompyScore(
            mode=str(mode or "rows"),
            metric=str(metric_name or "f1"),
        )
        result = await metric.ascore(reference=reference or "", response=response or "")
        return _metric_response(result)


class RagasSQLQueryEquivalenceRowFunction(RagasMetricRowFunction):
    async def transform(
        self,
        response: Optional[str],
        reference: Optional[str],
        reference_contexts: object,
        aisystem_id: Optional[str],
        aisystem_parameters: object,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> RagasMetricResponse:
        _ = timeout, retry
        from ragas.metrics.collections import SQLSemanticEquivalence

        metric = SQLSemanticEquivalence(
            llm=self._get_llm(
                _resolve_llm_config(aisystem_id, aisystem_parameters)
            )
        )
        result = await metric.ascore(
            response=response or "",
            reference=reference or "",
            reference_contexts=_str_list(reference_contexts) or None,
        )
        return _metric_response(result)


class _BaseRagasUdf(Udf):
    """Common explicit-schema UDF boilerplate for Ragas wrappers."""

    arg_names: Tuple[str, ...]
    schema: Tuple[DType, ...]
    row_function_cls: type[Function]

    def input_schema(self) -> Tuple[DType, ...]:
        return self.schema

    def input_args(self) -> Tuple[str, ...]:
        return self.arg_names

    def signature(self) -> Tuple[Param, ...]:
        return super().signature() + (
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return JsonEncodedPydanticType(RagasMetricResponse)

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return self.row_function_cls()


class RagasContextPrecisionUdf(_BaseRagasUdf, name="agent.ragas.context_precision"):
    arg_names = ("user_input", "reference", "retrieved_contexts") + AISYSTEM_ARG_NAMES
    schema = (StringType(), StringType(), JsonType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasContextPrecisionRowFunction


class RagasContextRecallUdf(_BaseRagasUdf, name="agent.ragas.context_recall"):
    arg_names = ("user_input", "retrieved_contexts", "reference") + AISYSTEM_ARG_NAMES
    schema = (StringType(), JsonType(), StringType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasContextRecallRowFunction


class RagasContextEntitiesRecallUdf(
    _BaseRagasUdf, name="agent.ragas.context_entities_recall"
):
    arg_names = ("reference", "retrieved_contexts") + AISYSTEM_ARG_NAMES
    schema = (StringType(), JsonType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasContextEntityRecallRowFunction


class RagasNoiseSensitivityUdf(_BaseRagasUdf, name="agent.ragas.noise_sensitivity"):
    arg_names = (
        "user_input",
        "response",
        "reference",
        "retrieved_contexts",
        "mode",
    ) + AISYSTEM_ARG_NAMES
    schema = (
        StringType(),
        StringType(),
        StringType(),
        JsonType(),
        StringType(),
    ) + AISYSTEM_SCHEMA
    row_function_cls = RagasNoiseSensitivityRowFunction


class RagasResponseRelevancyUdf(_BaseRagasUdf, name="agent.ragas.response_relevancy"):
    arg_names = ("user_input", "response", "strictness") + AISYSTEM_ARG_NAMES
    schema = (StringType(), StringType(), IntType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasResponseRelevancyRowFunction


class RagasFaithfulnessUdf(_BaseRagasUdf, name="agent.ragas.faithfulness"):
    arg_names = ("user_input", "response", "retrieved_contexts") + AISYSTEM_ARG_NAMES
    schema = (StringType(), StringType(), JsonType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasFaithfulnessRowFunction


class RagasMultimodalFaithfulnessUdf(
    _BaseRagasUdf, name="agent.ragas.multimodal_faithfulness"
):
    arg_names = ("response", "retrieved_contexts") + AISYSTEM_ARG_NAMES
    schema = (StringType(), JsonType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasMultimodalFaithfulnessRowFunction


class RagasMultimodalRelevanceUdf(_BaseRagasUdf, name="agent.ragas.multimodal_relevance"):
    arg_names = ("user_input", "response", "retrieved_contexts") + AISYSTEM_ARG_NAMES
    schema = (StringType(), StringType(), JsonType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasMultimodalRelevanceRowFunction


class RagasTopicAdherenceUdf(_BaseRagasUdf, name="agent.ragas.topic_adherence"):
    arg_names = ("user_input", "reference_topics", "mode") + AISYSTEM_ARG_NAMES
    schema = (JsonType(), JsonType(), StringType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasTopicAdherenceRowFunction


class RagasToolCallAccuracyUdf(_BaseRagasUdf, name="agent.ragas.tool_call_accuracy"):
    arg_names = ("user_input", "reference_tool_calls", "strict_order")
    schema = (JsonType(), JsonType(), BooleanType())
    row_function_cls = RagasToolCallAccuracyRowFunction


class RagasToolCallF1Udf(_BaseRagasUdf, name="agent.ragas.tool_call_f1"):
    arg_names = ("user_input", "reference_tool_calls", "batch_size", "is_multi_turn")
    schema = (JsonType(), JsonType(), IntType(), BooleanType())
    row_function_cls = RagasToolCallF1RowFunction


class RagasAgentGoalAccuracyUdf(_BaseRagasUdf, name="agent.ragas.agent_goal_accuracy"):
    arg_names = ("user_input", "reference") + AISYSTEM_ARG_NAMES
    schema = (JsonType(), StringType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasAgentGoalAccuracyRowFunction


class RagasAspectCriticUdf(_BaseRagasUdf, name="agent.ragas.aspect_critic"):
    arg_names = (
        "user_input",
        "response",
        "reference",
        "retrieved_contexts",
        "reference_contexts",
        "definition",
        "strictness",
        "rubrics",
    ) + AISYSTEM_ARG_NAMES
    schema = (
        StringType(),
        StringType(),
        StringType(),
        JsonType(),
        JsonType(),
        StringType(),
        IntType(),
        JsonType(),
    ) + AISYSTEM_SCHEMA
    row_function_cls = RagasAspectCriticRowFunction


class RagasSimpleCriteriaScoringUdf(
    _BaseRagasUdf, name="agent.ragas.simple_criteria_scoring"
):
    arg_names = (
        "user_input",
        "response",
        "reference",
        "retrieved_contexts",
        "reference_contexts",
        "definition",
        "strictness",
        "rubrics",
    ) + AISYSTEM_ARG_NAMES
    schema = (
        StringType(),
        StringType(),
        StringType(),
        JsonType(),
        JsonType(),
        StringType(),
        IntType(),
        JsonType(),
    ) + AISYSTEM_SCHEMA
    row_function_cls = RagasSimpleCriteriaScoringRowFunction


class RagasRubricsBasedScoringUdf(_BaseRagasUdf, name="agent.ragas.rubrics_based_scoring"):
    arg_names = (
        "user_input",
        "response",
        "reference",
        "retrieved_contexts",
        "reference_contexts",
        "rubrics",
    ) + AISYSTEM_ARG_NAMES
    schema = (
        StringType(),
        StringType(),
        StringType(),
        JsonType(),
        JsonType(),
        JsonType(),
    ) + AISYSTEM_SCHEMA
    row_function_cls = RagasRubricsBasedScoringRowFunction


class RagasInstanceSpecificRubricsScoringUdf(
    _BaseRagasUdf, name="agent.ragas.instance_specific_rubrics_scoring"
):
    arg_names = (
        "user_input",
        "response",
        "reference",
        "retrieved_contexts",
        "reference_contexts",
        "rubrics",
    ) + AISYSTEM_ARG_NAMES
    schema = (
        StringType(),
        StringType(),
        StringType(),
        JsonType(),
        JsonType(),
        JsonType(),
    ) + AISYSTEM_SCHEMA
    row_function_cls = RagasInstanceSpecificRubricsScoringRowFunction


class RagasSummarizationUdf(_BaseRagasUdf, name="agent.ragas.summarization"):
    arg_names = ("response", "reference", "length_penalty", "coeff") + AISYSTEM_ARG_NAMES
    schema = (StringType(), StringType(), BooleanType(), FloatType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasSummarizationRowFunction


class RagasExecutionBasedDatacompyScoreUdf(
    _BaseRagasUdf, name="agent.ragas.execution_based_datacompy_score"
):
    arg_names = ("reference", "response", "mode", "metric_name")
    schema = (StringType(), StringType(), StringType(), StringType())
    row_function_cls = RagasExecutionBasedDatacompyScoreRowFunction


class RagasSQLQueryEquivalenceUdf(_BaseRagasUdf, name="agent.ragas.sql_query_equivalence"):
    arg_names = ("response", "reference", "reference_contexts") + AISYSTEM_ARG_NAMES
    schema = (StringType(), StringType(), JsonType()) + AISYSTEM_SCHEMA
    row_function_cls = RagasSQLQueryEquivalenceRowFunction
