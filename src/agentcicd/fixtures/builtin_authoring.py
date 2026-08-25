from __future__ import annotations

from typing import Any, Callable, Mapping

from agentcicd.fixtures.core.types import (
    ArrayType,
    BooleanType,
    FloatType,
    FunctionType,
    IntType,
    JsonType,
    MapType,
    StringType,
)
from agentcicd.fixtures.core.udf import Udf
from agentcicd.fixtures.functions import load_builtin_udfs, udf
from agentcicd.fixtures.registry import EnvironmentRegistration, FixtureRegistry, FunctionRegistration, environment, function
from agentcicd.fixtures.types import (
    AgentHarnessSpec,
    BrowserSpec,
    Environment,
    McpHttpSpec,
    McpPlaywrightSpec,
    McpStdioSpec,
    ShellSpec,
)


BUILTIN_REGISTRY = FixtureRegistry()


def builtin_function_registrations() -> list[FunctionRegistration]:
    return list(BUILTIN_REGISTRY.functions)


def builtin_environment_registrations() -> list[EnvironmentRegistration]:
    return list(BUILTIN_REGISTRY.environments)


def _builtin_callable(call_name: str) -> Callable[..., Any]:
    def invoke(**kwargs: Any) -> Any:
        return udf(call_name)(**kwargs)

    invoke.__name__ = call_name.replace(".", "_")
    invoke.__qualname__ = invoke.__name__
    return invoke


def _manifest_for(call_name: str) -> dict[str, Any]:
    udf_cls = load_builtin_udfs()[call_name]
    return _builtin_function_manifest(call_name, udf_cls)


def _invoke_builtin(call_name: str, kwargs: dict[str, Any]) -> Any:
    return udf(call_name)(**kwargs)


def _builtin_function_manifest(name: str, udf_cls: type[Udf]) -> dict[str, Any]:
    instance = udf_cls()
    parameters = _builtin_parameters(instance)
    return_type = _builtin_manifest_type(instance.output_schema())
    return_type_sql = _manifest_type_sql(return_type)
    metadata = {
        "authoring_model": "function",
        "execution_runtime": "function_runner",
        "entrypoint_name": name.rsplit(".", 1)[-1],
        "module": udf_cls.__module__,
        "object": udf_cls.__name__,
        "shape": "1:1",
        "return_type_sql": return_type_sql,
        "output_schema": _builtin_json_schema(return_type),
        "signature": {
            "parameters": [
                {
                    "name": parameter["name"],
                    "type_sql": parameter["type_sql"],
                    "has_default": parameter["has_default"],
                    "nullable": parameter["nullable"],
                }
                for parameter in parameters
            ],
            "return": {
                "type_sql": return_type_sql,
                "nullable": True,
                "schema": return_type,
            },
        },
    }
    metadata.update(instance.metadata())
    return {
        "name": name,
        "module": udf_cls.__module__,
        "object": udf_cls.__name__,
        "shape": "1:1",
        "async": False,
        "parameters": [
            {
                "name": parameter["name"],
                "type": parameter["type"],
                "required": parameter["required"],
                "nullable": parameter["nullable"],
                "has_default": parameter["has_default"],
                "type_sql": parameter["type_sql"],
            }
            for parameter in parameters
        ],
        "returns": return_type,
        "runtime": {
            "kind": "python",
            "runtime_alias": name.replace(".", "_"),
            "entrypoint": f"{udf_cls.__module__}:{udf_cls.__name__}",
        },
        "metadata": metadata,
    }


def _builtin_parameters(instance: Udf) -> list[dict[str, Any]]:
    params = tuple(instance.signature())
    input_types = tuple(instance.input_schema())
    output: list[dict[str, Any]] = []
    for index, parameter in enumerate(params):
        raw_type = input_types[index] if index < len(input_types) else None
        manifest_type = _builtin_manifest_type(raw_type)
        type_sql = str(parameter.type_sql or "")
        if not type_sql or type_sql == "ANY":
            type_sql = _manifest_type_sql(manifest_type)
        required = bool(parameter.required)
        output.append(
            {
                "name": str(parameter.name),
                "type": manifest_type,
                "required": required,
                "nullable": not required,
                "has_default": not required,
                "type_sql": type_sql,
            }
        )
    return output


