from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Union

import litellm
from pydantic import BaseModel, ConfigDict, Field

from .auth_config import AuthConfig
from .auth_headers import auth_headers


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[Mapping[str, Any]] = Field(default_factory=list)
    timeout: Optional[Union[float, int]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stream: Optional[bool] = None
    stream_options: Optional[dict[str, Any]] = None
    stop: Optional[Any] = None
    max_completion_tokens: Optional[int] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[dict[str, Any]] = None
    user: Optional[str] = None
    response_format: Optional[dict[str, Any]] = None
    seed: Optional[int] = None
    tools: Optional[List[Any]] = None
    tool_choice: Optional[str] = None
    parallel_tool_calls: Optional[bool] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    safety_identifier: Optional[str] = None
    deployment_id: Optional[str] = None
    functions: Optional[List[Any]] = None
    function_call: Optional[str] = None
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    api_key: Optional[str] = None
    model_list: Optional[List[Any]] = None
    api_base: Optional[str] = None
    extra_headers: Optional[Mapping[str, str]] = None


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    object: Optional[str] = None
    created: Optional[int] = None
    model: Optional[str] = None
    system_fingerprint: Optional[str] = None
    choices: Optional[List[dict[str, Any]]] = None
    usage: Optional[dict[str, Any]] = None
    response_ms: Optional[float] = None
    response_headers: Optional[dict[str, Any]] = None


def _coerce_response(response: Any) -> CompletionResponse:
    if isinstance(response, CompletionResponse):
        return response
    if hasattr(response, "model_dump"):
        return CompletionResponse.model_validate(response.model_dump())
    if hasattr(response, "dict"):
        return CompletionResponse.model_validate(response.dict())
    if isinstance(response, dict):
        return CompletionResponse.model_validate(response)
    return CompletionResponse()


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
                merged = {**existing, **value}
                kwargs["extra_headers"] = merged
            else:
                kwargs["extra_headers"] = dict(value)
            continue
        kwargs[key] = value


def _build_completion_kwargs(
    request: CompletionRequest,
    auth_config: Optional[AuthConfig],
) -> dict[str, Any]:
    headers = _merge_headers(auth_config, request.extra_headers)
    kwargs = request.model_dump(exclude_none=True)
    kwargs["messages"] = list(request.messages)
    kwargs["extra_headers"] = headers
    kwargs["stream"] = request.stream if request.stream is not None else False
    kwargs["api_base"] = (
        request.base_url
        or request.api_base
        or (auth_config.url if auth_config else None)
    )
    kwargs.pop("base_url", None)
    _merge_additional_params(auth_config, kwargs)
    return {key: value for key, value in kwargs.items() if value is not None}


async def acompletion(
    request: CompletionRequest,
    auth_config: Optional[AuthConfig] = None,
) -> CompletionResponse:
    cleaned = _build_completion_kwargs(request, auth_config)
    response = await litellm.acompletion(**cleaned)
    return _coerce_response(response)


def completion(
    request: CompletionRequest,
    auth_config: Optional[AuthConfig] = None,
) -> CompletionResponse:
    cleaned = _build_completion_kwargs(request, auth_config)
    response = litellm.completion(**cleaned)
    return _coerce_response(response)
