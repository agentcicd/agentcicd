from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend
from agentcicd.sql.wrapped_validation import WrappedValidationError

from .fixtures import (
    E2ERuntimeServer,
    assert_schema_sidecar,
    assert_stage_completed,
    clean_cell,
    clear_local_e2e_udfs,
    error_cell,
    local_udf_specs,
    read_table_cells,
    register_local_e2e_udfs,
    runtime_specs,
    write_wrapped_table,
)


ARTIFACT_ROOT = Path(__file__).parent / "artifacts"

RECIPE_KEYS = {
    "id",
    "name",
    "description",
    "markers",
    "include_cells",
    "xfail",
    "inputs",
    "fixtures",
    "execution",
    "expected",
}
EXECUTION_KEYS = {"kind", "table_format", "env", "reuse", "attempts", "run_id", "debug"}
EXPECTED_KEYS = {"tables", "schemas", "manifests", "fixture_calls", "validation", "publishes", "reruns", "debug_sidecars"}
FIXTURE_KINDS = {"local_udf", "http_runtime", "publication_store", "annotation_store", "fake_codex_harness"}
HARNESS_RUN_RESULT_TYPE_SQL = (
    "STRUCT<"
    "status: STRING, "
    "final_output: STRING, "
    "transcript: ARRAY<VARIANT>, "
    "artifacts: ARRAY<STRUCT<"
    "kind: STRING, "
    "uri: STRING, "
    "path: STRING, "
    "name: STRING, "
    "mime_type: STRING, "
    "size_bytes: BIGINT, "
    "metadata: VARIANT"
    ">>, "
    "error: STRUCT<code: STRING, message: STRING, retryable: BOOLEAN, metadata: VARIANT>, "
    "duration_ms: BIGINT, "
    "metadata: VARIANT"
    ">"
)


@dataclass(frozen=True)
class E2EArtifact:
    root: Path
    metadata: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.metadata["id"])

    def relpath(self, path: str) -> Path:
        return self.root / path


def discover_e2e_artifacts() -> list[E2EArtifact]:
    artifacts: list[E2EArtifact] = []
    for root in sorted(ARTIFACT_ROOT.glob("e2e-*")):
        if not root.is_dir():
            continue
        metadata = _load_yaml(root / "recipe.yaml")
        _validate_recipe(root, metadata)
        artifacts.append(E2EArtifact(root=root, metadata=metadata))
    return artifacts


def artifact_pytest_params() -> list[Any]:
    params = []
    for artifact in discover_e2e_artifacts():
        marks = [getattr(pytest.mark, marker) for marker in artifact.metadata.get("markers", [])]
        xfail = artifact.metadata.get("xfail")
        if isinstance(xfail, dict):
            marks.append(pytest.mark.xfail(reason=xfail["reason"], strict=bool(xfail.get("strict", True))))
        params.append(pytest.param(artifact, id=artifact.id, marks=marks))
    return params


def run_e2e_artifact(artifact: E2EArtifact, *, spark, tmp_path: Path) -> None:
    _validate_expected_files(artifact)
    execution = artifact.metadata.get("execution") or {}
    kind = execution.get("kind", "recipe")
    if kind == "negative_validation":
        _run_negative_validation_artifact(artifact)
        return
    if kind == "manifest_reuse":
        _run_manifest_reuse_artifact(artifact, spark=spark, tmp_path=tmp_path)
        return
    if kind == "rerun_sequence":
        _run_rerun_sequence_artifact(artifact, spark=spark, tmp_path=tmp_path)
        return
    if kind != "recipe":
        raise AssertionError(f"{artifact.id}: unknown execution kind {kind!r}")
    _run_recipe_artifact(artifact, spark=spark, tmp_path=tmp_path, recipe_name="recipe.sql")