def _builtin_manifest_type(dtype: Any) -> dict[str, Any]:
    if isinstance(dtype, StringType):
        return {"type": "Str"}
    if isinstance(dtype, IntType):
        return {"type": "Int"}
    if isinstance(dtype, FloatType):
        return {"type": "Float"}
    if isinstance(dtype, BooleanType):
        return {"type": "Bool"}
    if isinstance(dtype, ArrayType):
        return {"type": "Array", "element": {"type": "Variant"}}
    if isinstance(dtype, MapType):
        return {"type": "Map", "key": {"type": "Str"}, "value": {"type": "Variant"}}
    if isinstance(dtype, FunctionType):
        return {"type": "Str"}
    if isinstance(dtype, JsonType):
        return {"type": "Variant"}
    return {"type": "Variant"}


def _builtin_json_schema(manifest_type: Mapping[str, Any]) -> dict[str, Any]:
    type_name = str(manifest_type["type"])
    if type_name == "Str":
        return {"type": "string"}
    if type_name == "Int":
        return {"type": "integer"}
    if type_name == "Float":
        return {"type": "number"}
    if type_name == "Bool":
        return {"type": "boolean"}
    if type_name == "Array":
        return {"type": "array", "items": _builtin_json_schema(manifest_type["element"])}
    if type_name == "Map":
        return {"type": "object", "additionalProperties": _builtin_json_schema(manifest_type["value"])}
    return {}


def _environment_manifest(kind: str, spec_name: str) -> dict[str, Any]:
    return {
        "name": f"agentcicd.{kind}",
        "spec_function": f"envs.{kind}.spec",
        "module": "agentcicd.fixtures.builtin_authoring",
        "class": spec_name.removesuffix("Spec"),
        "spec": {"type": "NamedStruct", "name": spec_name, "fields": []},
        "runtime": {
            "kind": "environment",
            "entrypoint": f"agentcicd.fixtures.builtin_authoring:{spec_name}",
        },
        "metadata": {
            "authoring_model": "environment",
        },
    }


def _manifest_type_sql(raw_type: Mapping[str, Any]) -> str:
    type_name = str(raw_type["type"])
    if type_name in {"Str", "SecretId"}:
        return "STRING"
    if type_name == "Int":
        return "BIGINT"
    if type_name == "Float":
        return "DOUBLE"
    if type_name == "Bool":
        return "BOOLEAN"
    if type_name == "Variant":
        return "VARIANT"
    if type_name == "Directory":
        return "ARRAY<STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>>"
    if type_name == "DirectoryEntry":
        return "STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>"
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
    raise ValueError(f"Unsupported manifest type: {type_name}")


