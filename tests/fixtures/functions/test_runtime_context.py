from __future__ import annotations

import json

from agentcicd.fixtures.functions.utils.runtime_context import (
    merge_litellm_payload_with_secret,
    resolve_a2a_headers_from_secret,
    resolve_a2a_payload_from_aisystem,
    resolve_litellm_payload_from_aisystem,
    resolve_litellm_options_from_secret,
    resolve_secret_record,
    resolve_container_image_reference,
)


def test_merge_litellm_payload_with_secret_applies_api_key_for_openai_prefix() -> None:
    payload = {"model": "openai/gpt-4o"}
    merged = merge_litellm_payload_with_secret(
        payload=payload,
        secret_id_or_key="secret.1",
        options={"secrets_by_id": {"secret.1": {"api_key": "sk-test"}}},
    )
    assert merged.get("api_key") == "sk-test"


def test_merge_litellm_payload_with_secret_skips_api_key_for_ollama_prefix() -> None:
    payload = {"model": "ollama/qwen3:4b"}
    merged = merge_litellm_payload_with_secret(
        payload=payload,
        secret_id_or_key="secret.1",
        options={"secrets_by_id": {"secret.1": {"api_key": "sk-test"}}},
    )
    assert "api_key" not in merged


def test_merge_litellm_payload_with_secret_keeps_api_key_for_unprefixed_openai_model() -> None:
    payload = {"model": "gpt-4o-mini"}
    merged = merge_litellm_payload_with_secret(
        payload=payload,
        secret_id_or_key="secret.1",
        options={"secrets_by_id": {"secret.1": {"api_key": "sk-test"}}},
    )
    assert merged.get("api_key") == "sk-test"


def test_merge_litellm_payload_with_secret_keeps_api_key_for_unprefixed_claude_model() -> None:
    payload = {"model": "claude-3-haiku-20240307"}
    merged = merge_litellm_payload_with_secret(
        payload=payload,
        secret_id_or_key="secret.1",
        options={"secrets_by_id": {"secret.1": {"api_key": "sk-ant-test"}}},
    )
    assert merged.get("api_key") == "sk-ant-test"


def test_merge_litellm_payload_with_secret_applies_api_key_for_bootstrapped_model_providers() -> None:
    targets = [
        "openai/gpt-5",
        "anthropic/claude-sonnet-4-5-20250929",
        "gemini/gemini-2.5-pro",
        "xai/grok-2-latest",
        "deepseek/deepseek-chat",
        "mistral/mistral-large-latest",
        "groq/llama-3.3-70b-versatile",
        "perplexity/sonar-pro",
    ]

    for target in targets:
        merged = merge_litellm_payload_with_secret(
            payload={"model": target},
            secret_id_or_key="secret.1",
            options={"secrets_by_id": {"secret.1": {"api_key": "sk-test"}}},
        )
        assert merged.get("api_key") == "sk-test", target


def test_resolve_litellm_payload_from_aisystem_canonicalizes_unprefixed_claude_model() -> None:
    resolved = resolve_litellm_payload_from_aisystem(
        aisystem_id="aisystem.anthropic",
        expected_interface_type="llm.chat",
        options={
            "aisystems_by_id": {
                "aisystem.anthropic": {
                    "id": "aisystem.anthropic",
                    "name": "claude-3-5-haiku-latest",
                    "target": "claude-3-5-haiku-latest",
                    "interface": {"interface_type": "llm.chat"},
                }
            },
            "aisystem_secret_bindings": [
                {
                    "id": "aisystem_secret_binding.1",
                    "organization_id": "org.test",
                    "aisystem_id": "aisystem.anthropic",
                    "secret_id": "secret.1",
                    "is_default": True,
                    "status": "active",
                }
            ],
        },
    )
    assert resolved == {"model": "anthropic/claude-3-5-haiku-latest", "secret_id": "secret.1"}


def test_resolve_a2a_payload_from_aisystem_allows_no_secret() -> None:
    resolved = resolve_a2a_payload_from_aisystem(
        aisystem_id="aisystem.support",
        options={
            "aisystems_by_id": {
                "aisystem.support": {
                    "id": "aisystem.support",
                    "target": "http://localhost:8088/",
                    "interface": {"interface_type": "agent_a2a"},
                }
            }
        },
    )
    assert resolved == {"base_url": "http://localhost:8088", "secret_id": None}