def _run_recipe_artifact(
    artifact: E2EArtifact,
    *,
    spark,
    tmp_path: Path,
    recipe_name: str,
    env: dict[str, str] | None = None,
) -> Any:
    with ExitStack() as stack:
        context = _materialize_inputs(artifact, spark=spark, tmp_path=tmp_path)
        fixture_specs = _load_fixture_specs(artifact)
        registered_functions: list[dict[str, Any]] = []
        runtime_server: E2ERuntimeServer | None = None
        fixture_env: dict[str, str] = {}

        if any(spec["kind"] == "local_udf" for spec in fixture_specs):
            register_local_e2e_udfs()
            registered_functions.extend(local_udf_specs())
            stack.callback(clear_local_e2e_udfs)
        if any(spec["kind"] in {"http_runtime", "fake_codex_harness"} for spec in fixture_specs):
            runtime_server = stack.enter_context(E2ERuntimeServer())
            context["runtime_base_url"] = runtime_server.base_url
            if any(spec["kind"] == "http_runtime" for spec in fixture_specs):
                registered_functions.extend(runtime_specs(runtime_server.base_url))
            if any(spec["kind"] == "fake_codex_harness" for spec in fixture_specs):
                registered_functions.append(_fake_agent_harness_runtime_spec(runtime_server.base_url))
        for spec in fixture_specs:
            if spec["kind"] == "fake_codex_harness":
                fixture_context, env_updates = _prepare_fake_codex_harness_fixture(spec, spark=spark, tmp_path=tmp_path)
                context.update(fixture_context)
                fixture_env.update(env_updates)

        recipe_sql = artifact.relpath(recipe_name).read_text(encoding="utf-8").format(**context)
        backend = SparkExecutionBackend(
            spark,
            working_dir=str(tmp_path),
            debug=(artifact.metadata.get("execution") or {}).get("debug"),
        )
        execution_env = _string_env((artifact.metadata.get("execution") or {}).get("env") or {})
        execution_env.update(fixture_env)
        if env:
            execution_env.update(env)
        with _patched_env(execution_env):
            result = EngineEntrypoint(recipe_sql, registered_functions=registered_functions).execute(
                backend,
                include_cells=bool(artifact.metadata["include_cells"]),
            )

        expected = _load_expected(artifact)
        _assert_tables(expected.get("tables"), spark=spark, tmp_path=tmp_path, artifact=artifact)
        _assert_schemas(expected.get("schemas"), tmp_path=tmp_path, artifact=artifact)
        _assert_manifests(expected.get("manifests"), tmp_path=tmp_path, artifact=artifact)
        _assert_debug_sidecars(expected.get("debug_sidecars"), tmp_path=tmp_path, artifact=artifact)
        _assert_publishes(expected.get("publishes"), tmp_path=tmp_path, artifact=artifact)
        if runtime_server is not None:
            _assert_fixture_calls(expected.get("fixture_calls"), runtime_server=runtime_server, artifact=artifact)
        return result


def _run_negative_validation_artifact(artifact: E2EArtifact) -> None:
    expected = _load_expected(artifact)
    validation = expected.get("validation") or {}
    cases = validation.get("cases")
    if not isinstance(cases, list):
        raise AssertionError(f"{artifact.id}: expected/validation.yaml must declare cases")
    for case in cases:
        snippet = case.get("snippet")
        script = case.get("script")
        expected_message = case["expected_message"]
        if script is None:
            script = f"CREATE BATCH TABLE out\n{snippet};"
        with pytest.raises(WrappedValidationError, match=expected_message):
            EngineEntrypoint(script).compile_plan(include_cells=True)


def _run_manifest_reuse_artifact(artifact: E2EArtifact, *, spark, tmp_path: Path) -> None:
    context = _materialize_inputs(artifact, spark=spark, tmp_path=tmp_path)
    first_dir = tmp_path / "first"
    reused_dir = tmp_path / "reused"
    changed_dir = tmp_path / "changed"

    first_sql = artifact.relpath("recipe.sql").read_text(encoding="utf-8").format(**context, raw=context["raw_v1"])
    EngineEntrypoint(first_sql).execute(SparkExecutionBackend(spark, working_dir=str(first_dir)), include_cells=True)

    completed = ",".join(["prepared", "scored", "summary"])
    reuse_env = {
        "AGENTCICD_PREVIOUS_RUN_OBJECT_URI": first_dir.as_posix(),
        "AGENTCICD_COMPLETED_BATCH_TABLES": completed,
    }
    reused_result = _execute_rendered_sql(
        artifact.relpath("recipe.sql").read_text(encoding="utf-8").format(**context, raw=context["raw_v1"]),
        spark=spark,
        working_dir=reused_dir,
        env=reuse_env,
    )
    changed_result = _execute_rendered_sql(
        artifact.relpath("recipe.sql").read_text(encoding="utf-8").format(**context, raw=context["raw_v2"]),
        spark=spark,
        working_dir=changed_dir,
        env=reuse_env,
    )

    expected = _load_expected(artifact)
    reuse_expected = ((expected.get("manifests") or {}).get("reuse") or {})
    skipped = {(event.step_kind, event.step_name) for event in reused_result.events if event.status == "skipped"}
    assert skipped == {("create_batch_table", stage) for stage in reuse_expected.get("reused_tables", [])}
    changed_skipped = {(event.step_kind, event.step_name) for event in changed_result.events if event.status == "skipped"}
    assert changed_skipped == {("create_batch_table", stage) for stage in reuse_expected.get("changed_reused_tables", [])}
    for stage in reuse_expected.get("stages", []):
        assert_stage_completed(first_dir, stage)
        assert_schema_sidecar(first_dir, stage)
        assert_stage_completed(changed_dir, stage)
        assert_schema_sidecar(changed_dir, stage)


