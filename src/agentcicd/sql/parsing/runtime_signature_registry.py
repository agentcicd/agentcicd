from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.fixture_manifest import builtin_registered_function_specs
from agentcicd.sql.ir.functions import (
    RegisteredFunctionSpec,
    coerce_registered_function_specs,
    registered_function_sql_enabled,
)


@dataclass(frozen=True)
class RuntimeFunctionSignature:
    runtime_alias: str
    input_args: tuple[str, ...]
    has_default_by_name: dict[str, bool]
    type_sql_by_name: dict[str, str]
    returns_json: bool = False


_REGISTERED_SIGNATURES: dict[str, RuntimeFunctionSignature] = {}
_BUILTIN_SIGNATURES: dict[str, RuntimeFunctionSignature] | None = None


def clear_registered_runtime_signatures() -> None:
    global _BUILTIN_SIGNATURES
    _REGISTERED_SIGNATURES.clear()
    _BUILTIN_SIGNATURES = None


def register_runtime_signature_specs(
    items: Iterable[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]],
) -> None:
    for spec in coerce_registered_function_specs(items):
        if not registered_function_sql_enabled(spec):
            continue
        runtime_alias = str(spec.runtime_alias or spec.name.replace(".", "_")).strip()
        signature = RuntimeFunctionSignature(
            runtime_alias=runtime_alias,
            input_args=tuple(parameter.name for parameter in spec.signature),
            has_default_by_name={
                parameter.name.lower(): parameter.has_default
                for parameter in spec.signature
            },
            type_sql_by_name={
                parameter.name.lower(): parameter.type_sql
                for parameter in spec.signature
            },
            returns_json=_metadata_returns_json(spec),
        )
        _store_signature(spec.name, signature)
        if spec.call_name:
            _store_signature(spec.call_name, signature)


def get_runtime_signature(name: str) -> RuntimeFunctionSignature | None:
    direct = _REGISTERED_SIGNATURES.get(name.lower())
    if direct is not None:
        return direct
    signatures = _load_builtin_runtime_signatures()
    return signatures.get(name.lower())


def _store_signature(name: str, signature: RuntimeFunctionSignature) -> None:
    normalized = name.strip().lower()
    if not normalized:
        return
    _REGISTERED_SIGNATURES[normalized] = signature
    _REGISTERED_SIGNATURES.setdefault(normalized.split(".")[-1], signature)
    _REGISTERED_SIGNATURES[signature.runtime_alias.lower()] = signature


def _load_builtin_runtime_signatures() -> dict[str, RuntimeFunctionSignature]:
    global _BUILTIN_SIGNATURES
    if _BUILTIN_SIGNATURES is not None:
        return _BUILTIN_SIGNATURES
    signatures: dict[str, RuntimeFunctionSignature] = {}
    for spec in builtin_registered_function_specs():
        if not registered_function_sql_enabled(spec):
            continue
        runtime_alias = str(spec.runtime_alias or spec.name.replace(".", "_")).strip()
        signature = RuntimeFunctionSignature(
            runtime_alias=runtime_alias,
            input_args=tuple(parameter.name for parameter in spec.signature),
            has_default_by_name={
                parameter.name.lower(): parameter.has_default
                for parameter in spec.signature
            },
            type_sql_by_name={
                parameter.name.lower(): parameter.type_sql
                for parameter in spec.signature
            },
            returns_json=_metadata_returns_json(spec),
        )
        signatures[spec.name.lower()] = signature
        if spec.call_name:
            signatures[str(spec.call_name).lower()] = signature
        signatures[runtime_alias.lower()] = signature
        signatures.setdefault(spec.name.split(".")[-1].lower(), signature)
    _BUILTIN_SIGNATURES = signatures
    return signatures


def _metadata_returns_json(spec: RegisteredFunctionSpec) -> bool:
    raw_value = spec.metadata.get("returns_json")
    if isinstance(raw_value, bool):
        return raw_value
    output_type = str(spec.metadata.get("output_type") or "").strip().lower()
    if output_type in {"json", "variant"}:
        return True
    return_type_sql = str(spec.metadata.get("return_type_sql") or "").strip().upper()
    if return_type_sql == "VARIANT":
        return True
    output_schema = spec.metadata.get("output_schema")
    if isinstance(output_schema, Mapping):
        schema_type = str(output_schema.get("type") or "").strip().lower()
        if schema_type in {"json", "variant"}:
            return True
    return False
