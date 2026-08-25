from dataclasses import replace

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.runtime.invokers.spark import SparkUdfRuntimeInvoker
from agentcicd.sql.semantics.types import parse_type_spec


USER_CALLBACK_TYPE = (
    "FUNCTION<(agent_response VARIANT, state VARIANT, environments ANY, turn INTEGER) "
    "RETURNS STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>>"
)
AGENT_CALLBACK_TYPE = (
    "FUNCTION<(request VARIANT, state VARIANT, environments ANY, turn INTEGER) "
    "RETURNS STRUCT<response: VARIANT, state: VARIANT>>"
)
OBSERVER_CALLBACK_TYPE = (
    "FUNCTION<(event VARIANT, state VARIANT, environments ANY) "
    "RETURNS STRUCT<observation: VARIANT, artifacts: ARRAY<VARIANT>, state: VARIANT>>"
)


def _callback_fixture(name: str, role: str, parameters: list[dict], return_type: str) -> dict:
    return {
        "name": name,
        "type": "python",
        "signature": {"parameters": parameters},
        "return_type_sql": return_type,
        "capabilities": ["row_callable", role],
    }


def _user_fixture(name: str = "helpers.user_fn") -> dict:
    return _callback_fixture(
        name,
        "simulator_user",
        [
            {"name": "agent_response", "type_sql": "VARIANT"},
            {"name": "state", "type_sql": "VARIANT"},
            {"name": "environments", "type_sql": "ANY"},
            {"name": "turn", "type_sql": "INTEGER"},
        ],
        "STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>",
    )


def _agent_fixture(name: str = "helpers.agent_fn") -> dict:
    return _callback_fixture(
        name,
        "simulator_agent",
        [
            {"name": "request", "type_sql": "VARIANT"},
            {"name": "state", "type_sql": "VARIANT"},
            {"name": "environments", "type_sql": "ANY"},
            {"name": "turn", "type_sql": "INTEGER"},
        ],
        "STRUCT<response: VARIANT, state: VARIANT>",
    )


def _observer_fixture(name: str = "helpers.observer_fn") -> dict:
    return _callback_fixture(
        name,
        "simulator_observer",
        [
            {"name": "event", "type_sql": "VARIANT"},
            {"name": "state", "type_sql": "VARIANT"},
            {"name": "environments", "type_sql": "ANY"},
        ],
        "STRUCT<observation: VARIANT, artifacts: ARRAY<VARIANT>, state: VARIANT>",
    )


def test_function_type_parser_normalizes_arguments_and_return_type() -> None:
    parsed = parse_type_spec(USER_CALLBACK_TYPE.replace("INTEGER", "INT"))

    assert parsed.normalized() == USER_CALLBACK_TYPE