def _run_rerun_sequence_artifact(artifact: E2EArtifact, *, spark, tmp_path: Path) -> None:
    execution = artifact.metadata.get("execution") or {}
    attempts = execution.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise AssertionError(f"{artifact.id}: rerun_sequence requires execution.attempts")

    fixture_specs = _load_fixture_specs(artifact)
    run_id = f"{execution.get('run_id') or artifact.id}-{uuid.uuid4().hex}"
    attempt_dirs: dict[str, Path] = {}
    attempt_results: dict[str, Any] = {}
    attempt_runtime_calls: dict[str, dict[str, list[dict[str, Any]]]] = {}

    with ExitStack() as stack:
        registered_functions: list[dict[str, Any]] = []
        runtime_server: E2ERuntimeServer | None = None
        if any(spec["kind"] == "local_udf" for spec in fixture_specs):
            register_local_e2e_udfs()
            registered_functions.extend(local_udf_specs())
            stack.callback(clear_local_e2e_udfs)
        if any(spec["kind"] == "http_runtime" for spec in fixture_specs):
            runtime_server = stack.enter_context(E2ERuntimeServer())
            registered_functions.extend(runtime_specs(runtime_server.base_url))

        for attempt in attempts:
            if not isinstance(attempt, dict) or "id" not in attempt:
                raise AssertionError(f"{artifact.id}: each rerun attempt must declare id")
            attempt_id = str(attempt["id"])
            attempt_dir = tmp_path / "attempts" / attempt_id
            attempt_dirs[attempt_id] = attempt_dir
            input_metadata = dict(artifact.metadata.get("inputs") or {})
            input_metadata.update(dict(attempt.get("inputs") or {}))
            context = _materialize_inputs_from_mapping(
                artifact,
                input_metadata,
                spark=spark,
                tmp_path=tmp_path / "rerun_inputs",
                stable_paths=True,
            )
            if runtime_server is not None:
                context["runtime_base_url"] = runtime_server.base_url
            recipe_name = str(attempt.get("recipe") or "recipe.sql")
            recipe_sql = artifact.relpath(recipe_name).read_text(encoding="utf-8").format(**context)
            previous_attempt = attempt.get("previous_attempt")
            env = _rerun_attempt_env(
                artifact,
                attempt,
                attempt_dirs=attempt_dirs,
                run_id=run_id,
                base_env=_string_env((execution.get("env") or {})),
            )
            backend = SparkExecutionBackend(spark, working_dir=str(attempt_dir))
            before_call_count = len(runtime_server.recorder.calls) if runtime_server is not None else 0
            try:
                with _patched_env(env):
                    result = EngineEntrypoint(recipe_sql, registered_functions=registered_functions).execute(
                        backend,
                        include_cells=bool(artifact.metadata["include_cells"]),
                    )
            except Exception as exc:
                expected_failure = attempt.get("expect_failure")
                if not expected_failure:
                    raise
                expected_stage = expected_failure.get("stage")
                expected_message = str(expected_failure.get("error_contains") or "")
                if expected_stage and str(expected_stage) not in str(exc):
                    matching_events = []
                else:
                    matching_events = []
                if expected_message and expected_message not in str(exc):
                    raise AssertionError(
                        f"{artifact.id}:{attempt_id}: failure did not contain {expected_message!r}: {exc}"
                    ) from exc
                result = None
            else:
                if attempt.get("expect_failure"):
                    raise AssertionError(f"{artifact.id}:{attempt_id}: expected failure but attempt completed")
                attempt_results[attempt_id] = result
            finally:
                if runtime_server is not None:
                    new_calls = runtime_server.recorder.calls[before_call_count:]
                    by_function: dict[str, list[dict[str, Any]]] = {}
                    for call in new_calls:
                        by_function.setdefault(str(call["function_name"]), []).append(call)
                    attempt_runtime_calls[attempt_id] = by_function
            if previous_attempt is not None and str(previous_attempt) not in attempt_dirs:
                raise AssertionError(f"{artifact.id}:{attempt_id}: unknown previous_attempt {previous_attempt}")

    expected = _load_expected(artifact)
    _assert_reruns(
        expected.get("reruns"),
        attempt_results=attempt_results,
        attempt_dirs=attempt_dirs,
        attempt_runtime_calls=attempt_runtime_calls,
        artifact=artifact,
    )


