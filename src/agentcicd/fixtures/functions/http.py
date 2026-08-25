from __future__ import annotations

import base64
import json
from typing import Any, Callable, Mapping, Optional, Tuple
from urllib.parse import urljoin

from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import BooleanType, DType, FType, IntType, StringType
from agentcicd.fixtures.core.udf import Param, Udf

from .utils.runtime_context import resolve_aisystem

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None


def _require_httpx():
    if httpx is None:
        raise ImportError("httpx is required for HTTP functions")
    return httpx


def _httpx_timeout(timeout: TimeoutConfig) -> httpx.Timeout:
    httpx_module = _require_httpx()
    return httpx_module.Timeout(
        timeout=timeout.timeout if timeout.timeout is not None else 60.0,
        connect=timeout.connect if timeout.connect is not None else 20.0,
        read=timeout.read if timeout.read is not None else 60.0,
        write=timeout.write if timeout.write is not None else 60.0,
    )


def _safe_int(value: Optional[int], default: int, minimum: int = 1) -> int:
    try:
        resolved = int(value) if value is not None else default
    except Exception:
        resolved = default
    return max(minimum, resolved)


def _json_mapping(text: Optional[str]) -> Mapping[str, Any]:
    if not text:
        return {}
    parsed = json.loads(text)
    if isinstance(parsed, Mapping):
        return parsed
    raise ValueError("Expected JSON object")