def test_simulator_run_function_references_are_validated_and_lowered() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.codex';

    DECLARE INPUT agent_secret SECRET DEFAULT 'secret.codex';

    CREATE BATCH TABLE out
    SELECT simulators.run(
      input=prompt,
      user=helpers.user_fn,
      agent=helpers.agent_fn,
      observers=ARRAY(
        simulators.observer(
          callback=helpers.observer_fn,
          schedule=ARRAY('after_turn', 'final')
        )
      ),
      environments=ARRAY(
        envs.browser.spec(session_id='browser', start_url=start_url),
        envs.agent_harness.spec(
          session_id='agent',
          aisystem=target_agent,
          workdir=workspace_root,
          secret_id=agent_secret
        )
      ),
      reuse='none',
      limits=simulators.limits(max_turns=5, timeout_seconds=30.0)
    ) AS simulated
    FROM prepared;
    """

    lowered = EngineEntrypoint(
        script,
        registered_functions=[_user_fixture(), _agent_fixture(), _observer_fixture()],
    ).lower_script()

    assert len(lowered) == 3
    assert lowered[0] == "DECLARE OR REPLACE VARIABLE target_agent STRING DEFAULT 'aisystem.codex'"
    assert lowered[1] == "DECLARE OR REPLACE VARIABLE agent_secret STRING DEFAULT 'secret.codex'"
    assert "SIMULATORS_RUN(" in lowered[2]
    assert "'helpers.user_fn'" in lowered[2]
    assert "'helpers.agent_fn'" in lowered[2]
    assert "SIMULATORS_OBSERVER('helpers.observer_fn', ARRAY('after_turn', 'final'), NULL)" in lowered[2]
    assert "ENVS_BROWSER_SPEC('browser', start_url, NULL, NULL, NULL, NULL, NULL)" in lowered[2]
    assert "ENVS_AGENT_HARNESS_SPEC('agent', target_agent, workspace_root, agent_secret, NULL)" in lowered[2]
    assert "SIMULATORS_LIMITS(5, 30.0)" in lowered[2]


def test_envs_agent_harness_run_task_lowers_with_environment_spec() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.codex';

    DECLARE INPUT agent_secret SECRET DEFAULT 'secret.codex';

    CREATE STREAM TABLE harness_runs
    SELECT
      case_id,
      envs.agent_harness.run_task(
        env=envs.agent_harness.spec(
          session_id=concat('agent-', case_id),
          aisystem=target_agent,
          workdir=workspace_root,
          secret_id=agent_secret
        ),
        task=issue_text,
        timeout_seconds=600
      ) AS result
    FROM prepared;
    """

    lowered = EngineEntrypoint(script).lower_script()

    assert len(lowered) == 3
    assert lowered[0] == "DECLARE OR REPLACE VARIABLE target_agent STRING DEFAULT 'aisystem.codex'"
    assert lowered[1] == "DECLARE OR REPLACE VARIABLE agent_secret STRING DEFAULT 'secret.codex'"
    assert "ENVS_AGENT_HARNESS_RUN_TASK(" in lowered[2]
    assert "ENVS_AGENT_HARNESS_SPEC(CONCAT('agent-', case_id), target_agent, workspace_root, agent_secret, NULL)" in lowered[2]


