from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.plan import DefinitionStepPayload, SqlStepPayload, StreamTableStepPayload
from agentcicd.sql.engine.validator import ValidationResult, validate_lowered_sql
from agentcicd.sql.fixture_manifest import (
    ParsedFixtureManifest,
    builtin_registered_function_specs,
    parse_fixture_manifest,
    validate_fixture_manifest,
)
from agentcicd.sql.integration import _reject_trailing_projection_commas, _validate_annotation_templates
from agentcicd.sql.ir.functions import (
    RegisteredFunctionSpec,
    coerce_registered_function_specs,
)
from agentcicd.sql.ir.statements import BatchTableStmt, QueryStmt, StreamTableStmt
from agentcicd.sql.wrapped_validation import validate_wrapped_statements

if TYPE_CHECKING:
    from agentcicd.sql.engine.runner import EngineRunConfig
    from agentcicd.sql.engine.runtime import ExecutionReport

FixtureManifestInput = Mapping[str, Any] | str | Path
RegisteredFunctionInput = RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]


class AgentCICDEvalSqlApiError(ValueError):
    """Raised when public agentcicd.sql API inputs are invalid."""


@dataclass(frozen=True)
class ManifestValidationResult:
    manifests: tuple[ParsedFixtureManifest, ...]
    registered_functions: tuple[RegisteredFunctionSpec, ...]


@dataclass(frozen=True)
class RecipeValidationResult:
    registered_function_count: int
    manifest_count: int
    validation_mode: str = "static"


@dataclass(frozen=True)
class RecipeExecutionValidationResult:
    static_validation: RecipeValidationResult
    registered_runtime_function_count: int
    lowered_sql_count: int
    spark_validations: tuple[ValidationResult, ...]


@dataclass(frozen=True)
class _StaticRecipeValidation:
    plan: list[Any]


def validate_manifests(
    manifests: Iterable[FixtureManifestInput],
    *,
    reject_builtin_conflicts: bool = True,
) -> ManifestValidationResult:
    parsed_manifests: list[ParsedFixtureManifest] = []
    registered_functions: list[RegisteredFunctionSpec] = []
    for manifest_input in manifests:
        manifest = _load_manifest_input(manifest_input)
        validate_fixture_manifest(manifest)
        parsed = parse_fixture_manifest(manifest)
        parsed_manifests.append(parsed)
        registered_functions.extend(parsed.registered_function_specs())

    _validate_registered_function_names(
        registered_functions,
        reject_builtin_conflicts=reject_builtin_conflicts,
    )
    return ManifestValidationResult(
        manifests=tuple(parsed_manifests),
        registered_functions=tuple(registered_functions),
    )


def registered_functions_from_manifests(
    manifests: Iterable[FixtureManifestInput],
    *,
    reject_builtin_conflicts: bool = True,
) -> list[RegisteredFunctionSpec]:
    return list(
        validate_manifests(
            manifests,
            reject_builtin_conflicts=reject_builtin_conflicts,
        ).registered_functions
    )


def validate_recipe(
    recipe_sql: str,
    *,
    manifests: Iterable[FixtureManifestInput] | None = None,
    registered_functions: Iterable[RegisteredFunctionInput] | None = None,
    include_cells: bool = True,
    allow_top_level_queries: bool = False,
    require_materialized_stage: bool = True,
) -> RecipeValidationResult:
    manifest_result, specs = _resolve_registered_functions(
        manifests=manifests,
        registered_functions=registered_functions,
    )
    _validate_recipe_sql(
        recipe_sql,
        registered_functions=specs,
        include_cells=include_cells,
        allow_top_level_queries=allow_top_level_queries,
        require_materialized_stage=require_materialized_stage,
    )
    return RecipeValidationResult(
        registered_function_count=len(specs),
        manifest_count=len(manifest_result.manifests),
    )


def validate_recipe_execution(
    recipe_sql: str,
    *,
    spark_session: Any,
    manifests: Iterable[FixtureManifestInput] | None = None,
    registered_functions: Iterable[RegisteredFunctionInput] | None = None,
    include_cells: bool = True,
    allow_top_level_queries: bool = False,
    require_materialized_stage: bool = True,
) -> RecipeExecutionValidationResult:
    if spark_session is None:
        raise AgentCICDEvalSqlApiError("spark_session is required for execution validation")
    manifest_result, specs = _resolve_registered_functions(
        manifests=manifests,
        registered_functions=registered_functions,
    )
    static_validation = _validate_recipe_sql(
        recipe_sql,
        registered_functions=specs,
        include_cells=include_cells,
        allow_top_level_queries=allow_top_level_queries,
        require_materialized_stage=require_materialized_stage,
    )
    registered_runtime_function_count = _register_runtime_functions_for_execution_validation(
        static_validation.plan,
        spark_session=spark_session,
    )
    spark_validations = _validate_plan_sql_with_spark(static_validation.plan, spark_session=spark_session)
    failed = [item for item in spark_validations if not item.ok]
    if failed:
        details = "; ".join(str(item.error or "unknown Spark validation error") for item in failed)
        raise AgentCICDEvalSqlApiError(f"Spark execution validation failed: {details}")
    return RecipeExecutionValidationResult(
        static_validation=RecipeValidationResult(
            registered_function_count=len(specs),
            manifest_count=len(manifest_result.manifests),
            validation_mode="execution",
        ),
        registered_runtime_function_count=registered_runtime_function_count,
        lowered_sql_count=len(spark_validations),
        spark_validations=tuple(spark_validations),
    )