class HttpRequestRowFunction(AsyncRowFunction):
    async def transform(
        self,
        method: Optional[str],
        url: Optional[str],
        headers_json: Optional[str],
        params_json: Optional[str],
        body: Optional[str],
        json_body: Optional[bool],
        allow_redirects: Optional[bool],
        max_bytes: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = retry
        if not url:
            return None

        resolved_method = (method or "GET").upper()
        resolved_headers = {str(k): str(v) for k, v in _json_mapping(headers_json).items()}
        resolved_params = _json_mapping(params_json) if params_json else None
        resolved_max_bytes = _safe_int(max_bytes, default=2_000_000)

        request_json: Any = None
        request_content: Any = None
        if body is not None:
            if bool(json_body):
                request_json = json.loads(body)
            else:
                request_content = body

        try:
            httpx_module = _require_httpx()
            async with httpx_module.AsyncClient(
                timeout=_httpx_timeout(timeout),
                follow_redirects=bool(allow_redirects) if allow_redirects is not None else True,
            ) as client:
                response = await client.request(
                    resolved_method,
                    str(url),
                    headers=resolved_headers,
                    params=resolved_params,
                    json=request_json,
                    content=request_content,
                )

            payload = response.content
            truncated = len(payload) > resolved_max_bytes
            payload = payload[:resolved_max_bytes]
            return json.dumps(
                {
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "ok": 200 <= int(response.status_code) < 300,
                    "content_type": response.headers.get("content-type", ""),
                    "headers": dict(response.headers),
                    "body": payload.decode("utf-8", errors="replace"),
                    "body_base64": base64.b64encode(payload).decode("ascii"),
                    "truncated": truncated,
                    "error": None,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "url": str(url),
                    "status_code": None,
                    "ok": False,
                    "content_type": "",
                    "headers": {},
                    "body": "",
                    "body_base64": "",
                    "truncated": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )


class HttpRequestUdf(Udf, name="http.request"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            StringType(),
            StringType(),
            StringType(),
            StringType(),
            BooleanType(),
            BooleanType(),
            IntType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return (
            "method",
            "url",
            "headers_json",
            "params_json",
            "body",
            "json_body",
            "allow_redirects",
            "max_bytes",
        )

    def signature(self) -> Tuple[Param, ...]:
        return tuple(Param(name, required=True) for name in self.input_args()) + (
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return HttpRequestRowFunction()


class HttpGetRowFunction(AsyncRowFunction):
    async def transform(
        self,
        url: Optional[str],
        headers_json: Optional[str],
        params_json: Optional[str],
        allow_redirects: Optional[bool],
        max_bytes: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        return await HttpRequestRowFunction().transform(
            "GET",
            url,
            headers_json,
            params_json,
            None,
            None,
            allow_redirects,
            max_bytes,
            timeout,
            retry,
        )


class HttpPostRowFunction(AsyncRowFunction):
    async def transform(
        self,
        url: Optional[str],
        headers_json: Optional[str],
        params_json: Optional[str],
        body: Optional[str],
        json_body: Optional[bool],
        allow_redirects: Optional[bool],
        max_bytes: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        return await HttpRequestRowFunction().transform(
            "POST",
            url,
            headers_json,
            params_json,
            body,
            json_body,
            allow_redirects,
            max_bytes,
            timeout,
            retry,
        )


class HttpGetUdf(Udf, name="http.get"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType(), BooleanType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("url", "headers_json", "params_json", "allow_redirects", "max_bytes")

    def signature(self) -> Tuple[Param, ...]:
        return tuple(Param(name, required=True) for name in self.input_args()) + (
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return HttpGetRowFunction()


class HttpPostUdf(Udf, name="http.post"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType(), StringType(), BooleanType(), BooleanType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("url", "headers_json", "params_json", "body", "json_body", "allow_redirects", "max_bytes")

    def signature(self) -> Tuple[Param, ...]:
        return tuple(Param(name, required=True) for name in self.input_args()) + (
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return HttpPostRowFunction()


def _http_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    extractors = {
        "api_key": _api_key_headers_from_secret,
        "bearer": _bearer_headers_from_secret,
        "basic": _basic_headers_from_secret,
        "oauth2": _oauth2_headers_from_secret,
        "raw": _raw_headers_from_secret,
    }
    secret_type = str(secret.get("type") or "").strip().lower()
    extractor = extractors.get(secret_type)
    return extractor(secret) if extractor is not None else {}


def _api_key_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    from agentcicd.fixtures.aisystem import (
        ApiKeyAuth,
        auth_headers,
    )

    api_key = secret.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return {}
    header_name = str(secret.get("header_name") or "X-API-Key").strip() or "X-API-Key"
    return dict(auth_headers(ApiKeyAuth(api_key=api_key.strip(), header_name=header_name)))


def _bearer_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    from agentcicd.fixtures.aisystem import (
        BearerTokenAuth,
        auth_headers,
    )

    token = secret.get("token")
    if not isinstance(token, str) or not token.strip():
        return {}
    return dict(auth_headers(BearerTokenAuth(token=token.strip())))


def _basic_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    from agentcicd.fixtures.aisystem import (
        BasicAuth,
        auth_headers,
    )

    username = secret.get("username")
    password = secret.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return {}
    return dict(auth_headers(BasicAuth(username=username, password=password)))


def _oauth2_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    from agentcicd.fixtures.aisystem import (
        OAuth2ClientCredentialsAuth,
        auth_headers,
    )

    token_url = secret.get("token_url")
    client_id = secret.get("client_id")
    client_secret = secret.get("client_secret")
    if not (
        isinstance(token_url, str)
        and token_url.strip()
        and isinstance(client_id, str)
        and isinstance(client_secret, str)
    ):
        return {}
    scopes = secret.get("scopes")
    extra_params = secret.get("extra_params")
    return dict(
        auth_headers(
            OAuth2ClientCredentialsAuth(
                token_url=token_url.strip(),
                client_id=client_id,
                client_secret=client_secret,
                scopes=(
                    [str(item) for item in scopes if isinstance(item, str)]
                    if isinstance(scopes, list)
                    else None
                ),
                extra_params=(
                    {str(k): str(v) for k, v in extra_params.items()}
                    if isinstance(extra_params, Mapping)
                    else None
                ),
            )
        )
    )


def _raw_headers_from_secret(secret: Mapping[str, Any]) -> dict[str, str]:
    from agentcicd.fixtures.aisystem import CustomHeaderAuth, auth_headers

    value = secret.get("value")
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    headers = parsed.get("headers")
    if isinstance(headers, Mapping):
        return {str(k): str(v) for k, v in headers.items()}
    header_name = parsed.get("header_name")
    header_value = parsed.get("value")
    if isinstance(header_name, str) and header_name.strip() and isinstance(header_value, str):
        return dict(auth_headers(CustomHeaderAuth(header_name=header_name.strip(), value=header_value)))
    return {}


def _resolve_http_headers_from_secrets(
    aisystem: Mapping[str, Any],
) -> dict[str, str]:
    from .utils.runtime_context import _context_path_from_env, _load_context

    attached_ids = [
        str(item).strip()
        for item in (aisystem.get("secret_ids") or aisystem.get("secretIds") or [])
        if isinstance(item, str) and item.strip()
    ]
    if not attached_ids:
        return {}
    selected_secret_id = attached_ids[0]

    context = _load_context(_context_path_from_env())
    for item in context.get("secrets") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("id") == selected_secret_id or item.get("key") == selected_secret_id:
            secret = item.get("secret")
            if isinstance(secret, Mapping):
                return _http_headers_from_secret(secret)
            return _http_headers_from_secret(item)
    return {}


def _resolve_http_url_from_aisystem(aisystem: Mapping[str, Any], url: Optional[str]) -> str | None:
    target = aisystem.get("target")
    base_url = str(target).strip() if isinstance(target, str) and target.strip() else None
    if isinstance(url, str) and url.strip():
        if base_url and not url.lower().startswith(("http://", "https://")):
            return urljoin(f"{base_url.rstrip('/')}/", url.lstrip("/"))
        return url.strip()
    return base_url


class AISystemHttpGetRowFunction(AsyncRowFunction):
    async def transform(
        self,
        aisystem_id: Optional[str],
        url: Optional[str],
        headers_json: Optional[str],
        params_json: Optional[str],
        allow_redirects: Optional[bool],
        max_bytes: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        if not aisystem_id:
            return None
        aisystem = resolve_aisystem(aisystem_id=aisystem_id, expected_interface_type="http.get")
        if not aisystem:
            raise ValueError(f"AI system not found: {aisystem_id}")
        resolved_url = _resolve_http_url_from_aisystem(aisystem, url)
        if not resolved_url:
            return None
        base_headers = _resolve_http_headers_from_secrets(aisystem)
        user_headers = {str(k): str(v) for k, v in _json_mapping(headers_json).items()}
        merged_headers = {**base_headers, **user_headers}
        return await HttpRequestRowFunction().transform(
            "GET",
            resolved_url,
            json.dumps(merged_headers, ensure_ascii=False) if merged_headers else None,
            params_json,
            None,
            None,
            allow_redirects,
            max_bytes,
            timeout,
            retry,
        )


class AISystemHttpPostRowFunction(AsyncRowFunction):
    async def transform(
        self,
        aisystem_id: Optional[str],
        url: Optional[str],
        headers_json: Optional[str],
        params_json: Optional[str],
        body: Optional[str],
        json_body: Optional[bool],
        allow_redirects: Optional[bool],
        max_bytes: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        if not aisystem_id:
            return None
        aisystem = resolve_aisystem(aisystem_id=aisystem_id, expected_interface_type="http.post")
        if not aisystem:
            raise ValueError(f"AI system not found: {aisystem_id}")
        resolved_url = _resolve_http_url_from_aisystem(aisystem, url)
        if not resolved_url:
            return None
        base_headers = _resolve_http_headers_from_secrets(aisystem)
        user_headers = {str(k): str(v) for k, v in _json_mapping(headers_json).items()}
        merged_headers = {**base_headers, **user_headers}
        return await HttpRequestRowFunction().transform(
            "POST",
            resolved_url,
            json.dumps(merged_headers, ensure_ascii=False) if merged_headers else None,
            params_json,
            body,
            json_body,
            allow_redirects,
            max_bytes,
            timeout,
            retry,
        )


class AISystemsHttpGetUdf(Udf, name="aisystems.http.get"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType(), StringType(), BooleanType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("aisystem_id", "url", "headers_json", "params_json", "allow_redirects", "max_bytes")

    def signature(self) -> Tuple[Param, ...]:
        return tuple(Param(name, required=True) for name in self.input_args()) + (
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return AISystemHttpGetRowFunction()


class AISystemsHttpPostUdf(Udf, name="aisystems.http.post"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType(), StringType(), StringType(), BooleanType(), BooleanType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("aisystem_id", "url", "headers_json", "params_json", "body", "json_body", "allow_redirects", "max_bytes")

    def signature(self) -> Tuple[Param, ...]:
        return tuple(Param(name, required=True) for name in self.input_args()) + (
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return AISystemHttpPostRowFunction()
