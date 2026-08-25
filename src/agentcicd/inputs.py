from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from agentcicd.errors import InputCoercionError, ProjectLoadError
from agentcicd.sql.integration import declared_inputs_from_sql
from agentcicd.sql.ir.statements import DeclareInputStmt
from agentcicd.sql.pool_inputs import canonical_pool_value_json, validate_pool_payload


_URI_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@dataclass(frozen=True)
class InputSource:
    source: str
    declared_type: str


@dataclass(frozen=True)
class CoercedInputs:
    input_values: dict[str, str]
    input_sources: dict[str, InputSource]


def load_inputs(project_dir: Path, recipe_sql: str) -> CoercedInputs:
    declared_inputs = declared_inputs_from_sql(recipe_sql)
    raw_inputs = _load_raw_inputs(project_dir)
    return coerce_declared_inputs(
        raw_inputs,
        declared_inputs=declared_inputs,
        project_dir=project_dir,
        source_name=_input_source_name(project_dir),
    )


def coerce_declared_inputs(
    raw_inputs: dict[str, Any],
    *,
    declared_inputs: list[DeclareInputStmt],
    project_dir: Path,
    source_name: str,
) -> CoercedInputs:
    declarations = _declared_inputs_by_name(declared_inputs)
    normalized_raw = {str(key).strip().lower(): value for key, value in raw_inputs.items()}
    unknown = sorted(name for name in normalized_raw if name not in declarations)
    if unknown:
        raise InputCoercionError(f"Unknown input key(s): {', '.join(unknown)}")

    missing = sorted(
        declaration.name
        for declaration in declarations.values()
        if declaration.default_sql is None and declaration.name.lower() not in normalized_raw
    )
    if missing:
        raise InputCoercionError(f"Missing required input value(s): {', '.join(missing)}")

    values: dict[str, str] = {}
    sources: dict[str, InputSource] = {}
    for normalized_name, raw_value in normalized_raw.items():
        declaration = declarations[normalized_name]
        coerced = _coerce_value(raw_value, declaration=declaration, project_dir=project_dir)
        values[declaration.name] = coerced
        sources[declaration.name] = InputSource(source=source_name, declared_type=declaration.input_type.upper())
    return CoercedInputs(input_values=values, input_sources=sources)


def _declared_inputs_by_name(declared_inputs: list[DeclareInputStmt]) -> dict[str, DeclareInputStmt]:
    declarations: dict[str, DeclareInputStmt] = {}
    for declaration in declared_inputs:
        normalized = declaration.name.strip().lower()
        if normalized in declarations:
            raise InputCoercionError(f"Duplicate declared input name: {declaration.name}")
        declarations[normalized] = declaration
    return declarations


def _load_raw_inputs(project_dir: Path) -> dict[str, Any]:
    yaml_path = project_dir / "inputs.yaml"
    properties_path = project_dir / "input.properties"
    if yaml_path.exists() and properties_path.exists():
        raise ProjectLoadError("Use either inputs.yaml or input.properties, not both")
    if yaml_path.exists():
        return _read_yaml_mapping(yaml_path)
    if properties_path.exists():
        return _read_properties(properties_path)
    return {}


def _input_source_name(project_dir: Path) -> str:
    if (project_dir / "inputs.yaml").exists():
        return "inputs.yaml"
    if (project_dir / "input.properties").exists():
        return "input.properties"
    return "defaults"


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProjectLoadError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectLoadError(f"{path.name} must contain a mapping")
    return {str(key): value for key, value in payload.items()}


def _read_properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProjectLoadError(f"{path}:{line_number} must use KEY=VALUE")
        key, value = line.split("=", 1)
        if not key.strip():
            raise ProjectLoadError(f"{path}:{line_number} has an empty key")
        result[key.strip()] = value.strip()
    return result


