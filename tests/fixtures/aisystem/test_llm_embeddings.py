from __future__ import annotations

import asyncio

import pytest

from agentcicd_fixtures_aisystem.auth_config import BearerTokenAuth
from agentcicd_fixtures_aisystem.llm_embeddings import (
    EmbeddingsRequest,
    _build_embeddings_kwargs,
    aembedding,
    embedding,
)


pytestmark = pytest.mark.essential


def test_build_embeddings_kwargs_uses_auth_url_and_merges_headers() -> None:
    request = EmbeddingsRequest(
        model="openai/text-embedding-3-small",
        input=["first answer", "second answer"],
        dimensions=256,
        extra_headers={"X-Trace-Id": "trace-123"},
    )
    auth = BearerTokenAuth(
        token="token-123",
        url="https://gateway.example.test/v1",
        additional_params={"timeout": 30},
    )

    kwargs = _build_embeddings_kwargs(request, auth)

    assert kwargs["input"] == ["first answer", "second answer"]
    assert kwargs["dimensions"] == 256
    assert kwargs["timeout"] == 30
    assert kwargs["api_base"] == "https://gateway.example.test/v1"
    assert kwargs["extra_headers"] == {
        "X-Trace-Id": "trace-123",
        "Authorization": "Bearer token-123",
    }


def test_build_embeddings_kwargs_prefers_request_base_url_over_auth_url() -> None:
    kwargs = _build_embeddings_kwargs(
        EmbeddingsRequest(
            model="openai/text-embedding-3-large",
            input="single answer",
            base_url="https://local-gateway.example.test/v1",
        ),
        BearerTokenAuth(token="token-123", url="https://ignored.example.test/v1"),
    )

    assert kwargs["api_base"] == "https://local-gateway.example.test/v1"
    assert "base_url" not in kwargs


def test_embedding_coerces_litellm_dict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentcicd_fixtures_aisystem.llm_embeddings as module

    captured: dict[str, object] = {}

    def fake_embedding(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "object": "list",
            "model": "openai/text-embedding-3-small",
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }

    monkeypatch.setattr(module.litellm, "embedding", fake_embedding)

    response = embedding(
        EmbeddingsRequest(model="openai/text-embedding-3-small", input="support answer"),
    )

    assert captured["input"] == "support answer"
    assert response.data == [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]
    assert response.usage == {"prompt_tokens": 3, "total_tokens": 3}


def test_aembedding_coerces_async_litellm_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentcicd_fixtures_aisystem.llm_embeddings as module

    async def fake_aembedding(**kwargs: object) -> dict[str, object]:
        return {
            "object": "list",
            "model": kwargs["model"],
            "data": [{"index": 0, "embedding": [0.4, 0.5]}],
        }

    monkeypatch.setattr(module.litellm, "aembedding", fake_aembedding)

    response = asyncio.run(
        aembedding(
            EmbeddingsRequest(model="openai/text-embedding-3-small", input=["first", "second"]),
        )
    )

    assert response.model == "openai/text-embedding-3-small"
    assert response.data == [{"index": 0, "embedding": [0.4, 0.5]}]
