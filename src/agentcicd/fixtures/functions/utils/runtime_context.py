from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from agentcicd.fixtures.functions.utils.resource_records import (
    normalize_aisystem_records,
    normalize_aisystem_secret_bindings,
    normalize_secret_id_list,
    normalize_secret_records,
)
try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside Spark image
    object_store_from_env = None  # type: ignore[assignment]

API_KEY_COMPATIBLE_PROVIDERS = {
    "openai",
    "azure",
    "anthropic",
    "gemini",
    "google",
    "vertex_ai",
    "groq",
    "mistral",
    "mistral_ai",
    "cohere",
    "together",
    "together_ai",
    "xai",
    "deepseek",
    "perplexity",
    "openrouter",
    "fireworks_ai",
    "cerebras",
    "nvidia_nim",
    "databricks",
}
NON_API_KEY_PROVIDERS = {"ollama"}
AGENT_HARNESS_COMPATIBLE_INTERFACES = {"llm.chat", "llm.responses", "llm.messages"}


@dataclass(frozen=True)
class AISystemRuntimeRecord:
    id: str
    target: str
    interface_type: str
    name: str = ""
    interface: Mapping[str, Any] | None = None
    interfaces: tuple[Mapping[str, Any], ...] = ()
    config: Mapping[str, Any] | None = None
    secret_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AISystemSecretBindingRuntimeRecord:
    id: str
    organization_id: str
    aisystem_id: str
    secret_id: str
    is_default: bool = False
    status: str = "active"


@dataclass(frozen=True)
class SecretRuntimeRecord:
    id: str
    key: str
    secret: Mapping[str, Any]
    litellm_options: Mapping[str, Any]


@dataclass(frozen=True)
class LiteLLMPayload:
    model: str
    secret_id: str | None


@dataclass(frozen=True)
class A2APayload:
    base_url: str
    secret_id: str | None


@dataclass(frozen=True)
class AgentHarnessPayload:
    harness: str
    config: Mapping[str, Any]
    auth: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RuntimeResolutionContext:
    aisystems_by_id: Mapping[str, AISystemRuntimeRecord]
    secrets_by_id: Mapping[str, SecretRuntimeRecord]
    secrets_by_key: Mapping[str, SecretRuntimeRecord]
    aisystem_secret_bindings: tuple[AISystemSecretBindingRuntimeRecord, ...] = ()
    secret_ids: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "RuntimeResolutionContext":
        context = _load_context(_context_path_from_env())
        return cls.from_records(
            aisystems=context.get("aisystems_by_id") or context.get("aisystems") or {},
            secrets=context.get("secrets") or [],
            aisystem_secret_bindings=context.get("aisystem_secret_bindings") or [],
            secret_ids=context.get("secret_ids") or [],
        )

    @classmethod
    def from_records(
        cls,
        *,
        aisystems: Mapping[str, Any] | list[Any],
        secrets: Mapping[str, Any] | list[Any],
        aisystem_secret_bindings: Mapping[str, Any] | list[Any] | None = None,
        secret_ids: list[Any] | tuple[Any, ...] | None = None,
    ) -> "RuntimeResolutionContext":
        aisystems_by_id = normalize_aisystem_records(
            aisystems,
            record_factory=AISystemRuntimeRecord,
        )
        secrets_by_id, secrets_by_key = normalize_secret_records(
            secrets,
            record_factory=SecretRuntimeRecord,
            litellm_options_factory=_litellm_options_from_secret,
        )
        return cls(
            aisystems_by_id=aisystems_by_id,
            secrets_by_id=secrets_by_id,
            secrets_by_key=secrets_by_key,
            aisystem_secret_bindings=normalize_aisystem_secret_bindings(
                aisystem_secret_bindings,
                record_factory=AISystemSecretBindingRuntimeRecord,
            ),
            secret_ids=normalize_secret_id_list(secret_ids),
        )

    def as_options(self) -> dict[str, Any]:
        aisystems = {
            key: {
                "id": record.id,
                "name": record.name,
                "target": record.target,
                "interface_type": record.interface_type,
                "interface": dict(record.interface or {}),
                "interfaces": [dict(item) for item in record.interfaces],
                "config": dict(record.config or {}),
                "secret_ids": list(record.secret_ids),
            }
            for key, record in self.aisystems_by_id.items()
        }
        secrets_by_id = {
            key: {
                "id": record.id,
                "key": record.key,
                "secret": dict(record.secret),
                "litellm_options": dict(record.litellm_options),
            }
            for key, record in self.secrets_by_id.items()
        }
        secrets_by_key = {
            key: {
                "id": record.id,
                "key": record.key,
                "secret": dict(record.secret),
                "litellm_options": dict(record.litellm_options),
            }
            for key, record in self.secrets_by_key.items()
        }
        return {
            "aisystems_by_id": aisystems,
            "secrets_by_id": secrets_by_id,
            "secrets_by_key": secrets_by_key,
            "aisystem_secret_bindings": [
                {
                    "id": record.id,
                    "organization_id": record.organization_id,
                    "aisystem_id": record.aisystem_id,
                    "secret_id": record.secret_id,
                    "is_default": record.is_default,
                    "status": record.status,
                }
                for record in self.aisystem_secret_bindings
            ],
            "secret_ids": list(self.secret_ids),
        }


