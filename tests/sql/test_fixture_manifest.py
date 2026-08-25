from __future__ import annotations

import json
import builtins

import pytest

from agentcicd.sql.fixture_manifest import (
    FixtureManifestError,
    builtin_fixture_manifest,
    parse_fixture_manifest,
    validate_fixture_manifest,
)
from agentcicd.sql.parsing.runtime_signature_registry import (
    clear_registered_runtime_signatures,
    get_runtime_signature,
    register_runtime_signature_specs,
)
from agentcicd.sql.semantics.registry import build_function_registry


@pytest.fixture(autouse=True)
def _clear_runtime_signatures() -> None:
    clear_registered_runtime_signatures()
    yield
    clear_registered_runtime_signatures()


def _manifest() -> dict[str, object]:
    directory_entry_type = {
        "type": "NamedStruct",
        "fields": [
            {"name": "path", "type": {"type": "Str"}},
            {"name": "name", "type": {"type": "Str"}},
            {"name": "parent_path", "type": {"type": "Str"}},
            {"name": "entry_type", "type": {"type": "Str"}},
            {"name": "size_bytes", "type": {"type": "Int"}},
            {"name": "content_type", "type": {"type": "Str"}},
            {"name": "sha256", "type": {"type": "Str"}},
            {"name": "object_uri", "type": {"type": "Str"}},
            {"name": "is_empty_dir", "type": {"type": "Bool"}},
        ],
    }
    return {
        "schema_version": "agentcicd.fixtures.manifest.v1",
        "package": {"name": "sample_acme_fixtures", "version": "0.1.0", "namespace": "acme"},
        "functions": [
            {
                "name": "acme.support.policy_score",
                "module": "sample_acme_fixtures.support",
                "object": "policy_score",
                "shape": "1:1",
                "runtime": {"runtime_alias": "acme_support_policy_score"},
                "parameters": [
                    {"name": "answer", "type": {"type": "Str"}, "required": True},
                    {"name": "policy", "type": {"type": "Str"}, "required": True},
                    {"name": "judge_secret_id", "type": {"type": "Str"}, "required": False},
                ],
                "returns": {
                    "type": "NamedStruct",
                    "fields": [
                        {"name": "value", "type": {"type": "Float"}},
                        {"name": "rationale", "type": {"type": "Str"}},
                    ],
                },
                "metadata": {
                    "signature": {
                        "parameters": [
                            {"name": "answer", "type_sql": "STRING", "has_default": False},
                            {"name": "policy", "type_sql": "STRING", "has_default": False},
                            {"name": "judge_secret_id", "type_sql": "STRING", "has_default": True},
                        ]
                    },
                    "return_type_sql": "STRUCT<value: DOUBLE, rationale: STRING>",
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "number"},
                            "rationale": {"type": "string"},
                        },
                    },
                    "execution_runtime": "function_runner",
                },
            },
            {
                "name": "acme.support.artifact_echo",
                "module": "sample_acme_fixtures.support",
                "object": "artifact_echo",
                "shape": "1:1",
                "runtime": {"runtime_alias": "acme_support_artifact_echo"},
                "parameters": [{"name": "payload", "type": {"type": "Str"}, "required": True}],
                "returns": {"type": "Directory"},
                "metadata": {
                    "signature": {
                        "parameters": [{"name": "payload", "type_sql": "STRING", "has_default": False}]
                    },
                    "return_type_sql": (
                        "ARRAY<STRUCT<path: STRING, name: STRING, parent_path: STRING, "
                        "entry_type: STRING, size_bytes: BIGINT, content_type: STRING, "
                        "sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>>"
                    ),
                    "output_schema": {
                        "type": "array",
                        "items": {"type": "object", "x-agentcicd-type": "directory"},
                    },
                    "execution_runtime": "function_runner",
                },
            },
        ],
        "environments": [
            {
                "name": "acme.warehouse",
                "spec_function": "envs.acme.warehouse.spec",
                "module": "sample_acme_fixtures.envs",
                "class": "WarehouseEnv",
                "spec": {
                    "name": "WarehouseSpec",
                    "type": "EnvSpec",
                    "fields": [
                        {"name": "database", "type": {"type": "Str"}, "required": True},
                        {"name": "schema", "type": {"type": "Str"}, "required": True},
                    ],
                },
            }
        ],
        "types": {"DirectoryEntry": directory_entry_type},
    }


