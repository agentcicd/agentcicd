from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

from agentcicd.sql.fixture_manifest import builtin_registered_function_specs
from agentcicd.sql.ir.expressions import LiteralExpr
from agentcicd.sql.ir.functions import (
    FunctionDefinitionIR,
    FunctionParameterIR,
    RegisteredFunctionSpec,
    coerce_registered_function_specs,
    registered_function_sql_enabled,
)
from agentcicd.sql.ir.statements import SqlFunctionDefStmt, StatementIR


@dataclass
class FunctionRegistry:
    by_canonical_name: Dict[str, FunctionDefinitionIR] = field(default_factory=dict)
    by_surface_name: Dict[str, FunctionDefinitionIR] = field(default_factory=dict)

    def register(self, definition: FunctionDefinitionIR) -> None:
        canonical = definition.canonical_name.lower()
        self.by_canonical_name[canonical] = definition
        for name in {definition.canonical_name, *definition.surface_names, definition.runtime_alias}:
            self.by_surface_name[name.lower()] = definition

    def resolve(self, name: str) -> Optional[FunctionDefinitionIR]:
        return self.by_surface_name.get(name.lower())


def build_function_registry(
    statements: Iterable[StatementIR],
    registered_functions: Iterable[RegisteredFunctionSpec],
) -> FunctionRegistry:
    registry = FunctionRegistry()
    for item in _builtin_manifest_function_specs():
        name = item.name
        parameters = _signature_parameters(item)
        registry.register(
            FunctionDefinitionIR(
                canonical_name=name,
                kind="python",
                surface_names=[str(item.call_name or name).strip(), name],
                runtime_alias=str(item.runtime_alias or name.replace(".", "_")).strip(),
                parameters=parameters,
                return_type_sql=_registered_return_type(item),
                sql_body=None,
                source_text="",
                metadata=dict(item.metadata),
            )
        )
    for statement in statements:
        if isinstance(statement, SqlFunctionDefStmt) and statement.definition is not None:
            registry.register(statement.definition)
    for item in coerce_registered_function_specs(registered_functions):
        if not registered_function_sql_enabled(item):
            continue
        name = item.name
        if not name:
            continue
        raw_type = item.kind
        if raw_type == "sql":
            kind = "sql"
        elif raw_type in {"py", "python", "pyudf"}:
            kind = "python"
        else:
            kind = "remote"
        surface_names = [str(item.call_name or name).strip(), name]
        runtime_alias = str(item.runtime_alias or name.replace(".", "_")).strip()
        parameters = _signature_parameters(item)
        if kind == "python" and not parameters:
            parameters = _python_udf_parameters(name)
        source_text = item.source_text
        registry.register(
            FunctionDefinitionIR(
                canonical_name=name,
                kind=kind,
                surface_names=surface_names,
                runtime_alias=runtime_alias,
                parameters=parameters,
                return_type_sql=_registered_return_type(item),
                sql_body=None,
                source_text=source_text,
                metadata=dict(item.metadata),
            )
        )
    return registry


def _builtin_manifest_function_specs() -> list[RegisteredFunctionSpec]:
    specs: list[RegisteredFunctionSpec] = []
    seen: set[str] = set()
    for spec in builtin_registered_function_specs():
        if not registered_function_sql_enabled(spec):
            continue
        key = spec.name.lower()
        if key in seen:
            continue
        seen.add(key)
        specs.append(spec)
    return specs


def _signature_parameters(item: RegisteredFunctionSpec) -> list[FunctionParameterIR]:
    return [
        FunctionParameterIR(
            name=parameter.name,
            type_sql=parameter.type_sql,
            has_default=parameter.has_default,
            default_value=(
                LiteralExpr(value=parameter.default_value)
                if parameter.has_default
                else None
            ),
        )
        for parameter in item.signature
        if parameter.name
    ]


def _python_udf_parameters(name: str) -> list[FunctionParameterIR]:
    from agentcicd.sql.udf_registry import get_registered_udf, load_builtin_udfs
    from agentcicd.sql.runtime.udf_compat.udf import _MISSING

    load_builtin_udfs()
    udf_cls = get_registered_udf(name)
    if udf_cls is None:
        return []
    udf_instance = udf_cls()
    return [
        FunctionParameterIR(
            name=parameter.name,
            type_sql=parameter.type_sql,
            has_default=not parameter.required,
            default_value=(
                LiteralExpr(value=None if parameter.default_value is None or parameter.default_value is _MISSING else parameter.default_value)
                if not parameter.required
                else None
            ),
        )
        for parameter in udf_instance.signature()
    ]


def _registered_return_type(item: RegisteredFunctionSpec) -> str | None:
    raw_return_type = item.metadata.get("return_type_sql") or item.metadata.get("return_type")
    if raw_return_type is None:
        return None
    return str(raw_return_type)