def _coerce_value(raw_value: Any, *, declaration: DeclareInputStmt, project_dir: Path) -> str:
    input_type = declaration.input_type.strip().upper()
    if input_type == "STRING":
        return _coerce_string_scalar(raw_value, declaration.name)
    if input_type == "DATASET":
        return _coerce_dataset(raw_value, declaration.name, project_dir)
    if input_type == "AISYSTEM":
        return _coerce_resource_id(raw_value, declaration.name, "aisystem.")
    if input_type == "SECRET":
        return _coerce_resource_id(raw_value, declaration.name, "secret.")
    if input_type == "DATE":
        return _coerce_date(raw_value, declaration.name)
    if input_type == "TIMESTAMP":
        return _coerce_timestamp(raw_value, declaration.name)
    if input_type in {"INT", "INTEGER"}:
        return str(_coerce_int(raw_value, declaration.name))
    if input_type in {"FLOAT", "DOUBLE"}:
        return _coerce_float(raw_value, declaration.name)
    if input_type in {"BOOLEAN", "BOOL"}:
        return _coerce_bool(raw_value, declaration.name)
    if input_type == "RATELIMIT":
        value = _coerce_int(raw_value, declaration.name)
        if value < 1:
            raise InputCoercionError(f"Input '{declaration.name}' RATELIMIT must be a positive integer")
        return str(value)
    if input_type == "POOL":
        return _coerce_pool(raw_value, declaration.name)
    if input_type == "VARIANT":
        return json.dumps(raw_value, sort_keys=True, separators=(",", ":"), default=_json_default)
    if isinstance(raw_value, (list, dict)):
        raise InputCoercionError(f"Input '{declaration.name}' type {input_type} does not accept YAML list/object values")
    return str(raw_value)


def _coerce_string_scalar(raw_value: Any, name: str) -> str:
    if isinstance(raw_value, (list, dict)):
        raise InputCoercionError(f"Input '{name}' STRING does not accept YAML list/object values")
    if raw_value is None:
        raise InputCoercionError(f"Input '{name}' STRING must not be null")
    return str(raw_value)


def _coerce_dataset(raw_value: Any, name: str, project_dir: Path) -> str:
    value = _coerce_string_scalar(raw_value, name).strip()
    if not value:
        raise InputCoercionError(f"Input '{name}' DATASET must be non-empty")
    if _URI_PATTERN.match(value) or value.startswith("agentcicd://") or Path(value).is_absolute():
        return value
    return (project_dir / value).resolve().as_posix()


def _coerce_resource_id(raw_value: Any, name: str, prefix: str) -> str:
    value = _coerce_string_scalar(raw_value, name).strip()
    if not value.startswith(prefix):
        raise InputCoercionError(f"Input '{name}' must be an id starting with '{prefix}'")
    return value


def _coerce_date(raw_value: Any, name: str) -> str:
    if isinstance(raw_value, datetime):
        return raw_value.date().isoformat()
    if isinstance(raw_value, date):
        return raw_value.isoformat()
    value = _coerce_string_scalar(raw_value, name).strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise InputCoercionError(f"Input '{name}' DATE must be YYYY-MM-DD") from exc


def _coerce_timestamp(raw_value: Any, name: str) -> str:
    if isinstance(raw_value, datetime):
        return raw_value.isoformat(sep=" ")
    if isinstance(raw_value, date):
        return datetime.combine(raw_value, datetime.min.time()).isoformat(sep=" ")
    value = _coerce_string_scalar(raw_value, name).strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(sep=" ")
    except ValueError as exc:
        raise InputCoercionError(f"Input '{name}' TIMESTAMP must be an ISO timestamp") from exc


def _coerce_int(raw_value: Any, name: str) -> int:
    if isinstance(raw_value, bool):
        raise InputCoercionError(f"Input '{name}' must be an integer")
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, str):
        try:
            return int(raw_value.strip())
        except ValueError as exc:
            raise InputCoercionError(f"Input '{name}' must be an integer") from exc
    raise InputCoercionError(f"Input '{name}' must be an integer")


def _coerce_float(raw_value: Any, name: str) -> str:
    if isinstance(raw_value, bool):
        raise InputCoercionError(f"Input '{name}' must be a number")
    if isinstance(raw_value, (int, float)):
        return str(raw_value)
    if isinstance(raw_value, str):
        try:
            float(raw_value.strip())
        except ValueError as exc:
            raise InputCoercionError(f"Input '{name}' must be a number") from exc
        return raw_value.strip()
    raise InputCoercionError(f"Input '{name}' must be a number")


def _coerce_bool(raw_value: Any, name: str) -> str:
    if isinstance(raw_value, bool):
        return "true" if raw_value else "false"
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "false"}:
            return normalized
    raise InputCoercionError(f"Input '{name}' must be a boolean")


def _coerce_pool(raw_value: Any, name: str) -> str:
    if isinstance(raw_value, dict):
        return json.dumps(validate_pool_payload(raw_value), sort_keys=True, separators=(",", ":"))
    if isinstance(raw_value, str):
        return canonical_pool_value_json(raw_value)
    raise InputCoercionError(f"Input '{name}' POOL must be an object or JSON string")


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