class AISystemRuntimeResolver:
    def __init__(self, context: RuntimeResolutionContext | None = None) -> None:
        self.context = context

    @property
    def _options(self) -> Mapping[str, Any] | None:
        return self.context.as_options() if self.context is not None else None

    def resolve_aisystem(self, aisystem_id: str, expected_interface_type: str) -> AISystemRuntimeRecord:
        if self.context is not None:
            record = self.context.aisystems_by_id.get(aisystem_id)
            if record is None:
                raise ValueError(f"AI system not found: {aisystem_id}")
            selected_interface_type = _select_interface_type(record, expected_interface_type)
            if selected_interface_type != expected_interface_type:
                raise ValueError(
                    f"AI system '{aisystem_id}' has interface '{selected_interface_type}', expected '{expected_interface_type}'."
                )
            return _record_with_selected_interface(record, expected_interface_type)
        resolved = resolve_aisystem(aisystem_id, expected_interface_type=expected_interface_type)
        if not resolved:
            raise ValueError(f"AI system not found: {aisystem_id}")
        interface_type = str(resolved.get("interface_type") or "").strip()
        interface = resolved.get("interface")
        if not interface_type and isinstance(interface, Mapping):
            interface_type = str(interface.get("interface_type") or interface.get("interfaceType") or "").strip()
        return AISystemRuntimeRecord(
            id=aisystem_id,
            target=str(resolved.get("target") or ""),
            interface_type=interface_type,
            name=str(resolved.get("name") or ""),
            interface=dict(interface) if isinstance(interface, Mapping) else {},
            interfaces=tuple(dict(item) for item in resolved.get("interfaces", []) if isinstance(item, Mapping)),
            config=dict(resolved.get("config") or {}) if isinstance(resolved.get("config"), Mapping) else {},
            secret_ids=tuple(str(value).strip() for value in resolved.get("secret_ids", []) if str(value).strip())
            if isinstance(resolved.get("secret_ids"), list)
            else (),
        )

    def resolve_litellm_payload(
        self,
        aisystem_id: str,
        expected_interface_type: str,
        secret_id: str | None,
    ) -> LiteLLMPayload:
        payload = resolve_litellm_payload_from_aisystem(
            aisystem_id=aisystem_id,
            expected_interface_type=expected_interface_type,
            secret_id_or_key=secret_id,
            options=self._options,
        )
        return LiteLLMPayload(
            model=str(payload["model"]),
            secret_id=str(payload["secret_id"]) if payload.get("secret_id") else None,
        )

    def resolve_a2a_payload(self, aisystem_id: str, secret_id: str | None) -> A2APayload:
        payload = resolve_a2a_payload_from_aisystem(
            aisystem_id=aisystem_id,
            secret_id_or_key=secret_id,
            options=self._options,
        )
        return A2APayload(
            base_url=str(payload["base_url"]),
            secret_id=str(payload["secret_id"]) if payload.get("secret_id") else None,
        )

    def resolve_agent_harness_payload(self, aisystem_id: str, secret_id: str | None = None) -> AgentHarnessPayload:
        payload = resolve_agent_harness_payload_from_aisystem(
            aisystem_id=aisystem_id,
            secret_id_or_key=secret_id,
            options=self._options,
        )
        return AgentHarnessPayload(
            harness=str(payload["harness"]),
            config=dict(payload.get("config") or {}),
            auth=dict(payload["auth"]) if isinstance(payload.get("auth"), Mapping) else None,
        )


