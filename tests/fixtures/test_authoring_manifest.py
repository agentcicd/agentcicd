from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentcicd.fixtures import (
    AgentHarnessSpec,
    Directory,
    Environment,
    EnvSpec,
    Int,
    SecretId,
    Str,
    bind_runtime_globals,
    env_specs,
    envs,
    environment,
    generate_manifest_for_package,
    generate_builtin_manifest,
    reset_runtime_globals,
    secret_parameter_names,
    ShellSpec,
    type_contract,
    validate_manifest,
    function,
)
from agentcicd.fixtures.registry import FixtureRegistry


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))


def test_function_and_environment_manifest_generation_omits_secret_metadata() -> None:
    manifest = generate_manifest_for_package("sample_acme_fixtures", namespace="acme")

    validate_manifest(manifest)
    function_names = {item["name"] for item in manifest["functions"]}
    assert "acme.support.policy_score" in function_names
    assert "acme.support.artifact_echo" in function_names
    assert "acme.warehouse.count_rows" not in function_names
    environment = manifest["environments"][0]
    assert environment["spec_function"] == "envs.acme.warehouse.spec"
    assert environment["spec"]["name"] == "WarehouseSpec"

    serialized = json.dumps(manifest)
    assert "SecretId" not in serialized
    assert "secret.default" not in serialized
    assert "judge_secret_id" in serialized


def test_secret_id_extraction_is_runtime_local() -> None:
    annotations = {"answer": Str, "secret": SecretId, "return": Str}

    assert secret_parameter_names(annotations) == ("secret",)


def test_directory_and_envspec_type_contracts() -> None:
    directory_contract = type_contract(Directory)
    env_contract = type_contract(EnvSpec[ShellSpec])

    assert directory_contract.json_schema["x-agentcicd-type"] == "directory"
    assert "path: STRING" in directory_contract.type_sql
    assert env_contract.type_sql == "VARIANT"
    assert env_contract.manifest_type == {"type": "EnvSpec", "spec": "ShellSpec"}


def test_runtime_globals_can_bind_fake_env_resolver() -> None:
    class Resolver:
        def resolve(self, spec: object) -> dict[str, object]:
            return {"resolved": spec}

    bind_runtime_globals(envs_impl=Resolver())
    try:
        assert envs.resolve("fs") == {"resolved": "fs"}
    finally:
        reset_runtime_globals()


def test_typed_agent_harness_spec_config_adds_mcp() -> None:
    agent = env_specs.agent_harness.spec(session_id="agent")
    mcp = env_specs.mcp.stdio.spec(name="ignored", command="playwright-mcp")

    returned = agent.config.add_mcp("playwright", mcp)

    assert returned is agent
    expected = mcp.to_dict()
    expected["config"]["name"] = "playwright"
    assert agent.to_dict()["config"]["mcps"] == {"playwright": expected}


def test_typed_agent_harness_spec_accepts_mcp_map() -> None:
    mcp = env_specs.mcp.stdio.spec(name="ignored", command="playwright-mcp")

    agent = env_specs.agent_harness.spec(session_id="agent", mcps={"playwright": mcp})

    expected = mcp.to_dict()
    expected["config"]["name"] = "playwright"
    assert agent.to_dict()["config"]["mcps"] == {"playwright": expected}


def test_cli_generates_manifest(tmp_path: Path) -> None:
    output_path = tmp_path / "fixtures.manifest.json"
    command = [
        sys.executable,
        "-m",
        "agentcicd_fixtures.cli",
        "manifest",
        "--package",
        "sample_acme_fixtures",
        "--namespace",
        "acme",
        "--output",
        str(output_path),
    ]
    repo_root = Path(__file__).resolve().parents[3]
    fixture_compat_src = repo_root / "agentcicd_fixtures" / "src"
    agentcicd_src = repo_root / "agentcicd" / "src"
    subprocess.check_call(
        command,
        env={"PYTHONPATH": os.pathsep.join([str(FIXTURES_DIR), str(agentcicd_src), str(fixture_compat_src)])},
    )

    manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "agentcicd.fixtures.manifest.v1"
    assert manifest["package"]["namespace"] == "acme"


def test_builtin_manifest_uses_public_authoring_model() -> None:
    manifest = generate_builtin_manifest()

    validate_manifest(manifest)
    assert manifest["functions"]
    assert manifest["environments"]
    assert {item["metadata"]["authoring_model"] for item in manifest["functions"]} == {"function"}
    assert {item["metadata"]["authoring_model"] for item in manifest["environments"]} == {"environment"}


def test_environment_rejects_public_methods_without_function_annotation() -> None:
    class LocalSpec:
        pass

    with pytest.raises(TypeError, match="must be decorated with @function"):
        @environment(registry=FixtureRegistry())
        class BadEnvironment(Environment[LocalSpec]):
            def call(self, value: Str) -> Int:
                return 1


def test_environment_allows_function_annotated_public_methods_without_registering_sql_function() -> None:
    registry = FixtureRegistry()

    class LocalSpec:
        pass

    @environment(registry=registry)
    class GoodEnvironment(Environment[LocalSpec]):
        @function
        def call(self, value: Str) -> Int:
            return 1

    assert len(registry.environments) == 1
    assert registry.environments[0].class_object is GoodEnvironment
    assert registry.functions == []