def test_envs_agent_harness_spec_lowers_with_http_mcp_spec() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.codex';

    DECLARE INPUT agent_secret SECRET DEFAULT 'secret.codex';
    DECLARE INPUT mcp_secret SECRET DEFAULT 'secret.playwright';

    CREATE BATCH TABLE env_specs
    SELECT
      envs.agent_harness.spec(
        session_id='agent',
        aisystem=target_agent,
        workdir=workspace_root,
        secret_id=agent_secret,
        mcps=ARRAY(
          envs.mcp.http.spec(
            name='playwright',
            endpoint=playwright_mcp_endpoint,
            required=true,
            secret_id=mcp_secret,
            allow_tools=ARRAY('browser_navigate'),
            deny_tools=ARRAY('browser_install'),
            headers={'X-Test': 'yes'}
          )
        )
      ) AS env_spec
    FROM prepared;
    """

    lowered = EngineEntrypoint(script).lower_script()

    assert len(lowered) == 4
    assert "ENVS_AGENT_HARNESS_SPEC(" in lowered[3]
    assert "ENVS_MCP_HTTP_SPEC(" in lowered[3]
    assert "ARRAY('browser_navigate')" in lowered[3]
    assert "ARRAY('browser_install')" in lowered[3]


def test_builtin_udfs_default_to_function_runner_runtime() -> None:
    registry = EngineEntrypoint(
        """
        CREATE BATCH TABLE harness_runs
        SELECT
          string.extract_from_fence(issue_text) AS fenced,
          envs.agent_harness.spec('agent', target_agent, workspace_root, agent_secret) AS env_spec,
          envs.agent_harness.run_task(
            env = envs.agent_harness.spec('agent', target_agent, workspace_root, agent_secret),
            task = issue_text
          ) AS result
        FROM prepared;
        """
    ).registry()

    for udf_name in [
        "string.extract_from_fence",
        "envs.agent_harness.spec",
        "envs.agent_harness.run_task",
    ]:
        definition = registry.resolve(udf_name)
        assert definition is not None
        assert definition.metadata["execution_runtime"] == "function_runner"
        assert definition.metadata["entrypoint_name"] == udf_name.rsplit(".", 1)[-1]


def test_envs_agent_harness_run_task_runtime_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = EngineEntrypoint(
        """
        CREATE BATCH TABLE harness_runs
        SELECT envs.agent_harness.run_task(
          env = envs.agent_harness.spec('agent', target_agent, workspace_root, agent_secret),
          task = issue_text
        ) AS result
        FROM prepared;
        """
    ).registry()

    definition = registry.resolve("envs.agent_harness.run_task")

    assert definition is not None
    assert definition.metadata["execution_runtime"] == "function_runner"
    monkeypatch.delenv("AGENTCICD_REQUIRE_FUNCTION_RUNNER_UDFS", raising=False)
    assert SparkUdfRuntimeInvoker().can_handle(definition) is False

    remote_definition = replace(
        definition,
        metadata={
            **definition.metadata,
            "base_url": "http://agent-harness-runtime",
            "invoke_path": "/invoke/run_task",
        },
    )
    assert SparkUdfRuntimeInvoker().can_handle(remote_definition) is False

    monkeypatch.setenv("AGENTCICD_REQUIRE_FUNCTION_RUNNER_UDFS", "1")
    assert SparkUdfRuntimeInvoker().can_handle(definition) is False


def test_envs_agent_harness_sql_ux_lowers_with_spec_and_run_task() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.codex';

    DECLARE INPUT agent_secret SECRET DEFAULT 'secret.codex';

    CREATE STREAM TABLE harness_runs
    SELECT
      case_id,
      envs.agent_harness.run_task(
        env=envs.agent_harness.spec(
          session_id='agent',
          aisystem=target_agent,
          workdir=workspace_root,
          secret_id=agent_secret
        ),
        task=issue_text,
        timeout_seconds=600
      ) AS result
    FROM prepared;
    """

    lowered = EngineEntrypoint(script).lower_script()

    assert len(lowered) == 3
    assert lowered[0] == "DECLARE OR REPLACE VARIABLE target_agent STRING DEFAULT 'aisystem.codex'"
    assert lowered[1] == "DECLARE OR REPLACE VARIABLE agent_secret STRING DEFAULT 'secret.codex'"
    assert "ENVS_AGENT_HARNESS_RUN_TASK(" in lowered[2]
    assert "ENVS_AGENT_HARNESS_SPEC('agent', target_agent, workspace_root, agent_secret, NULL)" in lowered[2]