def _select_interface_type(record: AISystemRuntimeRecord, expected_interface_type: str) -> str:
    expected = expected_interface_type.strip()
    for interface in record.interfaces:
        actual = str(interface.get("interface_type") or interface.get("interfaceType") or "").strip()
        if actual == expected:
            return actual
    return record.interface_type


def _record_with_selected_interface(record: AISystemRuntimeRecord, expected_interface_type: str) -> AISystemRuntimeRecord:
    for interface in record.interfaces:
        actual = str(interface.get("interface_type") or interface.get("interfaceType") or "").strip()
        if actual == expected_interface_type:
            return AISystemRuntimeRecord(
                id=record.id,
                target=record.target,
                interface_type=actual,
                name=record.name,
                interface=dict(interface),
                interfaces=record.interfaces,
                config=record.config,
                secret_ids=record.secret_ids,
            )
    return record


def _context_path_from_spark_files() -> str:
    try:
        from pyspark import SparkFiles  # type: ignore

        candidate = SparkFiles.get("agentcicd_fixture_context.json")
        if candidate and Path(candidate).exists():
            return candidate
    except Exception:
        pass
    return ""


def _context_path_from_env() -> str:
    spark_local = _context_path_from_spark_files()
    if spark_local:
        return spark_local
    direct = (os.getenv("AGENTCICD_FIXTURE_CONTEXT_PATH") or "").strip()
    if direct:
        return direct
    run_dir = (os.getenv("AGENTCICD_RUN_DIR") or "").strip()
    if run_dir:
        return str(Path(run_dir) / "fixtures" / "context.enriched.json")
    return ""


def _context_uri_from_env() -> str:
    direct = (os.getenv("AGENTCICD_FIXTURE_CONTEXT_URI") or "").strip()
    if direct:
        return direct
    run_object_uri = (os.getenv("AGENTCICD_RUN_OBJECT_URI") or "").strip()
    if run_object_uri:
        return f"{run_object_uri.rstrip('/')}/fixture-context/context.enriched.json"
    return ""


