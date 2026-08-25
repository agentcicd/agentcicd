from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Union

import litellm
from pydantic import BaseModel, ConfigDict, Field

from .auth_config import AuthConfig
from .auth_headers import auth_headers
from .llm_completion import CompletionRequest, acompletion


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: Union[str, Sequence[Mapping[str, Any]], Mapping[str, Any]]
    instructions: Optional[str] = None
    timeout: Optional[Union[float, int]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_output_tokens: Optional[int] = None
    stream: Optional[bool] = None
    tools: Optional[list[Any]] = None
    tool_choice: Optional[Any] = None
    user: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None
    response_format: Optional[dict[str, Any]] = None
    base_url: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    extra_headers: Optional[Mapping[str, str]] = None


class ResponsesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    object: Optional[str] = None
    created: Optional[int] = None
    model: Optional[str] = None
    status: Optional[str] = None
    output: Optional[list[Any]] = None
    output_text: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    response_ms: Optional[float] = None
    response_headers: Optional[dict[str, Any]] = None


def _coerce_response(response: Any) -> ResponsesResponse:
    if isinstance(response, ResponsesResponse):
        return response
    if hasattr(response, "model_dump"):
        return ResponsesResponse.model_validate(response.model_dump())
    if hasattr(response, "dict"):
        return ResponsesResponse.model_validate(response.dict())
    if isinstance(response, dict):
        return ResponsesResponse.model_validate(response)
    return ResponsesResponse()


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


def _extract_output_text(response: ResponsesResponse) -> Optional[str]:
    if response.output_text:
        return response.output_text

    parts: list[str] = []
    for output_item in response.output or []:
        if not isinstance(output_item, Mapping):
            continue
        content = output_item.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        text = output_item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts) if parts else None


def _build_responses_kwargs(
    request: ResponsesRequest,
    auth_config: Optional[AuthConfig],
) -> dict[str, Any]:
    headers = _merge_headers(auth_config, request.extra_headers)
    kwargs = request.model_dump(exclude_none=True)
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


def _responses_input_to_messages(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, Mapping):
        return [dict(input_value)]
    if isinstance(input_value, Sequence):
        return [dict(item) for item in input_value if isinstance(item, Mapping)]
    return [{"role": "user", "content": str(input_value)}]


async def aresponse(
    request: ResponsesRequest,
    auth_config: Optional[AuthConfig] = None,
) -> ResponsesResponse:
    cleaned = _build_responses_kwargs(request, auth_config)

    if hasattr(litellm, "aresponses"):
        raw = await litellm.aresponses(**cleaned)
        response = _coerce_response(raw)
        response.output_text = response.output_text or _extract_output_text(response)
        return response

    # Compatibility fallback when the LiteLLM build lacks responses API.
    completion_request = CompletionRequest(
        **{
            key: value
            for key, value in cleaned.items()
            if key not in {"input", "instructions", "max_output_tokens"}
        },
        messages=_responses_input_to_messages(cleaned.get("input")),
        max_tokens=cleaned.get("max_output_tokens"),
    )
    completion_response = await acompletion(completion_request, auth_config=auth_config)
    first_choice = (completion_response.choices or [{}])[0]
    output_text = None
    if isinstance(first_choice, Mapping):
        message = first_choice.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                output_text = content

    return ResponsesResponse(
        id=completion_response.id,
        object="response",
        created=completion_response.created,
        model=completion_response.model,
        status="completed",
        output_text=output_text,
        usage=completion_response.usage,
    )


def response(
    request: ResponsesRequest,
    auth_config: Optional[AuthConfig] = None,
) -> ResponsesResponse:
    cleaned = _build_responses_kwargs(request, auth_config)
    if hasattr(litellm, "responses"):
        raw = litellm.responses(**cleaned)
        coerced = _coerce_response(raw)
        coerced.output_text = coerced.output_text or _extract_output_text(coerced)
        return coerced
    raise RuntimeError("litellm.responses is unavailable in current runtime")
