from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from agentcicd.fixtures.core.types import FType, JsonType, StringType
from agentcicd.fixtures.functions.a2a import AISystemsA2ASendMessageUdf, A2ASendMessageRowFunction
from agentcicd.fixtures.functions.utils import runtime_context


class FakeTimeout:
    def __init__(
        self,
        timeout: float,
        connect: float,
        read: float,
        write: float,
    ) -> None:
        self.timeout = timeout
        self.connect = connect
        self.read = read
        self.write = write


def test_a2a_send_message_udf_metadata() -> None:
    udf = AISystemsA2ASendMessageUdf()

    assert udf.input_args() == (
        "aisystem_id",
        "message",
        "metadata",
        "context_id",
        "task_id",
        "secret_id",
        "limiter",
    )
    assert len(udf.input_schema()) == len(udf.input_args()) - 1
    assert udf.signature()[-1].type_sql == "RATELIMIT"
    assert isinstance(udf.input_schema()[0], StringType)
    assert isinstance(udf.input_schema()[1], JsonType)
    assert isinstance(udf.output_schema(), JsonType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), A2ASendMessageRowFunction)


@pytest.mark.asyncio
async def test_a2a_send_message_accepts_endpoint_target_for_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {"card_urls": []}

    class FakeResponse:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, headers: dict[str, str] | None = None, timeout: Any = None) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            captured["card_urls"].append(url)
            return FakeResponse({"supportedInterfaces": [{"url": "http://localhost:8088/a2a"}]})

        async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
            captured["post_url"] = url
            return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": {"ok": True}})

    httpx_module = types.ModuleType("httpx")
    httpx_module.AsyncClient = FakeAsyncClient
    httpx_module.Timeout = FakeTimeout
    monkeypatch.setitem(sys.modules, "httpx", httpx_module)

    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.support",
                        "target": "http://localhost:8088/a2a",
                        "interface": {"interface_type": "agent_a2a"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime_context._load_context.cache_clear()
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    result = await A2ASendMessageRowFunction().transform(
        aisystem_id="aisystem.support",
        message="Hello",
    )

    assert captured["card_urls"][0] == "http://localhost:8088/.well-known/agent-card.json"
    assert captured["post_url"] == "http://localhost:8088/a2a"
    assert result and result["result"]["ok"] is True


@pytest.mark.asyncio
async def test_a2a_send_message_uses_raw_json_rpc_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, headers: dict[str, str] | None = None, timeout: Any = None) -> None:
            captured["headers"] = headers or {}
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            captured["card_url"] = url
            return FakeResponse(
                {
                    "supportedInterfaces": [
                        {
                            "url": "http://localhost:8088/a2a",
                            "protocolBinding": "JSONRPC",
                        }
                    ]
                }
            )

        async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
            captured["post_url"] = url
            captured["request"] = json
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "result": {
                        "task": {
                            "id": "task.1",
                            "artifacts": [
                                {
                                    "parts": [
                                        {
                                            "kind": "text",
                                            "text": "Order ORD-1001 is in transit.",
                                        }
                                    ]
                                }
                            ],
                            "metadata": {
                                "agentcicdTrace": {
                                    "format": "opentelemetry-genai",
                                    "spans": [{"name": "a2a SendMessage"}],
                                }
                            },
                        }
                    },
                }
            )

    httpx_module = types.ModuleType("httpx")
    httpx_module.AsyncClient = FakeAsyncClient
    httpx_module.Timeout = FakeTimeout
    monkeypatch.setitem(sys.modules, "httpx", httpx_module)

    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.support",
                        "target": "http://localhost:8088/",
                        "interface": {"interface_type": "agent_a2a"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime_context._load_context.cache_clear()
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    result = await A2ASendMessageRowFunction().transform(
        aisystem_id="aisystem.support",
        message="Where is order ORD-1001?",
        metadata={"customer_id": "cust_123"},
        context_id="ctx.1",
    )

    assert captured["card_url"] == "http://localhost:8088/.well-known/agent-card.json"
    assert captured["post_url"] == "http://localhost:8088/a2a"
    assert captured["timeout"].read == 300.0
    sent_message = captured["request"]["params"]["message"]
    assert sent_message["parts"] == [{"kind": "text", "text": "Where is order ORD-1001?"}]
    assert sent_message["metadata"] == {"customer_id": "cust_123"}
    assert sent_message["contextId"] == "ctx.1"
    assert result["final_text"] == "Order ORD-1001 is in transit."
    trace = result["result"]["task"]["metadata"]["agentcicdTrace"]
    assert trace["format"] == "opentelemetry-genai"
    assert trace["spans"][0]["name"] == "a2a SendMessage"


@pytest.mark.asyncio
async def test_a2a_send_message_falls_back_to_json_rpc_without_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

    class FakeAsyncClient:
        def __init__(self, headers: dict[str, str] | None = None, timeout: Any = None) -> None:
            captured["headers"] = headers or {}
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            captured["card_url"] = url
            return FakeResponse({"url": "http://localhost:8088/a2a"})

        async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
            captured["post_url"] = url
            captured["request"] = json
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "result": {"status": {"state": "completed"}},
                }
            )

    httpx_module = types.ModuleType("httpx")
    httpx_module.AsyncClient = FakeAsyncClient
    httpx_module.Timeout = FakeTimeout

    monkeypatch.delitem(sys.modules, "a2a.types", raising=False)
    monkeypatch.setitem(sys.modules, "httpx", httpx_module)

    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.support",
                        "target": "http://localhost:8088/",
                        "interface": {"interface_type": "agent_a2a"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime_context._load_context.cache_clear()
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    result = await A2ASendMessageRowFunction().transform(
        aisystem_id="aisystem.support",
        message="Hello",
        metadata={"source": "test"},
    )

    assert captured["card_url"] == "http://localhost:8088/.well-known/agent-card.json"
    assert captured["post_url"] == "http://localhost:8088/a2a"
    assert captured["timeout"].read == 300.0
    assert captured["request"]["method"] == "SendMessage"
    assert captured["request"]["params"]["message"]["parts"] == [{"kind": "text", "text": "Hello"}]
    assert captured["request"]["params"]["message"]["metadata"] == {"source": "test"}
    assert result and result["result"]["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_a2a_send_message_fallback_returns_json_rpc_http_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class FakeResponse:
        status_code = 500
        text = "Internal Server Error"

        def json(self) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": "server-id",
                "error": {"code": -32603, "message": "Internal error"},
            }

    class FakeAsyncClient:
        def __init__(self, headers: dict[str, str] | None = None, timeout: Any = None) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            raise RuntimeError("no card")

        async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
            return FakeResponse()

    httpx_module = types.ModuleType("httpx")
    httpx_module.AsyncClient = FakeAsyncClient
    httpx_module.Timeout = FakeTimeout

    monkeypatch.delitem(sys.modules, "a2a.types", raising=False)
    monkeypatch.setitem(sys.modules, "httpx", httpx_module)

    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": "aisystem.support",
                        "target": "http://localhost:8088/",
                        "interface": {"interface_type": "agent_a2a"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime_context._load_context.cache_clear()
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    result = await A2ASendMessageRowFunction().transform(
        aisystem_id="aisystem.support",
        message="Hello",
    )

    assert result == {
        "jsonrpc": "2.0",
        "id": "server-id",
        "error": {"code": -32603, "message": "Internal error"},
    }