@lru_cache(maxsize=4)
def _load_context(path: str) -> dict[str, Any]:
    uri = _context_uri_from_env()
    if uri and object_store_from_env is not None:
        try:
            payload = object_store_from_env().get_json(uri)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            pass
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_litellm_options_from_secret(
    secret_id_or_key: str | None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = resolve_secret_record(secret_id_or_key, options)
    if not record:
        return {}
    return _secret_entry_to_litellm_options(record)


def resolve_secret_record(
    secret_id_or_key: str | None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(secret_id_or_key, str) or not secret_id_or_key.strip():
        return {}
    key = secret_id_or_key.strip()

    if isinstance(options, Mapping):
        by_id = options.get("secrets_by_id")
        if isinstance(by_id, Mapping) and isinstance(by_id.get(key), Mapping):
            return dict(by_id.get(key) or {})
        by_key = options.get("secrets_by_key")
        if isinstance(by_key, Mapping) and isinstance(by_key.get(key), Mapping):
            return dict(by_key.get(key) or {})

    context = _load_context(_context_path_from_env())
    for item in context.get("secrets") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("id") == key or item.get("key") == key:
            return dict(item)
    return {}


def merge_litellm_payload_with_secret(
    payload: dict[str, Any],
    secret_id_or_key: str | None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    secret_options = resolve_litellm_options_from_secret(secret_id_or_key, options)
    if not secret_options:
        return payload

    merged = dict(payload)
    provider = _infer_model_provider(merged.get("model"))
    for key, value in secret_options.items():
        if key == "api_key":
            if _provider_uses_api_key(provider):
                existing_api_key = merged.get("api_key")
                if existing_api_key is None or (isinstance(existing_api_key, str) and not existing_api_key.strip()):
                    merged["api_key"] = value
            continue
        if key == "extra_headers" and isinstance(value, Mapping):
            existing = merged.get("extra_headers")
            if isinstance(existing, Mapping):
                merged["extra_headers"] = {**value, **existing}
            else:
                merged["extra_headers"] = dict(value)
            continue
        existing_value = merged.get(key)
        if existing_value is None or (isinstance(existing_value, str) and not existing_value.strip()):
            merged[key] = value
    return merged


def _aisystem_item_by_id(
    aisystem_id: str,
    items: Any,
) -> dict[str, Any]:
    if isinstance(items, Mapping):
        direct = items.get(aisystem_id)
        if isinstance(direct, Mapping):
            return dict(direct)
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if item.get("id") == aisystem_id:
                return dict(item)
    return {}


def resolve_aisystem(
    aisystem_id: str | None,
    expected_interface_type: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(aisystem_id, str) or not aisystem_id.strip():
        return {}
    key = aisystem_id.strip()

    candidate_groups: list[Any] = []
    if isinstance(options, Mapping):
        candidate_groups.extend([options.get("aisystems_by_id"), options.get("aisystems")])

    context = _load_context(_context_path_from_env())
    candidate_groups.extend([context.get("aisystems_by_id"), context.get("aisystems")])

    aisystem_record: dict[str, Any] = {}
    for group in candidate_groups:
        aisystem_record = _aisystem_item_by_id(key, group)
        if aisystem_record:
            break

    if not aisystem_record:
        return {}

    interface = aisystem_record.get("interface")
    interfaces = aisystem_record.get("interfaces")
    interface_type = None

    def _matches_expected(actual: str | None, expected: str | None) -> bool:
        if not expected:
            return True
        if not isinstance(actual, str) or not actual.strip():
            return False
        normalized_actual = actual.strip()
        normalized_expected = expected.strip()
        if normalized_actual == normalized_expected:
            return True
        if normalized_actual == "http" and normalized_expected in {"http.get", "http.post"}:
            return True
        return False

    if isinstance(interface, Mapping):
        interface_type = interface.get("interface_type") or interface.get("interfaceType")
    if expected_interface_type and isinstance(interfaces, list):
        matched_interface = next(
            (
                dict(item)
                for item in interfaces
                if isinstance(item, Mapping)
                and _matches_expected(
                    item.get("interface_type") or item.get("interfaceType"),
                    expected_interface_type,
                )
            ),
            None,
        )
        if matched_interface is not None:
            aisystem_record["interface"] = matched_interface
            interface = matched_interface
            interface_type = matched_interface.get("interface_type") or matched_interface.get("interfaceType")
    if (
        expected_interface_type
        and isinstance(interface_type, str)
        and interface_type.strip()
        and not _matches_expected(interface_type, expected_interface_type)
    ):
        raise ValueError(
            f"AI system '{key}' has interface '{interface_type}', expected '{expected_interface_type}'."
        )
    return aisystem_record


def resolve_litellm_payload_from_aisystem(
    *,
    aisystem_id: str | None,
    expected_interface_type: str,
    secret_id_or_key: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    aisystem = resolve_aisystem(
        aisystem_id=aisystem_id,
        expected_interface_type=expected_interface_type,
        options=options,
    )
    if not aisystem:
        raise ValueError(f"AI system not found: {aisystem_id}")

    model = aisystem.get("target")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"AI system '{aisystem_id}' does not define a usable target")

    canonical_model = _canonicalize_litellm_model(model.strip())
    if isinstance(secret_id_or_key, str) and secret_id_or_key.strip():
        selected_secret = _resolve_available_secret_id(secret_id_or_key, options)
        if selected_secret is None:
            raise ValueError(f"Secret not available in runtime context: {secret_id_or_key}")
    else:
        selected_secret = _bound_secret_id_for_aisystem(str(aisystem_id or ""), options)
    if selected_secret is None and _provider_uses_api_key(_infer_model_provider(canonical_model)):
        raise ValueError(f"AI system '{aisystem_id}' requires an org secret binding or direct secret_id")

    return {
        "model": canonical_model,
        "secret_id": selected_secret,
    }


def resolve_a2a_payload_from_aisystem(
    *,
    aisystem_id: str | None,
    secret_id_or_key: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    aisystem = resolve_aisystem(
        aisystem_id=aisystem_id,
        expected_interface_type="agent_a2a",
        options=options,
    )
    if not aisystem:
        raise ValueError(f"AI system not found: {aisystem_id}")

    base_url = aisystem.get("target")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError(f"AI system '{aisystem_id}' does not define a usable A2A base URL target")

    selected_secret = None
    if isinstance(secret_id_or_key, str) and secret_id_or_key.strip():
        selected_secret = _resolve_available_secret_id(secret_id_or_key, options)
        if selected_secret is None:
            raise ValueError(f"Secret not available in runtime context: {secret_id_or_key}")
    else:
        selected_secret = _bound_secret_id_for_aisystem(str(aisystem_id or ""), options)

    return {
        "base_url": base_url.strip().rstrip("/"),
        "secret_id": selected_secret,
    }


def resolve_agent_harness_payload_from_aisystem(
    *,
    aisystem_id: str | None,
    secret_id_or_key: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    aisystem = resolve_aisystem(aisystem_id=aisystem_id, options=options)
    if not aisystem:
        raise ValueError(f"AI system not found: {aisystem_id}")

    interface = _select_agent_harness_compatible_interface(aisystem)
    config = _agent_harness_config_from_record(aisystem, interface)
    harness = str(
        config.pop("harness", None)
        or config.pop("adapter", None)
        or interface.get("harness")
        or interface.get("adapter")
        or ""
    ).strip().lower()
    target = str(aisystem.get("target") or "").strip()
    if not harness:
        harness = _infer_agent_harness_from_target(target)
    if not harness:
        harness = "codex"
    selected_secret = None
    if isinstance(secret_id_or_key, str) and secret_id_or_key.strip():
        selected_secret = _resolve_available_secret_id(secret_id_or_key, options)
        if selected_secret is None:
            raise ValueError(f"Secret not available in runtime context: {secret_id_or_key}")
    auth = _agent_harness_auth_payload(selected_secret, options)
    if auth is not None:
        config["auth"] = auth
        env = auth.get("env")
        if isinstance(env, Mapping):
            existing_env = config.get("env")
            config["env"] = {**dict(env), **(dict(existing_env) if isinstance(existing_env, Mapping) else {})}

    config["aisystem_id"] = str(aisystem_id or "")
    return {
        "harness": harness,
        "config": config,
        "auth": auth,
    }


def _agent_harness_config_from_record(
    aisystem: Mapping[str, Any],
    interface: Mapping[str, Any],
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for source in (
        interface.get("config"),
        interface.get("runtime_config"),
        interface.get("harness_config"),
        aisystem.get("config"),
        aisystem.get("runtime_config"),
        aisystem.get("harness_config"),
    ):
        if isinstance(source, Mapping):
            config.update(dict(source))
    target = str(aisystem.get("target") or "").strip()
    if target and target != str(config.get("harness") or config.get("adapter") or "").strip():
        if target.startswith("codex:") and "model" not in config:
            config["model"] = target.split(":", 1)[1].strip()
            config.setdefault("harness", "codex")
        elif target and "model" not in config:
            config["model"] = _canonical_agent_harness_model(target)
    if isinstance(config.get("model"), str):
        config["model"] = _canonical_agent_harness_model(str(config["model"]))
    return config


def _select_agent_harness_compatible_interface(aisystem: Mapping[str, Any]) -> Mapping[str, Any]:
    interfaces = aisystem.get("interfaces")
    if isinstance(interfaces, list):
        for item in interfaces:
            if not isinstance(item, Mapping):
                continue
            interface_type = str(item.get("interface_type") or item.get("interfaceType") or "").strip()
            if interface_type in AGENT_HARNESS_COMPATIBLE_INTERFACES:
                return dict(item)
    interface = aisystem.get("interface") if isinstance(aisystem.get("interface"), Mapping) else {}
    interface_type = str(interface.get("interface_type") or interface.get("interfaceType") or aisystem.get("interface_type") or "").strip()
    if interface_type not in AGENT_HARNESS_COMPATIBLE_INTERFACES:
        raise ValueError(
            f"AI system '{aisystem.get('id')}' has interface '{interface_type or 'unspecified'}', "
            f"expected one of {sorted(AGENT_HARNESS_COMPATIBLE_INTERFACES)}."
        )
    return dict(interface)


def _infer_agent_harness_from_target(target: str) -> str:
    normalized = target.strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("codex:") or normalized == "codex" or "codex" in normalized:
        return "codex"
    return ""


def _canonical_agent_harness_model(target: str) -> str:
    text = target.strip()
    if text.startswith("codex:"):
        return text.split(":", 1)[1].strip()
    normalized = text.replace(":", "/", 1)
    if "/" in normalized:
        provider, model = normalized.split("/", 1)
        if provider.strip().lower() == "openai" and model.strip():
            return model.strip()
    return text


def _agent_harness_auth_payload(secret_id: str | None, options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not secret_id:
        return None
    payload: dict[str, Any] = {"type": "secret_ref", "secret_id": secret_id}
    secret_record = resolve_secret_record(secret_id, options)
    if not secret_record:
        return payload
    secret = secret_record.get("secret") if isinstance(secret_record.get("secret"), Mapping) else secret_record
    env = secret.get("env") if isinstance(secret, Mapping) else None
    if isinstance(env, Mapping):
        payload["env"] = {str(key): str(value) for key, value in env.items()}
    secret_options = _secret_entry_to_litellm_options(secret_record)
    api_key = secret_options.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        payload_env = dict(payload.get("env") or {})
        payload_env.setdefault("CODEX_API_KEY", api_key.strip())
        payload["env"] = payload_env
    raw_payload = _raw_secret_json_payload(secret)
    if raw_payload is not None:
        raw_env = raw_payload.get("env")
        if isinstance(raw_env, Mapping):
            payload_env = dict(payload.get("env") or {})
            payload_env.update({str(key): str(value) for key, value in raw_env.items()})
            payload["env"] = payload_env
        codex_home_files = _string_mapping(raw_payload.get("codex_home_files"))
        if codex_home_files:
            payload["codex_home_files"] = codex_home_files
        for source_key, payload_key in (
            ("codex_home", "codex_home"),
            ("CODEX_HOME", "codex_home"),
            ("codex_auth_file", "codex_auth_file"),
            ("CODEX_AUTH_FILE", "codex_auth_file"),
        ):
            value = raw_payload.get(source_key)
            if isinstance(value, str) and value.strip():
                payload[payload_key] = value.strip()
    for source_key, payload_key in (
        ("codex_home", "codex_home"),
        ("CODEX_HOME", "codex_home"),
        ("codex_auth_file", "codex_auth_file"),
        ("CODEX_AUTH_FILE", "codex_auth_file"),
    ):
        value = secret.get(source_key) if isinstance(secret, Mapping) else None
        if isinstance(value, str) and value.strip():
            payload[payload_key] = value.strip()
    return payload


def _raw_secret_json_payload(secret: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(secret, Mapping):
        return None
    if str(secret.get("type") or "").strip().lower() != "raw":
        return None
    raw_value = secret.get("value")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and key.strip() and isinstance(item, str):
            result[key.strip()] = item
    return result


def _resolve_available_secret_id(
    secret_id_or_key: str | None,
    options: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(secret_id_or_key, str) or not secret_id_or_key.strip():
        return None
    selected = secret_id_or_key.strip()
    if isinstance(options, Mapping):
        run_secret_ids = options.get("secret_ids")
        if isinstance(run_secret_ids, list) and selected in {str(value).strip() for value in run_secret_ids}:
            return selected
        by_id = options.get("secrets_by_id") or options.get("secrets")
        if isinstance(by_id, Mapping) and isinstance(by_id.get(selected), Mapping):
            return str(by_id[selected].get("id") or selected)
        by_key = options.get("secrets_by_key")
        if isinstance(by_key, Mapping) and isinstance(by_key.get(selected), Mapping):
            selected_record = by_key[selected]
            resolved_id = selected_record.get("id")
            return str(resolved_id).strip() if isinstance(resolved_id, str) and resolved_id.strip() else None
    context = _load_context(_context_path_from_env())
    run_secret_ids = context.get("secret_ids")
    if isinstance(run_secret_ids, list) and selected in {str(value).strip() for value in run_secret_ids}:
        return selected
    secrets = context.get("secrets")
    secret_values = secrets.values() if isinstance(secrets, Mapping) else secrets
    if isinstance(secret_values, list) or not isinstance(secret_values, Mapping):
        for item in secret_values or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("id") == selected or item.get("key") == selected:
                resolved_id = item.get("id")
                return str(resolved_id).strip() if isinstance(resolved_id, str) and resolved_id.strip() else None
    return None


def _bound_secret_id_for_aisystem(aisystem_id: str, options: Mapping[str, Any] | None) -> str | None:
    bindings: Any = None
    if isinstance(options, Mapping):
        bindings = options.get("aisystem_secret_bindings")
    if bindings is None:
        context = _load_context(_context_path_from_env())
        bindings = context.get("aisystem_secret_bindings")
    binding_values = bindings.values() if isinstance(bindings, Mapping) else bindings
    for item in binding_values or []:
        if not isinstance(item, Mapping):
            continue
        item_aisystem_id = str(item.get("aisystem_id") or item.get("aisystemId") or "").strip()
        if item_aisystem_id != aisystem_id:
            continue
        if str(item.get("status") or "active") != "active":
            continue
        if not bool(item.get("is_default") or item.get("isDefault")):
            continue
        secret_id = str(item.get("secret_id") or item.get("secretId") or "").strip()
        if secret_id:
            return secret_id
    return None


def resolve_a2a_headers_from_secret(
    secret_id_or_key: str | None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    record = resolve_secret_record(secret_id_or_key, options)
    if not record:
        return {}

    secret = record.get("secret") if isinstance(record.get("secret"), Mapping) else record
    secret_type = str(secret.get("type") or "").strip().lower()
    headers: dict[str, str] = {}
    if secret_type == "bearer":
        token = secret.get("token")
        if isinstance(token, str) and token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
            return headers

    options_payload = _secret_entry_to_litellm_options(record)
    extra_headers = options_payload.get("extra_headers")
    if isinstance(extra_headers, Mapping):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
    api_key = options_payload.get("api_key")
    if isinstance(api_key, str) and api_key.strip() and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return headers


def _secret_entry_to_litellm_options(entry: Mapping[str, Any]) -> dict[str, Any]:
    ready = entry.get("litellm_options")
    if isinstance(ready, Mapping):
        return dict(ready)

    secret = entry.get("secret")
    if isinstance(secret, Mapping):
        secret_map = secret
    else:
        secret_map = entry

    return _litellm_options_from_secret(secret_map)


def _litellm_options_from_secret(secret: Mapping[str, Any]) -> dict[str, Any]:
    secret_type = str(secret.get("type") or "").strip().lower()
    options: dict[str, Any] = {}

    direct_api_key = secret.get("api_key")
    if isinstance(direct_api_key, str) and direct_api_key.strip():
        options["api_key"] = direct_api_key.strip()
        return options

    api_key_from_env = secret.get("api_key_from_env")
    if isinstance(api_key_from_env, str) and api_key_from_env.strip():
        env_value = os.getenv(api_key_from_env.strip())
        if isinstance(env_value, str) and env_value.strip():
            options["api_key"] = env_value.strip()
            return options

    if secret_type == "api_key":
        api_key = secret.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            options["api_key"] = api_key.strip()
        return options

    if secret_type == "raw":
        raw_value = secret.get("value")
        if isinstance(raw_value, str) and raw_value.strip():
            raw_text = raw_value.strip()
            try:
                raw_json = json.loads(raw_text)
            except Exception:
                raw_json = None
            if isinstance(raw_json, Mapping):
                api_key = raw_json.get("api_key")
                if isinstance(api_key, str) and api_key.strip():
                    options["api_key"] = api_key.strip()
            else:
                options["api_key"] = raw_text
        return options

    return options


def _infer_model_provider(model: Any) -> str | None:
    if not isinstance(model, str):
        return None
    text = model.strip().lower()
    if not text:
        return None

    if "/" in text:
        prefix = text.split("/", 1)[0].strip()
        return prefix or None
    if ":" in text:
        prefix = text.split(":", 1)[0].strip()
        if prefix in (API_KEY_COMPATIBLE_PROVIDERS | NON_API_KEY_PROVIDERS):
            return prefix

    if text.startswith(("gpt-", "o1", "o3", "chatgpt-", "text-embedding-")):
        return "openai"
    if text.startswith("claude"):
        return "anthropic"
    if text.startswith("gemini"):
        return "gemini"
    return None


def _canonicalize_litellm_model(model: Any) -> str:
    if not isinstance(model, str):
        return ""
    text = model.strip()
    if not text:
        return ""
    if "/" in text:
        return text

    provider = _infer_model_provider(text)
    if not provider:
        return text
    return f"{provider}/{text}"


def _provider_uses_api_key(provider: str | None) -> bool:
    if provider is None:
        return True
    normalized = provider.strip().lower()
    if normalized in NON_API_KEY_PROVIDERS:
        return False
    return normalized in API_KEY_COMPATIBLE_PROVIDERS


def _fixture_item_by_id(
    fixture_id: str,
    items: Any,
) -> dict[str, Any]:
    if isinstance(items, Mapping):
        direct = items.get(fixture_id)
        if isinstance(direct, Mapping):
            return dict(direct)
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            fixture_obj = item.get("fixture")
            image_obj = item.get("image")
            if isinstance(fixture_obj, Mapping) and isinstance(image_obj, Mapping):
                fixture_match = fixture_obj.get("id") == fixture_id
                image_match = image_obj.get("id") == fixture_id
                fixture_id_match = image_obj.get("fixture_id") == fixture_id
                if fixture_match or image_match or fixture_id_match:
                    runtime = image_obj.get("runtime")
                    if not isinstance(runtime, Mapping):
                        runtime = {}
                    merged: dict[str, Any] = {
                        "id": image_obj.get("id") or fixture_obj.get("id"),
                        "fixture_id": image_obj.get("fixture_id") or fixture_obj.get("id"),
                        "image_id": image_obj.get("id"),
                        "cluster_id": image_obj.get("cluster_id"),
                        "kind": fixture_obj.get("kind"),
                        "fixture_kind": fixture_obj.get("kind"),
                        "status": image_obj.get("status") or fixture_obj.get("status"),
                        "config": image_obj.get("config") or fixture_obj.get("config") or {},
                        "runtime": dict(runtime),
                    }
                    runtime_base = runtime.get("base_url") or runtime.get("api_base")
                    if isinstance(runtime_base, str) and runtime_base.strip():
                        merged["base_url"] = runtime_base
                        merged["api_base"] = runtime_base
                    return merged
            if (
                item.get("id") == fixture_id
                or item.get("fixture_id") == fixture_id
                or item.get("image_id") == fixture_id
            ):
                return dict(item)
    return {}


def resolve_fixture_runtime(
    fixture_id: str | None,
    expected_kind: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(fixture_id, str) or not fixture_id.strip():
        return {}
    key = fixture_id.strip()

    candidate_groups: list[Any] = []
    if isinstance(options, Mapping):
        candidate_groups.extend(
            [
                options.get("fixtures_by_id"),
                options.get("fixtures_by_image_id"),
                options.get("fixtures"),
                options.get("images"),
            ]
        )

    context = _load_context(_context_path_from_env())
    candidate_groups.extend(
        [
            context.get("fixtures_by_id"),
            context.get("fixtures_by_image_id"),
            context.get("fixtures"),
            context.get("images"),
        ]
    )

    fixture_record: dict[str, Any] = {}
    for group in candidate_groups:
        fixture_record = _fixture_item_by_id(key, group)
        if fixture_record:
            break

    if not fixture_record:
        return {}

    actual_kind = fixture_record.get("kind") or fixture_record.get("fixture_kind")
    if (
        expected_kind
        and isinstance(actual_kind, str)
        and actual_kind.strip()
        and actual_kind != expected_kind
    ):
        raise ValueError(
            f"Fixture '{key}' has kind '{actual_kind}', expected '{expected_kind}'."
        )
    return fixture_record


def _extract_fixture_image_ref(fixture_record: Mapping[str, Any]) -> str | None:
    runtime = fixture_record.get("runtime")
    if isinstance(runtime, Mapping):
        runtime_image = runtime.get("image_ref") or runtime.get("container_image")
        if isinstance(runtime_image, str) and runtime_image.strip():
            return runtime_image.strip()

    config = fixture_record.get("config")
    if isinstance(config, Mapping):
        container_cfg = config.get("container")
        if isinstance(container_cfg, Mapping):
            image_ref = container_cfg.get("image_ref") or container_cfg.get("expected_image_ref")
            if isinstance(image_ref, str) and image_ref.strip():
                return image_ref.strip()

        direct_cfg = config.get("container_image") or config.get("image") or config.get("image_ref")
        if isinstance(direct_cfg, str) and direct_cfg.strip():
            return direct_cfg.strip()

    direct = fixture_record.get("container_image") or fixture_record.get("image") or fixture_record.get("image_ref")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    return None


def resolve_container_image_reference(
    image_or_fixture_id: str | None,
    options: Mapping[str, Any] | None = None,
) -> str | None:
    if not isinstance(image_or_fixture_id, str) or not image_or_fixture_id.strip():
        return None
    value = image_or_fixture_id.strip()
    fixture_record = resolve_fixture_runtime(fixture_id=value, expected_kind=None, options=options)
    if fixture_record:
        image_ref = _extract_fixture_image_ref(fixture_record)
        if image_ref:
            return image_ref
    return value
