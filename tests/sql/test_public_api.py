from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcicd.sql import api
from agentcicd.sql.engine.runner import EngineRunConfig


def _manifest(name: str = "acme.support.policy_score") -> dict[str, object]:
    return {
        "schema_version": "agentcicd.fixtures.manifest.v1",
        "package": {"name": "acme-support", "version": "1.0.0", "namespace": "acme.support"},
        "types": [],
        "functions": [
            {
                "name": name,
                "module": "acme.support",
                "object": "policy_score",
                "shape": "1:1",
                "runtime": {"runtime_alias": name.replace(".", "_")},
                "parameters": [
                    {"name": "answer", "type": {"type": "Str"}, "required": True},
                    {"name": "policy", "type": {"type": "Str"}, "required": True},
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
                        ],
                        "return": {"type_sql": "STRUCT<value: DOUBLE, rationale: STRING>"},
                    },
                    "return_type_sql": "STRUCT<value: DOUBLE, rationale: STRING>",
                },
            }
        ],
        "environments": [],
    }


def test_validate_manifests_returns_registered_function_specs() -> None:
    result = api.validate_manifests([_manifest()])

    assert len(result.manifests) == 1
    assert [spec.name for spec in result.registered_functions] == ["acme.support.policy_score"]


def test_validate_recipe_accepts_manifest_function() -> None:
    result = api.validate_recipe(
        """
        CREATE BATCH TABLE prepared
        SELECT 'answer' AS answer, 'policy' AS policy;

        CREATE BATCH TABLE evaluated
        SELECT acme.support.policy_score(answer = answer, policy = policy) AS score
        FROM prepared;
        """,
        manifests=[_manifest()],
    )

    assert result.manifest_count == 1
    assert result.registered_function_count == 1
    assert result.validation_mode == "static"


def test_public_api_import_does_not_import_execution_runner() -> None:
    script = """
import sys
sys.path.insert(0, "agentcicd/src")
import agentcicd.sql.api
print("agentcicd_eval_sql.engine.runner" in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
    )

    assert completed.stdout.strip() == "False"


def test_validate_recipe_execution_uses_supplied_spark_session() -> None:
    parsed_sql: list[str] = []

    class Parser:
        def parsePlan(self, sql: str) -> object:
            parsed_sql.append(sql)
            return object()

    class SessionState:
        def sqlParser(self) -> Parser:
            return Parser()

    class JSparkSession:
        def sessionState(self) -> SessionState:
            return SessionState()

    class SparkSession:
        _jsparkSession = JSparkSession()

    result = api.validate_recipe_execution(
        "CREATE BATCH TABLE evaluated SELECT 1 AS id;",
        spark_session=SparkSession(),
    )

    assert result.static_validation.validation_mode == "execution"
    assert result.registered_runtime_function_count == 0
    assert result.lowered_sql_count == 1
    assert result.spark_validations[0].engine == "spark_parser"
    assert parsed_sql


def test_validate_recipe_execution_requires_spark_session() -> None:
    with pytest.raises(api.AgentCICDEvalSqlApiError, match="spark_session is required"):
        api.validate_recipe_execution(
            "CREATE BATCH TABLE evaluated SELECT 1 AS id;",
            spark_session=None,
        )


def test_validate_manifests_accepts_manifest_paths(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    result = api.validate_manifests([manifest_path])

    assert result.registered_functions[0].name == "acme.support.policy_score"


def test_validate_manifests_rejects_duplicate_function_names() -> None:
    with pytest.raises(api.AgentCICDEvalSqlApiError, match="Duplicate registered function"):
        api.validate_manifests([_manifest(), _manifest()])


def test_validate_recipe_rejects_builtin_manifest_conflict() -> None:
    with pytest.raises(api.AgentCICDEvalSqlApiError, match="conflicts with built-in fixture"):
        api.validate_recipe(
            """
            CREATE BATCH TABLE evaluated
            SELECT data.parse_json(text = payload) AS parsed
            FROM prepared;
            """,
            manifests=[_manifest("data.parse_json")],
        )


def test_validate_recipe_accepts_generated_builtin_runtime_overlay() -> None:
    api.validate_recipe(
        """
        CREATE BATCH TABLE evaluated
        SELECT aisystems.llm.chat(
          aisystem_id = 'openai/gpt-4.1-mini',
          messages = parse_json('[]')
        ) AS response_raw;
        """,
        registered_functions=[
            {
                "id": "builtin.aisystems.llm.chat",
                "name": "aisystems.llm.chat",
                "type": "remote",
                "call_name": "aisystems.llm.chat",
                "runtime_alias": "aisystems_llm_chat",
                "base_url": "http://127.0.0.1:10000",
                "invoke_path": "/invoke/chat",
                "entrypoint_name": "chat",
                "return_type_sql": "VARIANT",
                "pool_kind": "service",
                "signature": {
                    "parameters": [
                        {"name": "aisystem_id", "type_sql": "STRING"},
                        {"name": "messages", "type_sql": "VARIANT"},
                    ]
                },
            }
        ],
    )


def test_run_recipe_plumbs_manifest_functions_into_engine_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    expected_report = SimpleNamespace(events=[])

    def fake_run_script_with_new_engine(recipe_sql: str, config: EngineRunConfig):
        captured["recipe_sql"] = recipe_sql
        captured["config"] = config
        return expected_report

    monkeypatch.setattr(api, "_run_script_with_new_engine", fake_run_script_with_new_engine)

    report = api.run_recipe(
        "CREATE BATCH TABLE evaluated SELECT 1 AS id;",
        EngineRunConfig(working_dir=str(tmp_path)),
        manifests=[_manifest()],
    )

    assert report is expected_report
    config = captured["config"]
    assert isinstance(config, EngineRunConfig)
    assert config.registered_functions is not None
    assert [spec.name for spec in config.registered_functions] == ["acme.support.policy_score"]