def _rerun_attempt_env(
    artifact: E2EArtifact,
    attempt: dict[str, Any],
    *,
    attempt_dirs: dict[str, Path],
    run_id: str,
    base_env: dict[str, str],
) -> dict[str, str]:
    env = dict(base_env)
    env.setdefault("AGENTCICD_ORGANIZATION_ID", "e2e-org")
    env.setdefault("AGENTCICD_RECIPE_ID", artifact.id)
    env.setdefault("AGENTCICD_RUN_ID", run_id)
    previous_attempt = attempt.get("previous_attempt")
    if previous_attempt is not None:
        previous_dir = attempt_dirs.get(str(previous_attempt))
        if previous_dir is None:
            raise AssertionError(f"{artifact.id}: unknown previous_attempt {previous_attempt}")
        env["AGENTCICD_PREVIOUS_RUN_OBJECT_URI"] = previous_dir.as_posix()
    reusable = attempt.get("reuse") or attempt.get("completed_tables") or []
    if isinstance(reusable, dict):
        batch_tables = reusable.get("batch") or reusable.get("tables") or []
    else:
        batch_tables = reusable
    if batch_tables:
        env["AGENTCICD_COMPLETED_BATCH_TABLES"] = ",".join(str(item) for item in batch_tables)
    env.update(_string_env(attempt.get("env") or {}))
    return env


def _assert_reruns(
    spec: dict[str, Any] | None,
    *,
    attempt_results: dict[str, Any],
    attempt_dirs: dict[str, Path],
    attempt_runtime_calls: dict[str, dict[str, list[dict[str, Any]]]],
    artifact: E2EArtifact,
) -> None:
    if not spec:
        return
    for attempt_id, attempt_spec in (spec.get("attempts") or {}).items():
        result = attempt_results.get(str(attempt_id))
        if result is None and not attempt_spec.get("failed"):
            raise AssertionError(f"{artifact.id}:{attempt_id}: missing successful attempt result")
        if result is not None:
            events = {(event.step_kind, event.step_name, event.status) for event in result.events}
            skipped = {
                event.step_name
                for event in result.events
                if event.status == "skipped" and event.step_kind in {"create_batch_table", "create_stream_table"}
            }
            completed = {
                event.step_name
                for event in result.events
                if event.status == "completed" and event.step_kind in {"create_batch_table", "create_stream_table"}
            }
            for stage in attempt_spec.get("skipped", []):
                assert stage in skipped, f"{artifact.id}:{attempt_id}: expected skipped stage {stage}; events={events}"
            for stage in attempt_spec.get("recomputed", []):
                assert stage in completed, f"{artifact.id}:{attempt_id}: expected recomputed stage {stage}; events={events}"
            for stage in attempt_spec.get("forbidden_skips", []):
                assert stage not in skipped, f"{artifact.id}:{attempt_id}: stage {stage} must not be skipped"
        attempt_dir = attempt_dirs.get(str(attempt_id))
        if attempt_dir is not None:
            for stage in attempt_spec.get("clean_manifests", []):
                payload = assert_stage_completed(attempt_dir, stage)
                assert int(payload.get("row_error_count") or 0) == 0, f"{artifact.id}:{attempt_id}:{stage}.row_error_count"
                assert int(payload.get("cell_error_count") or 0) == 0, f"{artifact.id}:{attempt_id}:{stage}.cell_error_count"
            for table_name, expected_count in (attempt_spec.get("table_counts") or {}).items():
                rows = list((attempt_dir / "tables" / table_name).glob("*.parquet"))
                assert rows, f"{artifact.id}:{attempt_id}: missing table {table_name}"
            _assert_attempt_row_counts(attempt_spec.get("row_counts"), attempt_dir=attempt_dir, artifact=artifact, attempt_id=str(attempt_id))
        calls_spec = attempt_spec.get("fixture_calls") or {}
        calls_by_function = attempt_runtime_calls.get(str(attempt_id), {})
        for function_name, function_spec in calls_spec.items():
            calls = calls_by_function.get(str(function_name), [])
            if "count" in function_spec:
                assert len(calls) == int(function_spec["count"]), (
                    f"{artifact.id}:{attempt_id}:{function_name} call count actual={len(calls)} calls={calls}"
                )
            for key, expected_count in (function_spec.get("by_arg") or {}).items():
                arg_name, _, arg_value = str(key).partition("=")
                actual = sum(1 for call in calls if str((call.get("args") or {}).get(arg_name)) == arg_value)
                assert actual == int(expected_count), (
                    f"{artifact.id}:{attempt_id}:{function_name} calls where {key}; actual={actual} calls={calls}"
                )