@function(name="agent.ragas.agent_goal_accuracy", namespace="", manifest_entry=_manifest_for("agent.ragas.agent_goal_accuracy"), registry=BUILTIN_REGISTRY)
def agent_ragas_agent_goal_accuracy(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.agent_goal_accuracy", kwargs)


@function(name="agent.ragas.aspect_critic", namespace="", manifest_entry=_manifest_for("agent.ragas.aspect_critic"), registry=BUILTIN_REGISTRY)
def agent_ragas_aspect_critic(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.aspect_critic", kwargs)


@function(name="agent.ragas.context_entities_recall", namespace="", manifest_entry=_manifest_for("agent.ragas.context_entities_recall"), registry=BUILTIN_REGISTRY)
def agent_ragas_context_entities_recall(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.context_entities_recall", kwargs)


@function(name="agent.ragas.context_precision", namespace="", manifest_entry=_manifest_for("agent.ragas.context_precision"), registry=BUILTIN_REGISTRY)
def agent_ragas_context_precision(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.context_precision", kwargs)


@function(name="agent.ragas.context_recall", namespace="", manifest_entry=_manifest_for("agent.ragas.context_recall"), registry=BUILTIN_REGISTRY)
def agent_ragas_context_recall(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.context_recall", kwargs)


@function(name="agent.ragas.execution_based_datacompy_score", namespace="", manifest_entry=_manifest_for("agent.ragas.execution_based_datacompy_score"), registry=BUILTIN_REGISTRY)
def agent_ragas_execution_based_datacompy_score(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.execution_based_datacompy_score", kwargs)


@function(name="agent.ragas.faithfulness", namespace="", manifest_entry=_manifest_for("agent.ragas.faithfulness"), registry=BUILTIN_REGISTRY)
def agent_ragas_faithfulness(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.faithfulness", kwargs)


@function(name="agent.ragas.instance_specific_rubrics_scoring", namespace="", manifest_entry=_manifest_for("agent.ragas.instance_specific_rubrics_scoring"), registry=BUILTIN_REGISTRY)
def agent_ragas_instance_specific_rubrics_scoring(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.instance_specific_rubrics_scoring", kwargs)


@function(name="agent.ragas.multimodal_faithfulness", namespace="", manifest_entry=_manifest_for("agent.ragas.multimodal_faithfulness"), registry=BUILTIN_REGISTRY)
def agent_ragas_multimodal_faithfulness(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.multimodal_faithfulness", kwargs)


@function(name="agent.ragas.multimodal_relevance", namespace="", manifest_entry=_manifest_for("agent.ragas.multimodal_relevance"), registry=BUILTIN_REGISTRY)
def agent_ragas_multimodal_relevance(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.multimodal_relevance", kwargs)


@function(name="agent.ragas.noise_sensitivity", namespace="", manifest_entry=_manifest_for("agent.ragas.noise_sensitivity"), registry=BUILTIN_REGISTRY)
def agent_ragas_noise_sensitivity(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.noise_sensitivity", kwargs)


@function(name="agent.ragas.response_relevancy", namespace="", manifest_entry=_manifest_for("agent.ragas.response_relevancy"), registry=BUILTIN_REGISTRY)
def agent_ragas_response_relevancy(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.response_relevancy", kwargs)


@function(name="agent.ragas.rubrics_based_scoring", namespace="", manifest_entry=_manifest_for("agent.ragas.rubrics_based_scoring"), registry=BUILTIN_REGISTRY)
def agent_ragas_rubrics_based_scoring(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.rubrics_based_scoring", kwargs)


@function(name="agent.ragas.simple_criteria_scoring", namespace="", manifest_entry=_manifest_for("agent.ragas.simple_criteria_scoring"), registry=BUILTIN_REGISTRY)
def agent_ragas_simple_criteria_scoring(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.simple_criteria_scoring", kwargs)


@function(name="agent.ragas.sql_query_equivalence", namespace="", manifest_entry=_manifest_for("agent.ragas.sql_query_equivalence"), registry=BUILTIN_REGISTRY)
def agent_ragas_sql_query_equivalence(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.sql_query_equivalence", kwargs)


@function(name="agent.ragas.summarization", namespace="", manifest_entry=_manifest_for("agent.ragas.summarization"), registry=BUILTIN_REGISTRY)
def agent_ragas_summarization(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.summarization", kwargs)


@function(name="agent.ragas.tool_call_accuracy", namespace="", manifest_entry=_manifest_for("agent.ragas.tool_call_accuracy"), registry=BUILTIN_REGISTRY)
def agent_ragas_tool_call_accuracy(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.tool_call_accuracy", kwargs)


@function(name="agent.ragas.tool_call_f1", namespace="", manifest_entry=_manifest_for("agent.ragas.tool_call_f1"), registry=BUILTIN_REGISTRY)
def agent_ragas_tool_call_f1(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.tool_call_f1", kwargs)


@function(name="agent.ragas.topic_adherence", namespace="", manifest_entry=_manifest_for("agent.ragas.topic_adherence"), registry=BUILTIN_REGISTRY)
def agent_ragas_topic_adherence(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.ragas.topic_adherence", kwargs)


@function(name="agent.simple_agent.chat", namespace="", manifest_entry=_manifest_for("agent.simple_agent.chat"), registry=BUILTIN_REGISTRY)
def agent_simple_agent_chat(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.simple_agent.chat", kwargs)


@function(name="agent.tool.schema_match", namespace="", manifest_entry=_manifest_for("agent.tool.schema_match"), registry=BUILTIN_REGISTRY)
def agent_tool_schema_match(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.tool.schema_match", kwargs)


@function(name="agent.trajectory.step_adherence", namespace="", manifest_entry=_manifest_for("agent.trajectory.step_adherence"), registry=BUILTIN_REGISTRY)
def agent_trajectory_step_adherence(**kwargs: Any) -> Any:
    return _invoke_builtin("agent.trajectory.step_adherence", kwargs)


@function(name="aisystems.a2a.send_message", namespace="", manifest_entry=_manifest_for("aisystems.a2a.send_message"), registry=BUILTIN_REGISTRY)
def aisystems_a2a_send_message(**kwargs: Any) -> Any:
    return _invoke_builtin("aisystems.a2a.send_message", kwargs)


@function(name="aisystems.http.get", namespace="", manifest_entry=_manifest_for("aisystems.http.get"), registry=BUILTIN_REGISTRY)
def aisystems_http_get(**kwargs: Any) -> Any:
    return _invoke_builtin("aisystems.http.get", kwargs)


@function(name="aisystems.http.post", namespace="", manifest_entry=_manifest_for("aisystems.http.post"), registry=BUILTIN_REGISTRY)
def aisystems_http_post(**kwargs: Any) -> Any:
    return _invoke_builtin("aisystems.http.post", kwargs)


@function(name="aisystems.llm.chat", namespace="", manifest_entry=_manifest_for("aisystems.llm.chat"), registry=BUILTIN_REGISTRY)
def aisystems_llm_chat(**kwargs: Any) -> Any:
    return _invoke_builtin("aisystems.llm.chat", kwargs)


@function(name="aisystems.llm.messages", namespace="", manifest_entry=_manifest_for("aisystems.llm.messages"), registry=BUILTIN_REGISTRY)
def aisystems_llm_messages(**kwargs: Any) -> Any:
    return _invoke_builtin("aisystems.llm.messages", kwargs)


@function(name="aisystems.llm.responses", namespace="", manifest_entry=_manifest_for("aisystems.llm.responses"), registry=BUILTIN_REGISTRY)
def aisystems_llm_responses(**kwargs: Any) -> Any:
    return _invoke_builtin("aisystems.llm.responses", kwargs)


@function(name="data.parse_csv", namespace="", manifest_entry=_manifest_for("data.parse_csv"), registry=BUILTIN_REGISTRY)
def data_parse_csv(**kwargs: Any) -> Any:
    return _invoke_builtin("data.parse_csv", kwargs)


@function(name="data.parse_json", namespace="", manifest_entry=_manifest_for("data.parse_json"), registry=BUILTIN_REGISTRY)
def data_parse_json(**kwargs: Any) -> Any:
    return _invoke_builtin("data.parse_json", kwargs)


@function(name="data.parse_parquet", namespace="", manifest_entry=_manifest_for("data.parse_parquet"), registry=BUILTIN_REGISTRY)
def data_parse_parquet(**kwargs: Any) -> Any:
    return _invoke_builtin("data.parse_parquet", kwargs)


@function(name="data.parse_pdf", namespace="", manifest_entry=_manifest_for("data.parse_pdf"), registry=BUILTIN_REGISTRY)
def data_parse_pdf(**kwargs: Any) -> Any:
    return _invoke_builtin("data.parse_pdf", kwargs)


@function(name="agent_harness.run_task", namespace="", manifest_entry=_manifest_for("agent_harness.run_task"), registry=BUILTIN_REGISTRY)
def agent_harness_run_task(**kwargs: Any) -> Any:
    return _invoke_builtin("agent_harness.run_task", kwargs)


@function(name="agent_harness.spec", namespace="", manifest_entry=_manifest_for("agent_harness.spec"), registry=BUILTIN_REGISTRY)
def agent_harness_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("agent_harness.spec", kwargs)


@function(name="mcps.http.spec", namespace="", manifest_entry=_manifest_for("mcps.http.spec"), registry=BUILTIN_REGISTRY)
def mcps_http_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.http.spec", kwargs)


@function(name="mcps.playwright.browser.call_tool", namespace="", manifest_entry=_manifest_for("mcps.playwright.browser.call_tool"), registry=BUILTIN_REGISTRY)
def mcps_playwright_browser_call_tool(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.playwright.browser.call_tool", kwargs)


@function(name="mcps.playwright.browser.navigate", namespace="", manifest_entry=_manifest_for("mcps.playwright.browser.navigate"), registry=BUILTIN_REGISTRY)
def mcps_playwright_browser_navigate(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.playwright.browser.navigate", kwargs)


@function(name="mcps.playwright.browser.screenshot", namespace="", manifest_entry=_manifest_for("mcps.playwright.browser.screenshot"), registry=BUILTIN_REGISTRY)
def mcps_playwright_browser_screenshot(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.playwright.browser.screenshot", kwargs)


@function(name="mcps.playwright.browser.tabs", namespace="", manifest_entry=_manifest_for("mcps.playwright.browser.tabs"), registry=BUILTIN_REGISTRY)
def mcps_playwright_browser_tabs(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.playwright.browser.tabs", kwargs)


@function(name="mcps.playwright.browser.wait_for", namespace="", manifest_entry=_manifest_for("mcps.playwright.browser.wait_for"), registry=BUILTIN_REGISTRY)
def mcps_playwright_browser_wait_for(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.playwright.browser.wait_for", kwargs)


@function(name="mcps.playwright.spec", namespace="", manifest_entry=_manifest_for("mcps.playwright.spec"), registry=BUILTIN_REGISTRY)
def mcps_playwright_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.playwright.spec", kwargs)


@function(name="mcps.spec", namespace="", manifest_entry=_manifest_for("mcps.spec"), registry=BUILTIN_REGISTRY)
def mcps_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.spec", kwargs)


@function(name="mcps.stdio.spec", namespace="", manifest_entry=_manifest_for("mcps.stdio.spec"), registry=BUILTIN_REGISTRY)
def mcps_stdio_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("mcps.stdio.spec", kwargs)


@function(name="objectstore.download", namespace="", manifest_entry=_manifest_for("objectstore.download"), registry=BUILTIN_REGISTRY)
def objectstore_download(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.download", kwargs)


@function(name="objectstore.download_all", namespace="", manifest_entry=_manifest_for("objectstore.download_all"), registry=BUILTIN_REGISTRY)
def objectstore_download_all(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.download_all", kwargs)


@function(name="objectstore.entry", namespace="", manifest_entry=_manifest_for("objectstore.entry"), registry=BUILTIN_REGISTRY)
def objectstore_entry(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.entry", kwargs)


@function(name="objectstore.exists", namespace="", manifest_entry=_manifest_for("objectstore.exists"), registry=BUILTIN_REGISTRY)
def objectstore_exists(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.exists", kwargs)


@function(name="objectstore.find", namespace="", manifest_entry=_manifest_for("objectstore.find"), registry=BUILTIN_REGISTRY)
def objectstore_find(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.find", kwargs)


@function(name="objectstore.glob", namespace="", manifest_entry=_manifest_for("objectstore.glob"), registry=BUILTIN_REGISTRY)
def objectstore_glob(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.glob", kwargs)


@function(name="objectstore.read_json", namespace="", manifest_entry=_manifest_for("objectstore.read_json"), registry=BUILTIN_REGISTRY)
def objectstore_read_json(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.read_json", kwargs)


@function(name="objectstore.read_text", namespace="", manifest_entry=_manifest_for("objectstore.read_text"), registry=BUILTIN_REGISTRY)
def objectstore_read_text(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.read_text", kwargs)


@function(name="objectstore.upload", namespace="", manifest_entry=_manifest_for("objectstore.upload"), registry=BUILTIN_REGISTRY)
def objectstore_upload(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.upload", kwargs)


@function(name="objectstore.upload_all", namespace="", manifest_entry=_manifest_for("objectstore.upload_all"), registry=BUILTIN_REGISTRY)
def objectstore_upload_all(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.upload_all", kwargs)


@function(name="objectstore.write_json", namespace="", manifest_entry=_manifest_for("objectstore.write_json"), registry=BUILTIN_REGISTRY)
def objectstore_write_json(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.write_json", kwargs)


@function(name="objectstore.write_text", namespace="", manifest_entry=_manifest_for("objectstore.write_text"), registry=BUILTIN_REGISTRY)
def objectstore_write_text(**kwargs: Any) -> Any:
    return _invoke_builtin("objectstore.write_text", kwargs)


@function(name="envs.agent_harness.run_task", namespace="", manifest_entry=_manifest_for("envs.agent_harness.run_task"), registry=BUILTIN_REGISTRY)
def envs_agent_harness_run_task(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.agent_harness.run_task", kwargs)


@function(name="envs.agent_harness.spec", namespace="", manifest_entry=_manifest_for("envs.agent_harness.spec"), registry=BUILTIN_REGISTRY)
def envs_agent_harness_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.agent_harness.spec", kwargs)


@function(name="envs.browser.spec", namespace="", manifest_entry=_manifest_for("envs.browser.spec"), registry=BUILTIN_REGISTRY)
def envs_browser_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.browser.spec", kwargs)


@function(name="envs.mcp.http.spec", namespace="", manifest_entry=_manifest_for("envs.mcp.http.spec"), registry=BUILTIN_REGISTRY)
def envs_mcp_http_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.mcp.http.spec", kwargs)


@function(name="envs.mcp.playwright.spec", namespace="", manifest_entry=_manifest_for("envs.mcp.playwright.spec"), registry=BUILTIN_REGISTRY)
def envs_mcp_playwright_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.mcp.playwright.spec", kwargs)


@function(name="envs.mcp.stdio.spec", namespace="", manifest_entry=_manifest_for("envs.mcp.stdio.spec"), registry=BUILTIN_REGISTRY)
def envs_mcp_stdio_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.mcp.stdio.spec", kwargs)


@function(name="envs.shell.spec", namespace="", manifest_entry=_manifest_for("envs.shell.spec"), registry=BUILTIN_REGISTRY)
def envs_shell_spec(**kwargs: Any) -> Any:
    return _invoke_builtin("envs.shell.spec", kwargs)


@function(name="http.get", namespace="", manifest_entry=_manifest_for("http.get"), registry=BUILTIN_REGISTRY)
def http_get(**kwargs: Any) -> Any:
    return _invoke_builtin("http.get", kwargs)


@function(name="http.post", namespace="", manifest_entry=_manifest_for("http.post"), registry=BUILTIN_REGISTRY)
def http_post(**kwargs: Any) -> Any:
    return _invoke_builtin("http.post", kwargs)


@function(name="http.request", namespace="", manifest_entry=_manifest_for("http.request"), registry=BUILTIN_REGISTRY)
def http_request(**kwargs: Any) -> Any:
    return _invoke_builtin("http.request", kwargs)


@function(name="ranking.elo_expected_score", namespace="", manifest_entry=_manifest_for("ranking.elo_expected_score"), registry=BUILTIN_REGISTRY)
def ranking_elo_expected_score(**kwargs: Any) -> Any:
    return _invoke_builtin("ranking.elo_expected_score", kwargs)


@function(name="ranking.elo_final_rating", namespace="", manifest_entry=_manifest_for("ranking.elo_final_rating"), registry=BUILTIN_REGISTRY)
def ranking_elo_final_rating(**kwargs: Any) -> Any:
    return _invoke_builtin("ranking.elo_final_rating", kwargs)


@function(name="ranking.elo_rating_delta", namespace="", manifest_entry=_manifest_for("ranking.elo_rating_delta"), registry=BUILTIN_REGISTRY)
def ranking_elo_rating_delta(**kwargs: Any) -> Any:
    return _invoke_builtin("ranking.elo_rating_delta", kwargs)


@function(name="simulators.limits", namespace="", manifest_entry=_manifest_for("simulators.limits"), registry=BUILTIN_REGISTRY)
def simulators_limits(**kwargs: Any) -> Any:
    return _invoke_builtin("simulators.limits", kwargs)


@function(name="simulators.observer", namespace="", manifest_entry=_manifest_for("simulators.observer"), registry=BUILTIN_REGISTRY)
def simulators_observer(**kwargs: Any) -> Any:
    return _invoke_builtin("simulators.observer", kwargs)


@function(name="simulators.run", namespace="", manifest_entry=_manifest_for("simulators.run"), registry=BUILTIN_REGISTRY)
def simulators_run(**kwargs: Any) -> Any:
    return _invoke_builtin("simulators.run", kwargs)


@function(name="string.extract_from_fence", namespace="", manifest_entry=_manifest_for("string.extract_from_fence"), registry=BUILTIN_REGISTRY)
def string_extract_from_fence(**kwargs: Any) -> Any:
    return _invoke_builtin("string.extract_from_fence", kwargs)


@function(name="zip.untar", namespace="", manifest_entry=_manifest_for("zip.untar"), registry=BUILTIN_REGISTRY)
def zip_untar(**kwargs: Any) -> Any:
    return _invoke_builtin("zip.untar", kwargs)


@function(name="zip.unzip", namespace="", manifest_entry=_manifest_for("zip.unzip"), registry=BUILTIN_REGISTRY)
def zip_unzip(**kwargs: Any) -> Any:
    return _invoke_builtin("zip.unzip", kwargs)


@environment(name="browser", namespace="agentcicd", manifest_entry=_environment_manifest("browser", "BrowserSpec"), registry=BUILTIN_REGISTRY)
class Browser(Environment[BrowserSpec]):
    def __init__(self, spec: BrowserSpec) -> None:
        self.spec = spec


@environment(name="shell", namespace="agentcicd", manifest_entry=_environment_manifest("shell", "ShellSpec"), registry=BUILTIN_REGISTRY)
class Shell(Environment[ShellSpec]):
    def __init__(self, spec: ShellSpec) -> None:
        self.spec = spec


@environment(name="agent_harness", namespace="agentcicd", manifest_entry=_environment_manifest("agent_harness", "AgentHarnessSpec"), registry=BUILTIN_REGISTRY)
class AgentHarness(Environment[AgentHarnessSpec]):
    def __init__(self, spec: AgentHarnessSpec) -> None:
        self.spec = spec


@environment(name="mcp.http", namespace="agentcicd", manifest_entry=_environment_manifest("mcp.http", "McpHttpSpec"), registry=BUILTIN_REGISTRY)
class McpHttp(Environment[McpHttpSpec]):
    def __init__(self, spec: McpHttpSpec) -> None:
        self.spec = spec


@environment(name="mcp.stdio", namespace="agentcicd", manifest_entry=_environment_manifest("mcp.stdio", "McpStdioSpec"), registry=BUILTIN_REGISTRY)
class McpStdio(Environment[McpStdioSpec]):
    def __init__(self, spec: McpStdioSpec) -> None:
        self.spec = spec


@environment(name="mcp.playwright", namespace="agentcicd", manifest_entry=_environment_manifest("mcp.playwright", "McpPlaywrightSpec"), registry=BUILTIN_REGISTRY)
class McpPlaywright(Environment[McpPlaywrightSpec]):
    def __init__(self, spec: McpPlaywrightSpec) -> None:
        self.spec = spec
