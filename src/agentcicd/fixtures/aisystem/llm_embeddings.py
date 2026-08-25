from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Union

import litellm
from pydantic import BaseModel, ConfigDict, Field

from .auth_config import AuthConfig
from .auth_headers import auth_headers


class EmbeddingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: Union[str, Sequence[str]]
    timeout: Optional[Union[float, int]] = None
    dimensions: Optional[int] = None
    encoding_format: Optional[str] = None
    user: Optional[str] = None
    base_url: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    extra_headers: Optional[Mapping[str, str]] = None


class EmbeddingsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    object: Optional[str] = None
    data: Optional[list[dict[str, Any]]] = None
    model: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    response_ms: Optional[float] = None
    response_headers: Optional[dict[str, Any]] = None


def _coerce_response(response: Any) -> EmbeddingsResponse:
    if isinstance(response, EmbeddingsResponse):
        return response
    if hasattr(response, "model_dump"):
        return EmbeddingsResponse.model_validate(response.model_dump())
    if hasattr(response, "dict"):
        return EmbeddingsResponse.model_validate(response.dict())
    if isinstance(response, dict):
        return EmbeddingsResponse.model_validate(response)
    return EmbeddingsResponse()


def _merge_headers(
    auth_config: Optional[AuthConfig],
    extra_headers: Optional[Mapping[str, str]],
) -> Optional[dict[str, str]]:
    headers: dict[str, str] = {}
    if extra_headers:
        headers.update(extra_headers)
    if auth_config:
        headers.update(auth_headers(auth_config))
    return headers or None


def _merge_additional_params(
    auth_config: Optional[AuthConfig],
    kwargs: dict[str, Any],
) -> None:
    if not auth_config or not auth_config.additional_params:
        return
    for key, value in auth_config.additional_params.items():
        if key == "extra_headers" and isinstance(value, Mapping):
            existing = kwargs.get("extra_headers")
            if isinstance(existing, Mapping):
                kwargs["extra_headers"] = {**existing, **value}
            else:
                kwargs["extra_headers"] = dict(value)
            continue
        kwargs[key] = value


def _build_embeddings_kwargs(
    request: EmbeddingsRequest,
    auth_config: Optional[AuthConfig],
) -> dict[str, Any]:
    headers = _merge_headers(auth_config, request.extra_headers)
    kwargs = request.model_dump(exclude_none=True)
    kwargs["extra_headers"] = headers
    kwargs["api_base"] = (
        request.base_url
        or request.api_base
        or (auth_config.url if auth_config else None)
    )
    kwargs.pop("base_url", None)
    _merge_additional_params(auth_config, kwargs)
    return {key: value for key, value in kwargs.items() if value is not None}


async def aembedding(
    request: EmbeddingsRequest,
    auth_config: Optional[AuthConfig] = None,
) -> EmbeddingsResponse:
    cleaned = _build_embeddings_kwargs(request, auth_config)
    response = await litellm.aembedding(**cleaned)
    return _coerce_response(response)


def embedding(
    request: EmbeddingsRequest,
    auth_config: Optional[AuthConfig] = None,
) -> EmbeddingsResponse:
    cleaned = _build_embeddings_kwargs(request, auth_config)
    response = litellm.embedding(**cleaned)
    return _coerce_response(response)
