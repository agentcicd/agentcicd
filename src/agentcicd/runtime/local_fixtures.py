from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Iterator, Mapping

from agentcicd.config import FixtureGroup, PoolKind, WorkerSubstrate
from agentcicd.project import LocalRunSpec
from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.fixture_manifest import builtin_registered_function_specs
from agentcicd.sql.ir.statements import DeclareInputStmt
from agentcicd.sql.pool_inputs import parse_pool_default, pool_kind_from_statement, validate_pool_payload


@dataclass(frozen=True)
class FixtureRuntimePlan:
    registered_functions: tuple[RegisteredRuntimeFunction, ...]
    pool_nodes: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class LocalFixtureRuntime:
    registered_functions: tuple[RegisteredRuntimeFunction, ...]
    rate_limiter_base_url: str | None = None


def build_fixture_runtime_plan(spec: LocalRunSpec) -> FixtureRuntimePlan:
    if not spec.fixture_sources:
        return FixtureRuntimePlan(registered_functions=())
    manifest = _generate_manifest(spec.fixture_sources)
    runtime_group = _runtime_fixture_group(spec)
    function_payloads = _registered_function_payloads(
        manifest,
        spec.fixture_sources,
        pool_kind=runtime_group.pool_kind.value,
    )
    return FixtureRuntimePlan(
        registered_functions=tuple(RegisteredRuntimeFunction.from_mapping(payload) for payload in function_payloads)
    )


@contextmanager
def local_fixture_runtime(spec: LocalRunSpec) -> Iterator[LocalFixtureRuntime]:
    static_plan = build_fixture_runtime_plan(spec)
    builtin_functions = _builtin_service_function_payloads(spec.recipe_sql)
    if not static_plan.registered_functions and not builtin_functions:
        yield LocalFixtureRuntime(registered_functions=())
        return

    runtime_group = _runtime_fixture_group(spec)
    manager_port = _free_port()
    control_port = _free_port()
    manager_address = f"http://127.0.0.1:{manager_port}"
    rate_limiter_base_url = f"http://127.0.0.1:{control_port}"
    runtime_functions = tuple(
        RegisteredRuntimeFunction.from_mapping(
            {
                **function.to_dict(),
                "base_url": manager_address,
                "invoke_path": f"/invoke/{function.entrypoint_name or function.name.split('.')[-1]}",
                "pool_kind": runtime_group.pool_kind.value,
                "pool": {"kind": runtime_group.pool_kind.value},
            }
        )
        for function in static_plan.registered_functions
    ) + tuple(
        RegisteredRuntimeFunction.from_mapping(
            {
                **payload,
                "base_url": manager_address,
                "invoke_path": f"/invoke/{payload['entrypoint_name']}",
                "pool_kind": runtime_group.pool_kind.value,
                "pool": {"kind": runtime_group.pool_kind.value},
            }
        )
        for payload in builtin_functions
    )
    pool_nodes = [
        _pool_node(
            runtime_group,
            manager_address=manager_address,
            registered_functions=runtime_functions,
        )
    ]

    previous_env = _capture_env(_LOCAL_RUNTIME_ENV_KEYS)
    control_server = None
    manager_server = None
    try:
        os.environ["AGENTCICD_RATE_LIMITER_BASE_URL"] = rate_limiter_base_url
        os.environ["AGENTCICD_FUNCTION_SOURCE_PATHS"] = json.dumps([path.as_posix() for path in spec.fixture_sources])
        os.environ["AGENTCICD_FUNCTION_BUILTINS_JSON"] = json.dumps(_builtin_entrypoints(runtime_functions), sort_keys=True)
        os.environ["AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS"] = str(runtime_group.timeout_seconds)
        control_server = _start_runtime_control_server(control_port, pool_nodes)
        manager_server = _start_sandbox_manager(spec, runtime_group, manager_address, manager_port, rate_limiter_base_url, runtime_functions)
        yield LocalFixtureRuntime(
            registered_functions=runtime_functions,
            rate_limiter_base_url=rate_limiter_base_url,
        )
    finally:
        if manager_server is not None:
            manager_server.shutdown()
            manager_server.server_close()
        if control_server is not None:
            control_server.shutdown()
            control_server.server_close()
        _restore_env(previous_env)


def _generate_manifest(source_paths: tuple[Path, ...]) -> Mapping[str, object]:
    from agentcicd.fixtures import generate_manifest_for_sources

    return generate_manifest_for_sources(list(source_paths), namespace="local")


