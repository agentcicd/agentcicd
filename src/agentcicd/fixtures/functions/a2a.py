from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional, Tuple
from uuid import uuid4

from agentcicd.fixtures._attrs import read_attr
from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import DType, FType, JsonType, StringType
from agentcicd.fixtures.core.udf import Param, Udf

from .utils.runtime_context import (
    AISystemRuntimeResolver,
    RuntimeResolutionContext,
    resolve_a2a_headers_from_secret,
)


class A2ASendMessageRowFunction(AsyncRowFunction):
    """A2A SendMessage wrapper for AI-system-backed agents."""

    def __init__(self, runtime_context: RuntimeResolutionContext | None = None) -> None:
        super().__init__()
        self._runtime_context = runtime_context
        self._resolver = AISystemRuntimeResolver(runtime_context)

    @staticmethod
    def _timeout(timeout: Optional[TimeoutConfig]) -> TimeoutConfig:
        if timeout is not None:
            return timeout
        default_seconds = float(os.getenv("AGENTCICD_A2A_TIMEOUT_SECONDS", "300"))
        connect_seconds = float(os.getenv("AGENTCICD_A2A_CONNECT_TIMEOUT_SECONDS", "20"))
        return TimeoutConfig(
            timeout=default_seconds,
            read=default_seconds,
            connect=connect_seconds,
            write=default_seconds,
        )

    @staticmethod
    def _httpx_timeout(httpx_module: Any, timeout: TimeoutConfig) -> Any:
        return httpx_module.Timeout(
            timeout=timeout.timeout if timeout.timeout is not None else 300.0,
            connect=timeout.connect if timeout.connect is not None else 20.0,
            read=timeout.read if timeout.read is not None else 300.0,
            write=timeout.write if timeout.write is not None else 300.0,
        )

    @staticmethod
    def _coerce_message(
        message: Any,
        *,
        metadata: Optional[Mapping[str, Any]],
        context_id: Optional[str],
        task_id: Optional[str],
    ) -> dict[str, Any]:
        if isinstance(message, Mapping):
            resolved = dict(message)
        elif isinstance(message, str):
            resolved = {
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
            }
        else:
            resolved = {
                "role": "user",
                "parts": [{"kind": "text", "text": str(message)}],
            }

        resolved.setdefault("role", "user")
        resolved.setdefault("messageId", uuid4().hex)
        if context_id:
            resolved["contextId"] = context_id
        if task_id:
            resolved["taskId"] = task_id
        if metadata:
            existing = resolved.get("metadata")
            if isinstance(existing, Mapping):
                resolved["metadata"] = {**dict(existing), **dict(metadata)}
            else:
                resolved["metadata"] = dict(metadata)
        return resolved

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, Mapping):
            return {str(key): A2ASendMessageRowFunction._jsonable(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [A2ASendMessageRowFunction._jsonable(item) for item in value]
        if hasattr(value, "model_dump"):
            return A2ASendMessageRowFunction._jsonable(value.model_dump())
        if hasattr(value, "dict"):
            return A2ASendMessageRowFunction._jsonable(value.dict())
        return str(value)

    @staticmethod
    def _text_from_parts(parts: Any) -> str:
        if not isinstance(parts, list):
            return ""
        lines: list[str] = []
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
            elif "data" in part:
                lines.append(str(part["data"]).strip())
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _extract_final_text(payload: Mapping[str, Any]) -> str | None:
        result = payload.get("result")
        if not isinstance(result, Mapping):
            return None
        task = result.get("task")
        if not isinstance(task, Mapping):
            task = result
        if not isinstance(task, Mapping):
            return None

        artifacts = task.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                text = A2ASendMessageRowFunction._text_from_parts(artifact.get("parts"))
                if text:
                    return text

        status = task.get("status")
        if isinstance(status, Mapping):
            message = status.get("message")
            if isinstance(message, Mapping):
                text = A2ASendMessageRowFunction._text_from_parts(message.get("parts"))
                if text:
                    return text

        history = task.get("history")
        if isinstance(history, list):
            for message in reversed(history):
                if not isinstance(message, Mapping):
                    continue
                role = str(message.get("role") or "").lower()
                if role and "agent" not in role and "assistant" not in role:
                    continue
                text = A2ASendMessageRowFunction._text_from_parts(message.get("parts"))
                if text:
                    return text
        return None

    @staticmethod
    def _with_final_text(payload: dict[str, Any]) -> dict[str, Any]:
        if "final_text" in payload:
            return payload
        final_text = A2ASendMessageRowFunction._extract_final_text(payload)
        if not final_text:
            return payload
        return {**payload, "final_text": final_text}

    @staticmethod
    async def _send_json_rpc(
        *,
        base_url: str,
        headers: Mapping[str, str],
        request_id: str,
        message_payload: Mapping[str, Any],
        timeout: TimeoutConfig,
    ) -> dict[str, Any]:
        import httpx

        normalized_base_url = base_url.rstrip("/")
        endpoint_suffix = "/a2a"
        if normalized_base_url.endswith(endpoint_suffix):
            agent_url = normalized_base_url
            discovery_base_url = normalized_base_url[: -len(endpoint_suffix)] or normalized_base_url
        else:
            agent_url = f"{normalized_base_url}{endpoint_suffix}"
            discovery_base_url = normalized_base_url
        async with httpx.AsyncClient(
            headers=dict(headers),
            timeout=A2ASendMessageRowFunction._httpx_timeout(httpx, timeout),
        ) as httpx_client:
            for card_path in ("/.well-known/agent-card.json", "/.well-known/agent.json", "/agent"):
                try:
                    card_response = await httpx_client.get(f"{discovery_base_url}{card_path}")
                    if card_response.status_code >= 400:
                        continue
                    card_payload = card_response.json()
                    if isinstance(card_payload, Mapping) and card_payload.get("url"):
                        agent_url = str(card_payload["url"])
                    elif isinstance(card_payload, Mapping):
                        interfaces = card_payload.get("supportedInterfaces")
                        if isinstance(interfaces, list):
                            for interface in interfaces:
                                if isinstance(interface, Mapping) and interface.get("url"):
                                    agent_url = str(interface["url"])
                                    break
                    break
                except Exception:
                    continue

            response = await httpx_client.post(
                agent_url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "SendMessage",
                    "params": {"message": dict(message_payload)},
                },
            )
            try:
                payload = response.json()
            except Exception:
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": response.status_code,
                        "message": read_attr(response, "text", ""),
                    },
                }
            if response.status_code >= 400:
                if isinstance(payload, dict) and "error" in payload:
                    return payload
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": response.status_code,
                        "message": read_attr(response, "text", ""),
                        "data": payload,
                    },
                }
            if not isinstance(payload, dict):
                raise RuntimeError("A2A JSON-RPC response must be a JSON object")
            return A2ASendMessageRowFunction._with_final_text(payload)

    async def transform(
        self,
        aisystem_id: Optional[str],
        message: Any,
        metadata: Optional[Mapping[str, Any]] = None,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        secret_id: Optional[str] = None,
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> Optional[dict[str, Any]]:
        if not aisystem_id:
            return None

        resolved = self._resolver.resolve_a2a_payload(
            aisystem_id=aisystem_id,
            secret_id=secret_id,
        )
        selected_secret = resolved.secret_id
        headers = resolve_a2a_headers_from_secret(
            selected_secret,
            self._runtime_context.as_options() if self._runtime_context is not None else None,
        )
        resolved_timeout = self._timeout(timeout)
        message_payload = self._coerce_message(
            message,
            metadata=metadata,
            context_id=context_id,
            task_id=task_id,
        )

        request_id = str(uuid4())
        return await self._send_json_rpc(
            base_url=resolved.base_url,
            headers=headers,
            request_id=request_id,
            message_payload=message_payload,
            timeout=resolved_timeout,
        )


class AISystemsA2ASendMessageUdf(Udf, name="aisystems.a2a.send_message"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            JsonType(),
            JsonType(),
            StringType(),
            StringType(),
            StringType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("aisystem_id", required=True),
            Param("message", required=True),
            Param("metadata", required=False),
            Param("context_id", required=False),
            Param("task_id", required=False),
            Param("secret_id", required=False),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return A2ASendMessageRowFunction()
