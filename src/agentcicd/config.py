from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from agentcicd.errors import ProjectLoadError


ConfigEnum = TypeVar("ConfigEnum", bound=Enum)

try:  # pragma: no cover - Python 3.10 fallback
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


class BackendName(str, Enum):
    SPARK = "spark"
    DUCKDB = "duckdb"
    VALIDATE = "validate"


class ManagerMode(str, Enum):
    EXECUTOR_LOCAL = "executor_local"
    DRIVER_LOCAL = "driver_local"
    STATIC_HTTP = "static_http"


class WorkerSubstrate(str, Enum):
    SUBPROCESS = "subprocess"
    DOCKER = "docker"
    GVISOR = "gvisor"


class PoolKind(str, Enum):
    SERVICE = "service"
    SESSION = "session"
    SANDBOX = "sandbox"


@dataclass(frozen=True)
class RunSettings:
    backend: BackendName = BackendName.SPARK
    working_dir: str = ".agentcicd/runs"
    table_format: str = "parquet"
    include_cells: bool = True
    max_parallel_stages: int = 1


@dataclass(frozen=True)
class FixtureGroup:
    name: str
    paths: tuple[str, ...] = ()
    manager_mode: ManagerMode = ManagerMode.EXECUTOR_LOCAL
    worker_substrate: WorkerSubstrate = WorkerSubstrate.SUBPROCESS
    pool_kind: PoolKind = PoolKind.SERVICE
    max_workers: int = 1
    timeout_seconds: int = 300


@dataclass(frozen=True)
class DebugSettings:
    store_intermediate_tables: bool = False
    fixture_call_tracing_enabled: bool = False
    fixture_call_tracing_include_arguments: str = "redacted"
    fixture_call_tracing_include_results: str = "preview"

    def to_engine_debug(self) -> dict[str, object]:
        return {
            "store_intermediate_tables": self.store_intermediate_tables,
            "fixture_call_tracing": {
                "enabled": self.fixture_call_tracing_enabled,
                "include_arguments": self.fixture_call_tracing_include_arguments,
                "include_results": self.fixture_call_tracing_include_results,
            },
        }


@dataclass(frozen=True)
class ProjectConfig:
    run: RunSettings = field(default_factory=RunSettings)
    fixture_groups: tuple[FixtureGroup, ...] = ()
    macros: dict[str, str] = field(default_factory=dict)
    debug: DebugSettings = field(default_factory=DebugSettings)


def load_project_config(project_dir: Path) -> ProjectConfig:
    config_path = project_dir / "agentcicd.toml"
    if not config_path.exists():
        return ProjectConfig()
    payload = _read_toml(config_path)
    return ProjectConfig(
        run=_parse_run_settings(_mapping(payload.get("run"), "run")),
        fixture_groups=_parse_fixture_groups(payload),
        macros=_parse_macros(_mapping(payload.get("macros"), "macros")),
        debug=_parse_debug_settings(_mapping(payload.get("debug"), "debug")),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectLoadError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectLoadError(f"{path} must contain a TOML object")
    return payload


def _parse_run_settings(payload: dict[str, Any]) -> RunSettings:
    return RunSettings(
        backend=_enum_value(BackendName, payload.get("backend", BackendName.SPARK.value), "run.backend"),
        working_dir=_string_value(payload.get("working_dir", ".agentcicd/runs"), "run.working_dir"),
        table_format=_string_value(payload.get("table_format", "parquet"), "run.table_format"),
        include_cells=_bool_value(payload.get("include_cells", True), "run.include_cells"),
        max_parallel_stages=_positive_int(payload.get("max_parallel_stages", 1), "run.max_parallel_stages"),
    )


def _parse_fixture_groups(payload: dict[str, Any]) -> tuple[FixtureGroup, ...]:
    raw_groups = payload.get("fixture_groups")
    if raw_groups is None:
        return _legacy_fixture_group(payload)
    if not isinstance(raw_groups, list):
        raise ProjectLoadError("agentcicd.toml fixture_groups must be an array")
    return tuple(_parse_fixture_group(item, index) for index, item in enumerate(raw_groups))


def _legacy_fixture_group(payload: dict[str, Any]) -> tuple[FixtureGroup, ...]:
    raw_fixtures = payload.get("fixtures")
    if raw_fixtures is None:
        return ()
    fixtures = _mapping(raw_fixtures, "fixtures")
    paths = _string_tuple(fixtures.get("paths", ()), "fixtures.paths")
    if not paths:
        return ()
    return (
        FixtureGroup(
            name="default",
            paths=paths,
            manager_mode=_enum_value(ManagerMode, fixtures.get("manager_mode", ManagerMode.EXECUTOR_LOCAL.value), "fixtures.manager_mode"),
        ),
    )


def _parse_fixture_group(raw_item: object, index: int) -> FixtureGroup:
    item = _mapping(raw_item, f"fixture_groups[{index}]")
    return FixtureGroup(
        name=_string_value(item.get("name", "default"), f"fixture_groups[{index}].name"),
        paths=_string_tuple(item.get("paths", ()), f"fixture_groups[{index}].paths"),
        manager_mode=_enum_value(ManagerMode, item.get("manager_mode", ManagerMode.EXECUTOR_LOCAL.value), f"fixture_groups[{index}].manager_mode"),
        worker_substrate=_enum_value(WorkerSubstrate, item.get("worker_substrate", WorkerSubstrate.SUBPROCESS.value), f"fixture_groups[{index}].worker_substrate"),
        pool_kind=_enum_value(PoolKind, item.get("pool_kind", PoolKind.SERVICE.value), f"fixture_groups[{index}].pool_kind"),
        max_workers=_positive_int(item.get("max_workers", 1), f"fixture_groups[{index}].max_workers"),
        timeout_seconds=_positive_int(item.get("timeout_seconds", 300), f"fixture_groups[{index}].timeout_seconds"),
    )


def _parse_macros(payload: dict[str, Any]) -> dict[str, str]:
    return {str(key): _string_value(value, f"macros.{key}") for key, value in payload.items()}


def _parse_debug_settings(payload: dict[str, Any]) -> DebugSettings:
    trace = _mapping(payload.get("fixture_call_tracing"), "debug.fixture_call_tracing") if "fixture_call_tracing" in payload else {}
    return DebugSettings(
        store_intermediate_tables=_bool_value(payload.get("store_intermediate_tables", False), "debug.store_intermediate_tables"),
        fixture_call_tracing_enabled=_bool_value(trace.get("enabled", False), "debug.fixture_call_tracing.enabled"),
        fixture_call_tracing_include_arguments=_string_value(trace.get("include_arguments", "redacted"), "debug.fixture_call_tracing.include_arguments"),
        fixture_call_tracing_include_results=_string_value(trace.get("include_results", "preview"), "debug.fixture_call_tracing.include_results"),
    )


def _mapping(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectLoadError(f"{label} must be an object")
    return dict(value)


def _string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectLoadError(f"{label} must be a non-empty string")
    return value.strip()


def _bool_value(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectLoadError(f"{label} must be a boolean")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ProjectLoadError(f"{label} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProjectLoadError(f"{label} must be a positive integer") from exc
    if parsed < 1:
        raise ProjectLoadError(f"{label} must be a positive integer")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list):
        raise ProjectLoadError(f"{label} must be a string or array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string_value(item, f"{label}[{index}]"))
    return tuple(result)


def _enum_value(enum_type: type[ConfigEnum], value: object, label: str) -> ConfigEnum:
    raw = _string_value(value, label)
    try:
        return enum_type(raw)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ProjectLoadError(f"{label} must be one of: {allowed}") from exc
