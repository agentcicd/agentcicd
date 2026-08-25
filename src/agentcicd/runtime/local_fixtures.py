from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from agentcicd.config import FixtureGroup, PoolKind, WorkerSubstrate
from agentcicd.project import LocalRunSpec
from agentcicd.sql.contracts import RegisteredRuntimeFunction


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
    function_payloads = _registered_function_payloads(manifest, spec.fixture_sources)
    return FixtureRuntimePlan(
        registered_functions=tuple(RegisteredRuntimeFunction.from_mapping(payload) for payload in function_payloads)
    )


@contextmanager
def local_fixture_runtime(spec: LocalRunSpec) -> Iterator[LocalFixtureRuntime]:
    static_plan = build_fixture_runtime_plan(spec)
    if not static_plan.registered_functions:
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


def _registered_function_payloads(manifest: Mapping[str, object], source_paths: tuple[Path, ...]) -> tuple[dict[str, object], ...]:
    from agentcicd.fixtures import registered_function_specs

    source_payload = json.dumps([path.as_posix() for path in source_paths])
    payloads: list[dict[str, object]] = []
    for payload in registered_function_specs(manifest):
        entrypoint_name = str(payload.get("entrypoint_name") or payload["name"]).split(".")[-1]
        fixture_id = str(payload.get("id") or f"local.{payload['name']}").strip()
        payloads.append(
            {
                **payload,
                "id": fixture_id,
                "entrypoint_name": entrypoint_name,
                "runtime_group_source_paths": source_payload,
            }
        )
    return tuple(payloads)


def _runtime_fixture_group(spec: LocalRunSpec) -> FixtureGroup:
    if spec.config.fixture_groups:
        return spec.config.fixture_groups[0]
    return FixtureGroup(name="local", pool_kind=PoolKind.SERVICE)


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