def test_agent_harness_environment_requires_llm_aisystem_interface() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'agent_a2a'
    DEFAULT 'aisystem.a2a';

    DECLARE INPUT agent_secret SECRET DEFAULT 'secret.codex';

    CREATE BATCH TABLE out
    SELECT envs.agent_harness.spec(session_id='agent', aisystem=target_agent, workdir=workspace_root, secret_id=agent_secret) AS env
    FROM prepared;
    """

    with pytest.raises(ValueError, match="requires one of"):
        EngineEntrypoint(script).resolve()


def test_agent_harness_environment_rejects_non_declared_aisystem() -> None:
    script = """
    DECLARE INPUT target_agent STRING DEFAULT 'aisystem.codex';

    DECLARE INPUT agent_secret SECRET DEFAULT 'secret.codex';

    CREATE BATCH TABLE out
    SELECT envs.agent_harness.spec(session_id='agent', aisystem=target_agent, workdir=workspace_root, secret_id=agent_secret) AS env
    FROM prepared;
    """

    with pytest.raises(ValueError, match="must reference a declared AISYSTEM input"):
        EngineEntrypoint(script).resolve()


def test_old_env_agent_harness_namespace_is_not_registered() -> None:
    script = """
    DECLARE INPUT target_agent AISYSTEM
    WITH interface = 'llm.chat'
    DEFAULT 'aisystem.codex';

    CREATE BATCH TABLE out
    SELECT env.agent_harness(session_id='agent', aisystem=target_agent, workdir=workspace_root) AS env
    FROM prepared;
    """

    with pytest.raises(ValueError):
        EngineEntrypoint(script).resolve()


def test_simulator_run_rejects_non_function_argument() -> None:
    script = """
    CREATE BATCH TABLE out
    SELECT simulators.run(input=prompt, user='not_a_function', agent=helpers.agent_fn) AS simulated
    FROM prepared;
    """

    with pytest.raises(ValueError, match="must be a function reference"):
        EngineEntrypoint(script, registered_functions=[_agent_fixture()]).resolve()


def test_simulator_run_rejects_mismatched_function_signature() -> None:
    bad_user = _callback_fixture(
        "helpers.bad_user_fn",
        "simulator_user",
        [
            {"name": "agent_response", "type_sql": "VARIANT"},
            {"name": "state", "type_sql": "VARIANT"},
            {"name": "environments", "type_sql": "ANY"},
            {"name": "turn", "type_sql": "STRING"},
        ],
        "STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>",
    )
    script = """
    CREATE BATCH TABLE out
    SELECT simulators.run(input=prompt, user=helpers.bad_user_fn, agent=helpers.agent_fn) AS simulated
    FROM prepared;
    """

    with pytest.raises(ValueError, match="does not match argument 'user'"):
        EngineEntrypoint(script, registered_functions=[bad_user, _agent_fixture()]).resolve()


def test_simulator_run_rejects_local_sql_callback_even_when_signature_matches() -> None:
    script = """
    CREATE FUNCTION helpers.user_fn(
      agent_response VARIANT,
      state VARIANT,
      environments ANY,
      turn INTEGER
    )
    RETURNS STRUCT<request: VARIANT, terminate: BOOLEAN, state: VARIANT>
    RETURN named_struct('request', agent_response, 'terminate', true, 'state', state);

    CREATE BATCH TABLE out
    SELECT simulators.run(input=prompt, user=helpers.user_fn, agent=helpers.agent_fn) AS simulated
    FROM prepared;
    """

    with pytest.raises(ValueError, match="local SQL function"):
        EngineEntrypoint(script, registered_functions=[_agent_fixture()]).resolve()


def test_simulator_run_rejects_callback_without_row_callable_capability() -> None:
    user = _user_fixture()
    user["capabilities"] = ["simulator_user"]
    script = """
    CREATE BATCH TABLE out
    SELECT simulators.run(input=prompt, user=helpers.user_fn, agent=helpers.agent_fn) AS simulated
    FROM prepared;
    """

    with pytest.raises(ValueError, match="row_callable"):
        EngineEntrypoint(script, registered_functions=[user, _agent_fixture()]).resolve()


def test_simulator_run_rejects_callback_with_wrong_simulator_role() -> None:
    user = _user_fixture()
    user["capabilities"] = ["row_callable", "simulator_agent"]
    script = """
    CREATE BATCH TABLE out
    SELECT simulators.run(input=prompt, user=helpers.user_fn, agent=helpers.agent_fn) AS simulated
    FROM prepared;
    """

    with pytest.raises(ValueError, match="simulator role 'simulator_user'"):
        EngineEntrypoint(script, registered_functions=[user, _agent_fixture()]).resolve()


def test_simulator_observer_rejects_wrong_callback_role_at_spec_builder_call_site() -> None:
    observer = _observer_fixture()
    observer["capabilities"] = ["row_callable", "simulator_agent"]
    script = """
    CREATE BATCH TABLE out
    SELECT simulators.observer(callback=helpers.observer_fn, schedule=ARRAY('final')) AS observer
    FROM prepared;
    """

    with pytest.raises(ValueError, match="simulator role 'simulator_observer'"):
        EngineEntrypoint(script, registered_functions=[observer]).resolve()


def test_simulator_observer_rejects_unsupported_literal_schedule() -> None:
    script = """
    CREATE BATCH TABLE out
    SELECT simulators.observer(callback=helpers.observer_fn, schedule=ARRAY('after_turn', '1s')) AS observer
    FROM prepared;
    """

    with pytest.raises(ValueError, match="unsupported value '1s'"):
        EngineEntrypoint(script, registered_functions=[_observer_fixture()]).resolve()
