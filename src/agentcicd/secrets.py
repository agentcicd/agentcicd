from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentcicd.errors import ProjectLoadError

_ENV_REF_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class LocalSecretRecord:
    id: str
    organization_id: str
    key: str
    description: str | None
    secret_type: str
    value: str
    secret: dict[str, object]

    def to_runtime_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "key": self.key,
            "description": self.description,
            "secret_type": self.secret_type,
            "value": self.value,
            "secret": dict(self.secret),
        }


def load_local_secrets(project_dir: Path) -> tuple[LocalSecretRecord, ...]:
    yaml_path = project_dir / "secrets.yaml"
    properties_path = project_dir / "secret.properties"
    if yaml_path.exists() and properties_path.exists():
        raise ProjectLoadError("Use either secrets.yaml or secret.properties, not both")
    if yaml_path.exists():
        return _load_secrets_yaml(yaml_path)
    if properties_path.exists():
        return _load_secret_properties(properties_path)
    return ()


def _load_secrets_yaml(path: Path) -> tuple[LocalSecretRecord, ...]:
    payload = _read_yaml_mapping(path)
    return tuple(_secret_record_from_yaml_item(key, value) for key, value in payload.items())


def _load_secret_properties(path: Path) -> tuple[LocalSecretRecord, ...]:
    records: list[LocalSecretRecord] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProjectLoadError(f"{path}:{line_number} must use KEY=VALUE")
        key, raw_value = line.split("=", 1)
        normalized_key = _secret_key(key, path_label=f"{path}:{line_number}")
        value = raw_value.strip()
        records.append(_raw_secret_record(normalized_key, value))
    return tuple(records)


def _secret_record_from_yaml_item(key: object, value: object) -> LocalSecretRecord:
    normalized_key = _secret_key(key, path_label=f"secrets.yaml key {key!r}")
    if isinstance(value, str):
        return _raw_secret_record(normalized_key, value)
    if not isinstance(value, dict):
        raise ProjectLoadError(f"Secret '{normalized_key}' must be a string or object")
    secret_type = str(value.get("type") or "raw").strip().lower()
    description = value.get("description")
    description_value = str(description).strip() if description is not None else None
    if secret_type == "raw":
        raw_value = _secret_payload_string(value, "value", normalized_key)
        secret_payload = {"type": "raw", "value": raw_value}
        return _secret_record(normalized_key, secret_type, raw_value, secret_payload, description_value)
    if secret_type == "api_key":
        api_key = _secret_payload_string(value, "api_key", normalized_key, fallback_key="value")
        secret_payload: dict[str, object] = {"type": "api_key", "api_key": api_key}
        env_name = _env_ref_name(api_key)
        if env_name is not None:
            secret_payload = {"type": "api_key", "api_key_from_env": env_name}
            api_key = os.getenv(env_name, api_key)
        return _secret_record(normalized_key, secret_type, api_key, secret_payload, description_value)
    if secret_type == "bearer":
        token = _secret_payload_string(value, "token", normalized_key, fallback_key="value")
        secret_payload = {"type": "bearer", "token": token}
        return _secret_record(normalized_key, secret_type, token, secret_payload, description_value)
    raise ProjectLoadError(f"Secret '{normalized_key}' has unsupported type '{secret_type}'")


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProjectLoadError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectLoadError(f"{path.name} must contain a mapping")
    return payload


def _secret_key(value: object, *, path_label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectLoadError(f"{path_label} must be a non-empty string")
    normalized = value.strip()
    if normalized.startswith("secret."):
        raise ProjectLoadError(f"{path_label} should be a key, not a secret id")
    return normalized


def _secret_payload_string(payload: dict[object, object], key: str, secret_key: str, *, fallback_key: str | None = None) -> str:
    value = payload.get(key)
    if value is None and fallback_key is not None:
        value = payload.get(fallback_key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectLoadError(f"Secret '{secret_key}' requires non-empty '{key}'")
    return value.strip()


def _env_ref_name(value: str) -> str | None:
    match = _ENV_REF_PATTERN.match(value.strip())
    return match.group(1) if match else None


def _raw_secret_record(key: str, value: str) -> LocalSecretRecord:
    if not value:
        raise ProjectLoadError(f"Secret '{key}' value must be non-empty")
    return _secret_record(key, "raw", value, {"type": "raw", "value": value}, None)


def _secret_record(
    key: str,
    secret_type: str,
    value: str,
    secret_payload: dict[str, object],
    description: str | None,
) -> LocalSecretRecord:
    return LocalSecretRecord(
        id=f"secret.{key}",
        organization_id="local",
        key=key,
        description=description,
        secret_type=secret_type,
        value=value,
        secret=secret_payload,
    )