def test_sql_side_validates_fixture_manifest() -> None:
    manifest = _manifest()

    validate_fixture_manifest(manifest)
    parsed = parse_fixture_manifest(manifest)
    specs = parsed.registered_function_specs()

    names = {item.name for item in specs}
    assert "acme.support.policy_score" in names
    assert "envs.acme.warehouse.spec" in names


def test_sql_side_rejects_invalid_manifest_shape() -> None:
    manifest = _manifest()
    manifest["functions"][0]["shape"] = "aggregate"

    with pytest.raises(FixtureManifestError, match="Only 1:1"):
        validate_fixture_manifest(manifest)


def test_manifest_registered_functions_feed_semantic_registry_and_runtime_signatures() -> None:
    specs = parse_fixture_manifest(_manifest()).registered_function_specs()

    registry = build_function_registry([], specs)
    definition = registry.resolve("acme.support.policy_score")
    env_builder = registry.resolve("envs.acme.warehouse.spec")
    register_runtime_signature_specs(specs)
    signature = get_runtime_signature("acme.support.policy_score")

    assert definition is not None
    assert [parameter.name for parameter in definition.parameters] == ["answer", "policy", "judge_secret_id"]
    assert definition.return_type_sql == "STRUCT<value: DOUBLE, rationale: STRING>"
    assert env_builder is not None
    assert [parameter.name for parameter in env_builder.parameters] == ["database", "schema"]
    assert signature is not None
    assert signature.type_sql_by_name["answer"] == "STRING"


def test_manifest_contains_directory_return_type_for_sql_lowering() -> None:
    specs = parse_fixture_manifest(_manifest()).registered_function_specs()
    artifact_spec = next(item for item in specs if item.name == "acme.support.artifact_echo")

    assert artifact_spec.metadata["return_type_sql"].startswith("ARRAY<STRUCT<")
    assert json.dumps(artifact_spec.metadata["output_schema"]).find('"x-agentcicd-type": "directory"') > 0


def test_builtin_manifest_artifact_is_valid_for_sql_registration() -> None:
    manifest = builtin_fixture_manifest()

    validate_fixture_manifest(manifest)
    specs = parse_fixture_manifest(manifest).registered_function_specs()

    names = {item.name for item in specs}
    assert "agent.ragas.faithfulness" in names
    assert "envs.browser.spec" in names


def test_builtin_manifest_marks_python_only_objectstore_functions() -> None:
    specs = {item.name: item for item in parse_fixture_manifest(builtin_fixture_manifest()).registered_function_specs()}

    assert specs["objectstore.download"].metadata["sql_enabled"] is False
    assert specs["objectstore.download_all"].metadata["sql_enabled"] is False
    assert specs["objectstore.upload"].metadata["sql_enabled"] is False
    assert specs["objectstore.upload_all"].metadata["sql_enabled"] is False
    assert specs["objectstore.read_text"].metadata["sql_enabled"] is True
    assert specs["objectstore.read_json"].metadata["sql_enabled"] is True
    assert specs["objectstore.download"].metadata["placements"] == ["local_python"]
    assert "spark_executor" in specs["objectstore.read_text"].metadata["placements"]


def test_builtin_sql_registration_uses_manifest_without_importing_agentcicd_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "agentcicd_fixtures" or name.startswith("agentcicd_fixtures."):
            raise AssertionError(f"SQL registry should not import {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    registry = build_function_registry([], [])
    definition = registry.resolve("agent.ragas.faithfulness")
    env_builder = registry.resolve("envs.browser.spec")

    assert definition is not None
    assert definition.parameters
    assert definition.metadata["authoring_model"] == "function"
    assert env_builder is not None
    assert env_builder.metadata["authoring_model"] == "function"
    assert env_builder.metadata["execution_runtime"] == "function_runner"