def _registered_function_payloads(
    manifest: Mapping[str, object],
    source_paths: tuple[Path, ...],
    *,
    pool_kind: str,
) -> tuple[dict[str, object], ...]:
    from agentcicd.fixtures import registered_function_specs

    source_payload = json.dumps([path.as_posix() for path in source_paths])
    payloads: list[dict[str, object]] = []
    for payload in registered_function_specs(manifest):
        entrypoint_name = str(payload.get("entrypoint_name") or payload["name"]).split(".")[-1]
        fixture_id = _local_fixture_id(payload)
        payloads.append(
            {
                **payload,
                "id": fixture_id,
                "entrypoint_name": entrypoint_name,
                "runtime_group_source_paths": source_payload,
                "signature": _signature_with_service_pool(payload.get("signature")),
                "pool_kind": pool_kind,
                "pool": {"kind": pool_kind},
            }
        )
    return tuple(payloads)


def _signature_with_service_pool(signature: object) -> dict[str, object]:
    raw_parameters = signature.get("parameters") if isinstance(signature, Mapping) else ()
    parameters = [dict(item) for item in raw_parameters if isinstance(item, Mapping)]
    if not any(str(item.get("name") or "").strip().lower() == "pool" for item in parameters):
        parameters.append({"name": "pool", "type_sql": "POOL", "has_default": True})
    return {"parameters": parameters}


