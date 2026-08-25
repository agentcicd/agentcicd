from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Mapping

from agentcicd.sql.ir.functions import RegisteredFunctionParameterSpec, RegisteredFunctionSpec


SCHEMA_VERSION = "agentcicd.fixtures.manifest.v1"


class FixtureManifestError(ValueError):
    """Raised when a AgentCICD fixture manifest is invalid for SQL consumption."""


@dataclass(frozen=True)
class ParsedFixtureManifest:
    raw: Mapping[str, Any]

    def registered_function_specs(self) -> list[RegisteredFunctionSpec]:
        validate_fixture_manifest(self.raw)
        specs: list[RegisteredFunctionSpec] = []
        for item in self.raw["functions"]:
            metadata = dict(item["metadata"])
            signature = metadata["signature"]
            specs.append(
                RegisteredFunctionSpec(
                    name=str(item["name"]),
                    kind="remote",
                    call_name=str(item["name"]),
                    runtime_alias=str(item["runtime"]["runtime_alias"]),
                    signature=tuple(
                        RegisteredFunctionParameterSpec(
                            name=str(parameter["name"]),
                            type_sql=str(parameter["type_sql"]),
                            has_default=bool(parameter.get("has_default", False)),
                        )
                        for parameter in signature["parameters"]
                    ),
                    metadata=metadata,
                )
            )
        for item in self.raw["environments"]:
            spec = item["spec"]
            specs.append(
                RegisteredFunctionSpec(
                    name=str(item["spec_function"]),
                    kind="remote",
                    call_name=str(item["spec_function"]),
                    runtime_alias=str(item["spec_function"]).replace(".", "_"),
                    signature=tuple(
                        RegisteredFunctionParameterSpec(
                            name=str(field["name"]),
                            type_sql=_manifest_type_sql(field["type"]),
                            has_default=not bool(field["required"]),
                        )
                        for field in spec["fields"]
                    ),
                    metadata={
                        "execution_runtime": "fixture_env_spec_builder",
                        "entrypoint_name": "spec",
                        "return_type_sql": "VARIANT",
                        "output_schema": {
                            "type": "object",
                            "additionalProperties": True,
                            "x-agentcicd-type": "env_spec",
                            "spec": spec["name"],
                        },
                    },
                )
            )
        return specs


def parse_fixture_manifest(manifest: Mapping[str, Any]) -> ParsedFixtureManifest:
    validate_fixture_manifest(manifest)
    return ParsedFixtureManifest(raw=manifest)


@lru_cache(maxsize=1)
def builtin_fixture_manifest() -> Mapping[str, Any]:
    raw = resources.files("agentcicd.sql").joinpath("builtin_fixtures_manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    validate_fixture_manifest(manifest)
    return manifest


def builtin_registered_function_specs() -> list[RegisteredFunctionSpec]:
    return parse_fixture_manifest(builtin_fixture_manifest()).registered_function_specs()


def validate_fixture_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise FixtureManifestError(f"Unsupported fixture manifest schema_version: {manifest.get('schema_version')!r}")
    package = manifest.get("package")
    if not isinstance(package, Mapping):
        raise FixtureManifestError("Fixture manifest requires package metadata")
    for key in ("name", "version", "namespace"):
        if not _non_empty_string(package.get(key)):
            raise FixtureManifestError(f"Fixture manifest package.{key} must be a non-empty string")
    functions = manifest.get("functions")
    if not isinstance(functions, list):
        raise FixtureManifestError("Fixture manifest functions must be a list")
    for item in functions:
        _validate_function(item)
    environments = manifest.get("environments")
    if not isinstance(environments, list):
        raise FixtureManifestError("Fixture manifest environments must be a list")
    for item in environments:
        _validate_environment(item)


def _validate_function(item: Any) -> None:
    if not isinstance(item, Mapping):
        raise FixtureManifestError("Fixture manifest function entries must be objects")
    for key in ("name", "module", "object", "shape"):
        if not _non_empty_string(item.get(key)):
            raise FixtureManifestError(f"Fixture manifest function.{key} must be a non-empty string")
    if item["shape"] != "1:1":
        raise FixtureManifestError("Only 1:1 fixture functions are supported")
    runtime = item.get("runtime")
    if not isinstance(runtime, Mapping) or not _non_empty_string(runtime.get("runtime_alias")):
        raise FixtureManifestError("Fixture manifest function.runtime.runtime_alias is required")
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("signature"), Mapping):
        raise FixtureManifestError("Fixture manifest function.metadata.signature is required")
    if not isinstance(item.get("parameters"), list):
        raise FixtureManifestError("Fixture manifest function.parameters must be a list")
    for parameter in item["parameters"]:
        if not isinstance(parameter, Mapping) or not _non_empty_string(parameter.get("name")):
            raise FixtureManifestError("Fixture manifest function parameters require names")
        _validate_type(parameter.get("type"))
    _validate_type(item.get("returns"))


def _validate_environment(item: Any) -> None:
    if not isinstance(item, Mapping):
        raise FixtureManifestError("Fixture manifest environment entries must be objects")
    for key in ("name", "spec_function", "module", "class"):
        if not _non_empty_string(item.get(key)):
            raise FixtureManifestError(f"Fixture manifest environment.{key} must be a non-empty string")
    _validate_type(item.get("spec"))


def _validate_type(raw_type: Any) -> None:
    if not isinstance(raw_type, Mapping):
        raise FixtureManifestError("Fixture manifest type entries must be objects")
    type_name = raw_type.get("type")
    if not _non_empty_string(type_name):
        raise FixtureManifestError("Fixture manifest type requires a type name")
    if type_name in {"Str", "Int", "Float", "Bool", "Variant", "DirectoryEntry", "Directory", "EnvSpec"}:
        return
    if type_name == "Array":
        _validate_type(raw_type.get("element"))
        return
    if type_name == "Map":
        _validate_type(raw_type.get("key"))
        _validate_type(raw_type.get("value"))
        return
    if type_name == "NamedStruct":
        fields = raw_type.get("fields")
        if not isinstance(fields, list):
            raise FixtureManifestError("NamedStruct manifest types require fields")
        for field in fields:
            if not isinstance(field, Mapping) or not _non_empty_string(field.get("name")):
                raise FixtureManifestError("NamedStruct fields require names")
            _validate_type(field.get("type"))
        return
    raise FixtureManifestError(f"Unsupported fixture manifest type: {type_name}")


def _manifest_type_sql(raw_type: Mapping[str, Any]) -> str:
    type_name = str(raw_type["type"])
    if type_name == "Str":
        return "STRING"
    if type_name == "Int":
        return "BIGINT"
    if type_name == "Float":
        return "DOUBLE"
    if type_name == "Bool":
        return "BOOLEAN"
    if type_name == "Variant":
        return "VARIANT"
    if type_name == "DirectoryEntry":
        return "STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>"
    if type_name == "Directory":
        return "ARRAY<STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>>"
    if type_name == "Array":
        return f"ARRAY<{_manifest_type_sql(raw_type['element'])}>"
    if type_name == "Map":
        return f"MAP<{_manifest_type_sql(raw_type['key'])}, {_manifest_type_sql(raw_type['value'])}>"
    if type_name == "EnvSpec":
        return "VARIANT"
    if type_name == "NamedStruct":
        return "STRUCT<" + ", ".join(
            f"{field['name']}: {_manifest_type_sql(field['type'])}" for field in raw_type["fields"]
        ) + ">"
    raise FixtureManifestError(f"Unsupported fixture manifest type: {type_name}")


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