def _assert_attempt_row_counts(
    spec: dict[str, Any] | None,
    *,
    attempt_dir: Path,
    artifact: E2EArtifact,
    attempt_id: str,
) -> None:
    if not spec:
        return
    from pyspark.sql import SparkSession

    spark = SparkSession.getActiveSession()
    if spark is None:
        raise AssertionError(f"{artifact.id}:{attempt_id}: no active Spark session for row count assertions")
    for table_name, expected_count in spec.items():
        table_path = attempt_dir / "tables" / str(table_name)
        assert table_path.exists(), f"{artifact.id}:{attempt_id}: missing table {table_name}"
        actual = spark.read.parquet(str(table_path)).count()
        assert actual == int(expected_count), (
            f"{artifact.id}:{attempt_id}:{table_name} row count actual={actual} expected={expected_count}"
        )


def _execute_rendered_sql(sql: str, *, spark, working_dir: Path, env: dict[str, str]) -> Any:
    with _patched_env(env):
        return EngineEntrypoint(sql).execute(
            SparkExecutionBackend(spark, working_dir=str(working_dir)),
            include_cells=True,
        )


def _materialize_inputs(artifact: E2EArtifact, *, spark, tmp_path: Path) -> dict[str, str]:
    return _materialize_inputs_from_mapping(
        artifact,
        artifact.metadata.get("inputs") or {},
        spark=spark,
        tmp_path=tmp_path,
    )


def _materialize_inputs_from_mapping(
    artifact: E2EArtifact,
    inputs: dict[str, Any],
    *,
    spark,
    tmp_path: Path,
    stable_paths: bool = False,
) -> dict[str, str]:
    rendered_root = tmp_path / "inputs"
    rendered_root.mkdir(parents=True, exist_ok=True)
    context: dict[str, str] = {}
    for name, relpath in inputs.items():
        source = artifact.relpath(str(relpath))
        if source.suffix in {".yaml", ".yml"}:
            spec = _load_yaml(source)
            if spec.get("kind") == "wrapped_parquet":
                output_path = rendered_root / (_stable_input_dir(name, source) if stable_paths else name)
                _write_wrapped_yaml_table(spark, output_path, spec)
                context[name] = output_path.as_posix()
                continue
            if spec.get("kind") == "raw_parquet":
                output_path = rendered_root / (_stable_input_dir(name, source) if stable_paths else name)
                _write_raw_yaml_table(spark, output_path, spec)
                context[name] = output_path.as_posix()
                continue
            if spec.get("kind") == "annotation_results":
                annotation_id = str(spec["annotation_id"])
                task_root = tmp_path / "annotation_tasks" / annotation_id
                task_root.mkdir(parents=True, exist_ok=True)
                (task_root / "results.json").write_text(json.dumps(spec["rows"]), encoding="utf-8")
                context[name] = annotation_id
                continue
        output_path = rendered_root / (_stable_input_dir(name, source) if stable_paths else Path(relpath).name)
        shutil.copyfile(source, output_path)
        context[name] = output_path.as_posix()
    return context


def _stable_input_dir(name: str, source: Path) -> str:
    relative = source.as_posix().replace("/", "_").replace(".", "_").replace("-", "_")
    return f"{name}_{relative}"


def _write_wrapped_yaml_table(spark, output_path: Path, spec: dict[str, Any]) -> None:
    value_schema = {name: _spark_type(type_name) for name, type_name in spec["value_schema"].items()}
    rows = []
    for row in spec["rows"]:
        materialized = {}
        for column, cell in row.items():
            if "error" in cell:
                error = cell["error"]
                materialized[column] = error_cell(
                    error["code"],
                    error.get("message", error["code"]),
                    error.get("source", "fixture"),
                )
            else:
                materialized[column] = clean_cell(cell.get("value"))
        rows.append(materialized)
    write_wrapped_table(spark, output_path, rows, value_schema)


def _write_raw_yaml_table(spark, output_path: Path, spec: dict[str, Any]) -> None:
    spark.createDataFrame(spec["rows"]).write.mode("overwrite").parquet(str(output_path))