def _local_fixture_id(payload: Mapping[str, object]) -> str:
    raw_id = str(payload.get("id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if name.startswith("local."):
        return name
    if raw_id.startswith("local.local."):
        return raw_id.removeprefix("local.")
    if raw_id:
        return raw_id
    return f"local.{name}"


def _builtin_service_function_payloads(recipe_sql: str) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    seen_entrypoints: set[str] = set()
    for spec in builtin_registered_function_specs():
        metadata = dict(spec.metadata)
        if str(metadata.get("execution_runtime") or "").strip().lower() != "function_runner":
            continue
        if str(metadata.get("pool_kind") or "").strip().lower() != PoolKind.SERVICE.value:
            continue
        if not _recipe_references_function(recipe_sql, spec.name, spec.call_name, spec.runtime_alias):
            continue
        entrypoint_name = str(metadata.get("entrypoint_name") or spec.name.rsplit(".", 1)[-1]).strip()
        if not entrypoint_name or entrypoint_name in seen_entrypoints:
            continue
        seen_entrypoints.add(entrypoint_name)
        payloads.append(
            {
                **spec.as_dict(),
                "id": f"builtin.{spec.name}",
                "entrypoint_name": entrypoint_name,
            }
        )
    return tuple(payloads)


def _recipe_references_function(recipe_sql: str, *names: object) -> bool:
    for name in names:
        text = str(name or "").strip()
        if not text:
            continue
        pattern = rf"(?<![\w.]){re.escape(text)}\s*\("
        if re.search(pattern, recipe_sql, flags=re.IGNORECASE):
            return True
    return False


def _builtin_entrypoints(functions: tuple[RegisteredRuntimeFunction, ...]) -> dict[str, str]:
    entrypoints: dict[str, str] = {}
    for function in functions:
        if not function.id.startswith("builtin."):
            continue
        entrypoint_name = str(function.entrypoint_name or function.name.rsplit(".", 1)[-1]).strip()
        call_name = str(function.call_name or function.name).strip()
        if entrypoint_name and call_name:
            entrypoints[entrypoint_name] = call_name
    return entrypoints


def _runtime_fixture_group(spec: LocalRunSpec) -> FixtureGroup:
    if spec.config.fixture_groups:
        return spec.config.fixture_groups[0]
    recipe_group = _fixture_group_from_recipe_pool(spec)
    if recipe_group is not None:
        return recipe_group
    return FixtureGroup(name="service_pool", pool_kind=PoolKind.SERVICE)


def _fixture_group_from_recipe_pool(spec: LocalRunSpec) -> FixtureGroup | None:
    try:
        statements = EngineEntrypoint(spec.recipe_sql).parse()
    except Exception:
        return None
    for statement in statements:
        if not isinstance(statement, DeclareInputStmt) or statement.input_type.strip().upper() != "POOL":
            continue
        try:
            kind = pool_kind_from_statement(statement)
        except Exception:
            continue
        if kind not in {PoolKind.SERVICE.value, PoolKind.SESSION.value, PoolKind.SANDBOX.value}:
            continue
        payload = _pool_payload_for_statement(spec, statement)
        max_workers = int(payload.get("max_instances") or 1)
        timeout_seconds = int(payload.get("timeout_seconds") or 300)
        pool_kind = PoolKind(kind)
        return replace(
            FixtureGroup(name=statement.name, pool_kind=pool_kind),
            max_workers=max(1, max_workers),
            timeout_seconds=max(1, timeout_seconds),
        )
    return None


def _pool_payload_for_statement(spec: LocalRunSpec, statement: DeclareInputStmt) -> dict[str, object]:
    raw_value = spec.inputs.input_values.get(statement.name)
    if raw_value:
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed = parse_pool_default(raw_value)
    else:
        parsed = parse_pool_default(statement.default_sql)
    if parsed.get("kind") is None:
        parsed["kind"] = pool_kind_from_statement(statement)
    return validate_pool_payload(parsed)


def _pool_node(
    group: FixtureGroup,
    *,
    manager_address: str,
    registered_functions: tuple[RegisteredRuntimeFunction, ...],
) -> dict[str, object]:
    fixture_ids = [function.id for function in registered_functions]
    function_names = [function.entrypoint_name or function.name.split(".")[-1] for function in registered_functions]
    return {
        "pool_name": group.name,
        "pool_kind": group.pool_kind.value,
        "node_id": f"local-{group.name}",
        "address": manager_address,
        "capacity": group.max_workers,
        "metadata": {
            "fixture_ids": fixture_ids,
            "function_names": function_names,
            "generation": 1,
            "runtime_provider": "agentcicd_local",
            "worker_substrate": group.worker_substrate.value,
        },
        "heartbeat_ttl_seconds": 3600.0,
    }


def _start_runtime_control_server(port: int, pool_nodes: list[dict[str, object]]) -> object:
    from agentcicd.sql.runtime.udf_compat.runtime_control import start_driver_runtime_control_server

    return start_driver_runtime_control_server(
        host="127.0.0.1",
        port=port,
        pool_nodes=pool_nodes,
    )


def _start_sandbox_manager(
    spec: LocalRunSpec,
    group: FixtureGroup,
    manager_address: str,
    port: int,
    rate_limiter_base_url: str,
    registered_functions: tuple[RegisteredRuntimeFunction, ...],
) -> object:
    from agentcicd.sandbox.manager import ManagerConfig, SandboxManager, create_server

    manager = SandboxManager(
        ManagerConfig(
            fixture_id=",".join(function.id for function in registered_functions),
            function_name=",".join(function.entrypoint_name or function.name.split(".")[-1] for function in registered_functions),
            pool_name=group.name,
            pool_kind=group.pool_kind.value,
            manager_id=f"local-{group.name}",
            generation=1,
            address=manager_address,
            max_workers=group.max_workers,
            require_lease=False,
            debug=spec.config.debug.fixture_call_tracing_enabled,
            fixture_ids=tuple(function.id for function in registered_functions),
            function_names=tuple(function.entrypoint_name or function.name.split(".")[-1] for function in registered_functions),
            driver_base_url=rate_limiter_base_url,
            call_timeout_seconds=float(group.timeout_seconds),
        ),
        lifecycle=_lifecycle_for(group.worker_substrate),
    )
    server = create_server(manager, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.serve_forever, name=f"agentcicd-local-sandbox-manager-{group.name}", daemon=True)
    thread.start()
    _wait_for_server(manager_address)
    return server


def _lifecycle_for(substrate: WorkerSubstrate) -> object | None:
    if substrate == WorkerSubstrate.SUBPROCESS:
        return None
    from agentcicd.sandbox.manager import worker_lifecycle_from_env

    return worker_lifecycle_from_env()


def _wait_for_server(address: str) -> None:
    import time
    from urllib.request import urlopen

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{address}/health", timeout=0.2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise TimeoutError("Local sandbox manager did not become ready")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_LOCAL_RUNTIME_ENV_KEYS = (
    "AGENTCICD_RATE_LIMITER_BASE_URL",
    "AGENTCICD_FUNCTION_SOURCE_PATHS",
    "AGENTCICD_FUNCTION_BUILTINS_JSON",
    "AGENTCICD_SANDBOX_MANAGER_CALL_TIMEOUT_SECONDS",
)


def _capture_env(keys: tuple[str, ...]) -> dict[str, str | None]:
    return {key: os.environ[key] if key in os.environ else None for key in keys}


def _restore_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
