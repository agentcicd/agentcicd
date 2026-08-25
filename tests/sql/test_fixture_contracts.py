from __future__ import annotations

import pytest

from agentcicd.sql.fixture_contracts import FixtureContractError, extract_fixture_contract


def test_extract_fixture_contract_supports_nested_named_structs() -> None:
    source = """
class Details(NamedStruct):
    reason: Str
    confidence: Required[Float]


class Result(NamedStruct):
    score: Required[Float]
    label: Str
    details: Details
    history: Array[Variant]
    tags: Map[Str, Str]


@function
async def judge(text: Str, max_turns: Int = 3) -> Result:
    return {}
"""

    contract = extract_fixture_contract(source)

    assert contract.entrypoint == "judge"
    assert contract.metadata["async"] is True
    assert contract.metadata["signature"]["parameters"] == [
        {"name": "text", "type_sql": "STRING", "nullable": True, "has_default": False},
        {"name": "max_turns", "type_sql": "BIGINT", "nullable": True, "has_default": True},
    ]
    assert contract.metadata["signature"]["return"]["type_sql"] == (
        "STRUCT<score: DOUBLE, label: STRING, details: STRUCT<reason: STRING, confidence: DOUBLE>, "
        "history: ARRAY<VARIANT>, tags: MAP<STRING, STRING>>"
    )
    assert contract.output_schema["required"] == ["score"]
    assert contract.output_schema["properties"]["details"]["required"] == ["confidence"]
    assert contract.output_schema["properties"]["history"]["items"]["type"] == "variant"


def test_extract_fixture_contract_supports_directory_inside_named_struct() -> None:
    source = """
class Result(NamedStruct):
    artifacts: Directory
    passed: Bool


@function
async def run_task(tree: Directory) -> Result:
    return {}
"""

    contract = extract_fixture_contract(source)

    return_type = contract.metadata["signature"]["return"]["type_sql"]
    assert return_type.startswith("STRUCT<artifacts: ARRAY<STRUCT<")
    assert "path: STRING" in return_type
    assert "passed: BOOLEAN" in return_type
    assert contract.output_schema["properties"]["artifacts"]["x-agentcicd-type"] == "directory"


@pytest.mark.parametrize("annotation", ["str", "int", "Any", "dict[str, Any]", "list[Any]", "Optional[Str]"])
def test_extract_fixture_contract_rejects_python_annotations(annotation: str) -> None:
    source = f"""
from typing import Any, Optional

@function
def transform(value: Str) -> {annotation}:
    return None
"""

    with pytest.raises(FixtureContractError):
        extract_fixture_contract(source)


def test_extract_fixture_contract_rejects_typed_dict() -> None:
    source = """
from typing import TypedDict


class Result(TypedDict):
    value: str


@function
def transform(value: Str) -> Result:
    return {"value": value}
"""

    with pytest.raises(FixtureContractError, match="TypedDict fixture contracts are no longer supported"):
        extract_fixture_contract(source)


def test_extract_fixture_contract_supports_typed_fixture_environment_and_directory_symbols() -> None:
    source = """
from agentcicd import AgentHarnessEnv, Directory, EnvSpec, McpSpec, Session, ShellEnv, function


@function
async def browser_task(
    session: Session,
    sh: ShellEnv,
    agent: AgentHarnessEnv,
    mcp: McpSpec,
    explicit: EnvSpec["shell"],
    tree: Directory,
) -> Directory:
    return []
"""

    contract = extract_fixture_contract(source)
    parameters = contract.metadata["signature"]["parameters"]

    assert parameters[0]["type_sql"] == "VARIANT"
    assert parameters[0]["nullable"] is True
    assert "path: STRING" in contract.metadata["signature"]["return"]["type_sql"]
    assert contract.output_schema["x-agentcicd-type"] == "directory"
    assert contract.input_schema["properties"]["sh"]["environment_kind"] == "shell"
    assert contract.input_schema["properties"]["agent"]["environment_kind"] == "agent_harness"
    assert "session" not in contract.input_schema["properties"]
    assert contract.metadata["injected_parameters"] == [{"name": "session", "kind": "session"}]