def run_recipe(
    recipe_sql: str,
    config: "EngineRunConfig",
    *,
    manifests: Iterable[FixtureManifestInput] | None = None,
    registered_functions: Iterable[RegisteredFunctionInput] | None = None,
) -> "ExecutionReport":
    _manifest_result, specs = _resolve_registered_functions(
        manifests=manifests,
        registered_functions=registered_functions,
    )
    run_config = replace(
        config,
        registered_functions=tuple([*(config.registered_functions or ()), *specs]),
    )
    return _run_script_with_new_engine(recipe_sql, run_config)


def _resolve_registered_functions(
    *,
    manifests: Iterable[FixtureManifestInput] | None,
    registered_functions: Iterable[RegisteredFunctionInput] | None,
) -> tuple[ManifestValidationResult, list[RegisteredFunctionSpec]]:
    manifest_result = validate_manifests(manifests or [])
    specs = [
        *coerce_registered_function_specs(registered_functions or []),
        *manifest_result.registered_functions,
    ]
    _validate_registered_function_names(specs, reject_builtin_conflicts=True)
    return manifest_result, specs


def _validate_recipe_sql(
    source_text: str,
    *,
    registered_functions: list[RegisteredFunctionSpec],
    include_cells: bool,
    allow_top_level_queries: bool,
    require_materialized_stage: bool,
) -> _StaticRecipeValidation:
    _reject_trailing_projection_commas(source_text)
    entrypoint = EngineEntrypoint(source_text, registered_functions=registered_functions)
    statements, registry = entrypoint.resolve_with_registry(apply_defaults=True)
    _validate_annotation_templates(statements)
    if include_cells:
        validate_wrapped_statements(statements)
    if not allow_top_level_queries and any(isinstance(statement, QueryStmt) for statement in statements):
        raise ValueError(
            "Top-level ad hoc SQL statements are not valid recipe stages. "
            "Use CREATE BATCH TABLE or CREATE STREAM TABLE for query stages."
        )
    if require_materialized_stage:
        materialized_stages = [
            statement
            for statement in statements
            if isinstance(statement, (BatchTableStmt, StreamTableStmt))
        ]
        if not materialized_stages:
            raise ValueError(
                "Recipe must include at least one materialized AgentCICD SQL stage, "
                "for example CREATE BATCH TABLE <name> SELECT ..."
            )
    plan = entrypoint.compile_resolved_plan(
        statements,
        registry=registry,
        include_cells=include_cells,
        render_sql=False,
    )
    return _StaticRecipeValidation(plan=plan)


def _register_runtime_functions_for_execution_validation(plan: list[Any], *, spark_session: Any) -> int:
    runtime_steps = [step for step in plan if step.kind == "register_runtime_function"]
    if not runtime_steps:
        return 0

    from agentcicd.sql.runtime.invokers import CompositeRuntimeFunctionInvoker

    invoker = CompositeRuntimeFunctionInvoker()
    registered = 0
    for step in runtime_steps:
        if not isinstance(step.payload, DefinitionStepPayload):
            raise AgentCICDEvalSqlApiError(f"Invalid runtime function registration payload for {step.name}")
        invoker.register(spark_session, step.payload.definition)
        registered += 1
    return registered


def _validate_plan_sql_with_spark(plan: list[Any], *, spark_session: Any) -> tuple[ValidationResult, ...]:
    validations: list[ValidationResult] = []
    for step in plan:
        if isinstance(step.payload, SqlStepPayload):
            validations.append(validate_lowered_sql(step.payload.sql, spark_session=spark_session))
            continue
        if isinstance(step.payload, StreamTableStepPayload):
            validations.append(validate_lowered_sql(step.payload.sql, spark_session=spark_session))
    return tuple(validations)


def _run_script_with_new_engine(recipe_sql: str, config: "EngineRunConfig") -> "ExecutionReport":
    from agentcicd.sql.engine.runner import run_script_with_new_engine

    return run_script_with_new_engine(recipe_sql, config)


def _load_manifest_input(manifest_input: FixtureManifestInput) -> Mapping[str, Any]:
    if isinstance(manifest_input, Mapping):
        return manifest_input
    path = Path(manifest_input)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentCICDEvalSqlApiError(f"Unable to read fixture manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentCICDEvalSqlApiError(f"Fixture manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, Mapping):
        raise AgentCICDEvalSqlApiError(f"Fixture manifest must be a JSON object: {path}")
    return payload


def _validate_registered_function_names(
    specs: Iterable[RegisteredFunctionSpec],
    *,
    reject_builtin_conflicts: bool,
) -> None:
    spec_list = list(specs)
    seen: dict[str, tuple[int, str]] = {}
    for index, spec in enumerate(spec_list):
        for label, value in _registered_function_keys(spec):
            previous = seen.get(value)
            if previous is not None and previous[0] != index:
                raise AgentCICDEvalSqlApiError(
                    f"Duplicate registered function {label} '{value}' from {previous[1]} and {spec.name}"
                )
            seen[value] = (index, spec.name)

    if not reject_builtin_conflicts:
        return

    builtin_keys: dict[str, str] = {}
    for spec in builtin_registered_function_specs():
        for _label, value in _registered_function_keys(spec):
            builtin_keys[value] = spec.name
    for spec in spec_list:
        for label, value in _registered_function_keys(spec):
            builtin_name = builtin_keys.get(value)
            if builtin_name is not None:
                raise AgentCICDEvalSqlApiError(
                    f"Registered function {label} '{value}' conflicts with built-in fixture '{builtin_name}'"
                )


def _registered_function_keys(spec: RegisteredFunctionSpec) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    name = spec.name.strip().lower()
    if name:
        keys.append(("name", name))
    call_name = str(spec.call_name or "").strip().lower()
    if call_name:
        keys.append(("call_name", call_name))
    runtime_alias = str(spec.runtime_alias or "").strip().lower()
    if runtime_alias:
        keys.append(("runtime_alias", runtime_alias))
    return keys