def _prepare_fake_codex_harness_fixture(
    spec: dict[str, Any],
    *,
    spark,
    tmp_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    fixture_root = tmp_path / "fixtures" / "fake_codex_harness"
    fixture_root.mkdir(parents=True, exist_ok=True)
    workspace = fixture_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    codex_home = fixture_root / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_file = codex_home / "auth.json"
    auth_file.write_text(json.dumps({"kind": "e2e-fake-codex-auth"}), encoding="utf-8")

    binary = fixture_root / "fake-codex"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "\n"
        "args = sys.argv[1:]\n"
        "last_message = args[args.index('--output-last-message') + 1]\n"
        "task = sys.stdin.read().strip()\n"
        "with open(last_message, 'w', encoding='utf-8') as handle:\n"
        "    handle.write('resolved:' + task)\n"
        "print(json.dumps({\n"
        "    'event': 'completed',\n"
        "    'task': task,\n"
        "    'cwd': os.getcwd(),\n"
        "    'codex_home': os.environ.get('CODEX_HOME'),\n"
        "    'codex_auth_file': os.environ.get('CODEX_AUTH_FILE'),\n"
        "}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    aisystem_id = str(spec.get("aisystem_id") or "aisystem.codex")
    secret_id = str(spec.get("secret_id") or "secret.codex")
    context_path = fixture_root / "agentcicd_fixture_context.json"
    context_path.write_text(
        json.dumps(
            {
                "aisystems": [
                    {
                        "id": aisystem_id,
                        "name": str(spec.get("aisystem_name") or "Codex fake harness"),
                        "target": str(spec.get("target") or "codex:gpt-5-codex"),
                        "interface": {"interface_type": str(spec.get("interface") or "llm.chat")},
                        "config": {
                            "binary": str(binary),
                            "approval_mode": str(spec.get("approval_mode") or "never"),
                        },
                    }
                ],
                "secret_ids": [secret_id],
                "secrets": [
                    {
                        "id": secret_id,
                        "key": str(spec.get("secret_key") or "codex"),
                        "secret": {
                            "type": "codex",
                            "codex_home": str(codex_home),
                            "env": {"CODEX_AUTH_FILE": str(auth_file)},
                        },
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    spark.sparkContext.addFile(context_path.as_posix())
    context = {
        str(spec.get("workspace_context") or "workspace_root"): workspace.as_posix(),
        str(spec.get("aisystem_context") or "aisystem_id"): aisystem_id,
    }
    return context, {"AGENTCICD_FIXTURE_CONTEXT_PATH": context_path.as_posix()}


def _fake_agent_harness_runtime_spec(base_url: str) -> dict[str, Any]:
    return {
        "name": "envs.agent_harness.run_task",
        "type": "remote",
        "call_name": "envs.agent_harness.run_task",
        "runtime_alias": "envs_agent_harness_run_task",
        "base_url": base_url,
        "invoke_path": "/invoke/run_task",
        "return_type_sql": HARNESS_RUN_RESULT_TYPE_SQL,
        "output_type": "variant",
        "execution_runtime": "function_runner",
        "signature": {
            "parameters": [
                {"name": "env", "type_sql": "VARIANT", "has_default": False},
                {"name": "task", "type_sql": "STRING", "has_default": False},
                {"name": "timeout_seconds", "type_sql": "DOUBLE", "has_default": True},
                {"name": "output_size_limit_bytes", "type_sql": "INTEGER", "has_default": True},
                {"name": "artifact_size_limit_bytes", "type_sql": "INTEGER", "has_default": True},
                {"name": "pool", "type_sql": "POOL", "has_default": True},
                {"name": "limiter", "type_sql": "RATELIMIT", "has_default": True},
            ]
        },
        "output_schema": {"type": "variant"},
    }


def _spark_type(type_name: str):
    from pyspark.sql.types import BooleanType, DoubleType, IntegerType, MapType, StringType, StructType

    normalized = str(type_name).lower()
    if normalized == "string":
        return StringType()
    if normalized == "double":
        return DoubleType()
    if normalized == "int":
        return IntegerType()
    if normalized == "boolean":
        return BooleanType()
    if normalized == "map_string_string":
        return MapType(StringType(), StringType(), True)
    if normalized == "struct":
        return StructType([])
    raise AssertionError(f"Unsupported wrapped fixture value type {type_name!r}")


def _assert_tables(spec: dict[str, Any] | None, *, spark, tmp_path: Path, artifact: E2EArtifact) -> None:
    if not spec:
        return
    for table_name, table_spec in spec.get("tables", {}).items():
        rows = read_table_cells(spark, tmp_path, table_name)
        for row_spec in table_spec.get("rows", []):
            row = _find_matching_row(rows, row_spec["match"], artifact=artifact, table_name=table_name)
            for column, cell_spec in row_spec.get("cells", {}).items():
                if column not in row:
                    raise AssertionError(f"{artifact.id}:{table_name}: missing column {column}")
                _assert_cell(row[column], cell_spec, artifact=artifact, table_name=table_name, column=column)


def _find_matching_row(rows: list[dict[str, Any]], match: dict[str, Any], *, artifact: E2EArtifact, table_name: str) -> dict[str, Any]:
    for row in rows:
        if all(_path_get(row, key) == value for key, value in match.items()):
            return row
    raise AssertionError(f"{artifact.id}:{table_name}: no row matched {match}; rows={rows}")


def _assert_cell(cell: dict[str, Any], spec: dict[str, Any], *, artifact: E2EArtifact, table_name: str, column: str) -> None:
    if "value" in spec:
        assert cell["value"] == spec["value"], f"{artifact.id}:{table_name}.{column}.value"
    if "approx" in spec:
        assert float(cell["value"]) == pytest.approx(float(spec["approx"])), (
            f"{artifact.id}:{table_name}.{column}.value"
        )
    if "errors" in spec:
        errors = cell["metadata"]["errors"]
        if spec["errors"] == []:
            assert errors == [], f"{artifact.id}:{table_name}.{column}.errors"
        else:
            assert [error["code"] for error in errors] == spec["errors"], f"{artifact.id}:{table_name}.{column}.errors"
    if "errors_contains" in spec:
        error_codes = [error["code"] for error in cell["metadata"]["errors"]]
        for expected_code in spec["errors_contains"]:
            assert expected_code in error_codes, (
                f"{artifact.id}:{table_name}.{column}.errors missing {expected_code}; errors={error_codes}"
            )
    if spec.get("value_type") == "json":
        assert cell["value"] is not None and not isinstance(cell["value"], str), (
            f"{artifact.id}:{table_name}.{column} is not JSON/variant-like"
        )


def _assert_schemas(spec: dict[str, Any] | None, *, tmp_path: Path, artifact: E2EArtifact) -> None:
    if not spec:
        return
    for table_name in spec.get("tables", []):
        assert_schema_sidecar(tmp_path, table_name)


def _assert_manifests(spec: dict[str, Any] | None, *, tmp_path: Path, artifact: E2EArtifact) -> None:
    if not spec:
        return
    for table_name in spec.get("stages", []):
        assert_stage_completed(tmp_path, table_name)


def _assert_debug_sidecars(spec: dict[str, Any] | None, *, tmp_path: Path, artifact: E2EArtifact) -> None:
    if not spec:
        return
    for table_name, table_spec in spec.get("tables", {}).items():
        stage_manifest = json.loads((tmp_path / "outputs" / f"stage_{table_name}.json").read_text(encoding="utf-8"))
        row_stream = ((stage_manifest.get("debug_artifacts") or {}).get("row_stream") or {})
        assert row_stream.get("format") == "jsonl", f"{artifact.id}: {table_name} debug row stream format"
        assert row_stream.get("content_type") == "application/x-ndjson", (
            f"{artifact.id}: {table_name} debug row stream content type"
        )
        sidecar_dir = tmp_path / "debug" / "tables" / table_name / "rows"
        assert sidecar_dir.is_dir(), f"{artifact.id}: missing debug row stream dir {sidecar_dir}"
        paths = sorted(sidecar_dir.glob("*.jsonl"))
        assert paths, f"{artifact.id}: missing debug row stream jsonl files"
        rows = []
        for path in paths:
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if "count" in table_spec:
            assert len(rows) == table_spec["count"], f"{artifact.id}: {table_name} debug row stream row count"
        for row_spec in table_spec.get("rows", []):
            assert any(_matches_unwrapped(row, row_spec) for row in rows), (
                f"{artifact.id}: {table_name} debug row stream missing row {row_spec}; rows={rows}"
            )


def _assert_fixture_calls(spec: dict[str, Any] | None, *, runtime_server: E2ERuntimeServer, artifact: E2EArtifact) -> None:
    if not spec:
        return
    for function_name, call_spec in spec.get("calls", {}).items():
        calls = runtime_server.calls_for(function_name)
        if "count" in call_spec:
            assert len(calls) == call_spec["count"], (
                f"{artifact.id}:{function_name} call count actual={len(calls)} calls={calls}"
            )
        for case_id, expected_count in call_spec.get("by_case_id", {}).items():
            actual_count = sum(1 for call in calls if call["case_id"] == case_id)
            assert actual_count == expected_count, f"{artifact.id}:{function_name} calls for {case_id}"


def _assert_publishes(spec: dict[str, Any] | None, *, tmp_path: Path, artifact: E2EArtifact) -> None:
    if not spec:
        return
    for report_spec in spec.get("reports", []):
        component = report_spec["component"]
        report_file = {"metric": "metrics.json", "table": "tables.json", "issue": "issues.json", "chart": "charts.json"}.get(component, f"{component}s.json")
        path = tmp_path / "reports" / report_file
        assert path.exists(), f"{artifact.id}: missing report file {path}"
        rows = json.loads(path.read_text(encoding="utf-8"))
        if "count" in report_spec:
            assert len(rows) == report_spec["count"], f"{artifact.id}: report {component} count"
        for row_spec in report_spec.get("rows", []):
            assert any(_matches_unwrapped(row, row_spec) for row in rows), (
                f"{artifact.id}: report {component} missing row {row_spec}; rows={rows}"
            )
    for manifest_spec in spec.get("manifests", []):
        path = tmp_path / "published" / manifest_spec["file"]
        assert path.exists(), f"{artifact.id}: missing publish manifest {path}"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, value in manifest_spec.get("contains", {}).items():
            assert _path_get(payload, key) == value, f"{artifact.id}: publish manifest {path.name}.{key}"


def _matches_unwrapped(row: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, expected_value in expected.items():
        try:
            actual = _path_get(row, key)
        except KeyError:
            return False
        if isinstance(actual, dict) and "value" in actual:
            actual = actual["value"]
        if actual != expected_value:
            return False
    return True


def _load_expected(artifact: E2EArtifact) -> dict[str, Any]:
    expected = {}
    for key, relpath in artifact.metadata["expected"].items():
        expected[key] = _load_yaml(artifact.relpath(relpath))
    return expected


def _load_fixture_specs(artifact: E2EArtifact) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for item in artifact.metadata.get("fixtures", []) or []:
        if not isinstance(item, str):
            raise AssertionError(f"{artifact.id}: fixture entries must be paths")
        path = artifact.relpath(item)
        if not path.exists():
            raise AssertionError(f"{artifact.id}: missing fixture file {path}")
        spec = _load_yaml(path)
        kind = spec.get("kind")
        if kind not in FIXTURE_KINDS:
            raise AssertionError(f"{artifact.id}: unknown fixture kind {kind!r} in {path}")
        specs.append(spec)
    return specs


def _validate_recipe(root: Path, metadata: dict[str, Any]) -> None:
    unknown = set(metadata) - RECIPE_KEYS
    if unknown:
        raise AssertionError(f"{root.name}: unknown recipe keys {sorted(unknown)}")
    required = {"id", "markers", "include_cells", "expected"}
    missing = required - set(metadata)
    if missing:
        raise AssertionError(f"{root.name}: missing recipe keys {sorted(missing)}")
    if not str(metadata["id"]).startswith(root.name[:6]):
        raise AssertionError(f"{root.name}: id must use e2e-NN prefix")
    execution = metadata.get("execution") or {}
    unknown_execution = set(execution) - EXECUTION_KEYS
    if unknown_execution:
        raise AssertionError(f"{root.name}: unknown execution keys {sorted(unknown_execution)}")
    unknown_expected = set(metadata["expected"]) - EXPECTED_KEYS
    if unknown_expected:
        raise AssertionError(f"{root.name}: unknown expected keys {sorted(unknown_expected)}")
    if execution.get("kind", "recipe") != "negative_validation" and not (root / "recipe.sql").exists():
        raise AssertionError(f"{root.name}: missing recipe.sql")


def _validate_expected_files(artifact: E2EArtifact) -> None:
    declared = {artifact.relpath(path).resolve() for path in artifact.metadata["expected"].values()}
    expected_dir = artifact.root / "expected"
    if expected_dir.exists():
        actual = {path.resolve() for path in expected_dir.glob("*.yaml")}
        unused = actual - declared
        if unused:
            raise AssertionError(f"{artifact.id}: unused expected files {[path.name for path in sorted(unused)]}")
    for path in declared:
        if not path.exists():
            raise AssertionError(f"{artifact.id}: missing expected file {path}")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise AssertionError(f"{path}: expected mapping")
    return payload


def _path_get(row: dict[str, Any], path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        value = value[part]
    return value


def _string_env(env: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in env.items()}


@contextmanager
def _patched_env(env: dict[str, str]) -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