def test_resolve_a2a_headers_from_bearer_secret() -> None:
    resolved = resolve_a2a_headers_from_secret(
        "secret.1",
        options={
            "secrets_by_id": {
                "secret.1": {
                    "id": "secret.1",
                    "secret": {
                        "type": "bearer",
                        "token": "token-test",
                    },
                }
            }
        },
    )
    assert resolved == {"Authorization": "Bearer token-test"}


def test_resolve_litellm_options_from_secret_reads_api_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    resolved = resolve_litellm_options_from_secret(
        "secret.1",
        options={
            "secrets_by_id": {
                "secret.1": {
                    "id": "secret.1",
                    "secret": {
                        "type": "api_key",
                        "api_key_from_env": "ANTHROPIC_API_KEY",
                    },
                }
            }
        },
    )
    assert resolved == {"api_key": "sk-ant-env"}


def test_resolve_secret_record_prefers_raw_secret_record_from_lookup_maps() -> None:
    resolved = resolve_secret_record(
        "secret.1",
        options={
            "secrets_by_id": {
                "secret.1": {
                    "id": "secret.1",
                    "key": "anthropic_key_a",
                    "secret_type": "api_key",
                    "secret": {
                        "type": "api_key",
                        "api_key": "sk-ant-a",
                    },
                }
            }
        },
    )
    assert resolved["id"] == "secret.1"
    assert resolved["secret"]["api_key"] == "sk-ant-a"


def test_resolve_litellm_options_from_context_file_uses_selected_raw_secret(monkeypatch, tmp_path) -> None:
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "secrets": [
                    {
                        "id": "secret.a",
                        "key": "anthropic_key_a",
                        "secret_type": "api_key",
                        "secret": {"type": "api_key", "api_key": "sk-ant-a"},
                    },
                    {
                        "id": "secret.b",
                        "key": "anthropic_key_b",
                        "secret_type": "api_key",
                        "secret": {"type": "api_key", "api_key": "sk-ant-b"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    resolved = resolve_litellm_options_from_secret("secret.b")
    assert resolved == {"api_key": "sk-ant-b"}


def test_context_file_end_to_end_aisystem_and_selected_secret_merge(monkeypatch, tmp_path) -> None:
    context_path = tmp_path / "context.enriched.json"
    context_path.write_text(
        json.dumps(
            {
                "secrets": [
                    {
                        "id": "secret.a",
                        "key": "anthropic_key_a",
                        "secret_type": "api_key",
                        "secret": {"type": "api_key", "api_key": "sk-ant-a"},
                    },
                    {
                        "id": "secret.b",
                        "key": "anthropic_key_b",
                        "secret_type": "api_key",
                        "secret": {"type": "api_key", "api_key": "sk-ant-b"},
                    },
                ],
                "aisystems": [
                    {
                        "id": "aisystem.anthropic",
                        "target": "claude-3-5-haiku-latest",
                        "interface": {"interface_type": "llm.chat"},
                    }
                ],
                "aisystem_secret_bindings": [
                    {
                        "id": "aisystem_secret_binding.b",
                        "organization_id": "org.test",
                        "aisystem_id": "aisystem.anthropic",
                        "secret_id": "secret.b",
                        "is_default": True,
                        "status": "active",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCICD_FIXTURE_CONTEXT_PATH", str(context_path))

    resolved = resolve_litellm_payload_from_aisystem(
        aisystem_id="aisystem.anthropic",
        expected_interface_type="llm.chat",
    )
    assert resolved == {"model": "anthropic/claude-3-5-haiku-latest", "secret_id": "secret.b"}

    merged = merge_litellm_payload_with_secret(
        {"model": resolved["model"], "messages": [{"role": "user", "content": "hello"}]},
        resolved["secret_id"],
    )
    assert merged["model"] == "anthropic/claude-3-5-haiku-latest"
    assert merged["api_key"] == "sk-ant-b"


def test_resolve_container_image_reference_uses_fixture_container_image_ref() -> None:
    resolved = resolve_container_image_reference(
        image_or_fixture_id="fixture.runtime",
        options={
            "fixtures_by_id": {
                "fixture.runtime": {
                    "kind": "container",
                    "config": {
                        "container": {
                            "image_ref": "registry.example.com/fixtures/runtime:latest",
                        }
                    },
                }
            }
        },
    )
    assert resolved == "registry.example.com/fixtures/runtime:latest"


def test_resolve_container_image_reference_falls_back_to_input_when_not_fixture() -> None:
    resolved = resolve_container_image_reference(
        image_or_fixture_id="ghcr.io/example/agent:1.0",
        options={"fixtures_by_id": {}},
    )
    assert resolved == "ghcr.io/example/agent:1.0"
