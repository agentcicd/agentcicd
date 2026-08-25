from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Literal, Mapping, Optional

from .expressions import AssignmentExpr, ExprIR, ReturnExpr
from agentcicd.sql.contracts import (
    RegisteredRuntimeFunction,
    RuntimeFunctionParameter,
    coerce_registered_runtime_functions,
)


@dataclass
class FunctionParameterIR:
    name: str
    type_sql: str
    has_default: bool = False
    default_value: Optional[ExprIR] = None


@dataclass
class SqlFunctionBodyIR:
    assignments: List[AssignmentExpr] = field(default_factory=list)
    return_expr: Optional[ReturnExpr] = None


@dataclass
class FunctionDefinitionIR:
    canonical_name: str
    kind: Literal["sql", "python", "remote", "spark_builtin"]
    surface_names: List[str]
    runtime_alias: str
    parameters: List[FunctionParameterIR]
    return_type_sql: Optional[str] = None
    sql_body: Optional[SqlFunctionBodyIR] = None
    source_text: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


RegisteredFunctionKind = Literal["sql", "python", "pyudf", "py", "pydeps", "aisystems", "remote"]


@dataclass(frozen=True)
class RegisteredFunctionParameterSpec:
    name: str
    type_sql: str = "ANY"
    has_default: bool = False
    default_value: object | None = None

    @classmethod
    def from_contract(cls, parameter: RuntimeFunctionParameter) -> "RegisteredFunctionParameterSpec":
        return cls(
            name=parameter.name,
            type_sql=parameter.type_sql,
            has_default=parameter.has_default,
            default_value=parameter.default_value,
        )


@dataclass(frozen=True)
class RegisteredFunctionSpec:
    name: str
    kind: RegisteredFunctionKind
    call_name: str | None = None
    runtime_alias: str | None = None
    signature: tuple[RegisteredFunctionParameterSpec, ...] = ()
    source_text: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_contract(cls, contract: RegisteredRuntimeFunction) -> "RegisteredFunctionSpec":
        metadata = {
            "id": contract.id,
            "input_schema": dict(contract.input_schema),
            "output_schema": dict(contract.output_schema),
            "operations": [operation.to_dict() for operation in contract.operations],
            **({"operation": contract.operation.to_dict()} if contract.operation is not None else {}),
            **({"requirements_text": contract.requirements_text} if contract.requirements_text else {}),
            **({"base_url": contract.base_url} if contract.base_url else {}),
            **({"invoke_path": contract.invoke_path} if contract.invoke_path else {}),
            **({"entrypoint_name": contract.entrypoint_name} if contract.entrypoint_name else {}),
            **({"image_ref": contract.image_ref} if contract.image_ref else {}),
            **({"pool_kind": contract.pool_kind, "pool": {"kind": contract.pool_kind}} if contract.pool_kind else {}),
            **dict(contract.extra),
        }
        return cls(
            name=contract.name,
            kind=contract.kind,
            call_name=contract.call_name,
            runtime_alias=contract.runtime_alias,
            signature=tuple(RegisteredFunctionParameterSpec.from_contract(item) for item in contract.signature),
            source_text=contract.source_text,
            metadata=metadata,
        )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "type": self.kind,
        }
        if self.call_name is not None:
            payload["call_name"] = self.call_name
        if self.runtime_alias is not None:
            payload["runtime_alias"] = self.runtime_alias
        if self.signature:
            payload["signature"] = {
                "parameters": [
                    {
                        "name": parameter.name,
                        "type_sql": parameter.type_sql,
                        "has_default": parameter.has_default,
                        **({"default_value": parameter.default_value} if parameter.has_default else {}),
                    }
                    for parameter in self.signature
                ]
            }
        if self.source_text:
            payload["source_text"] = self.source_text
        payload.update(dict(self.metadata))
        return payload

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RegisteredFunctionSpec":
        return cls.from_contract(RegisteredRuntimeFunction.from_mapping(raw))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RegisteredFunctionSpec):
            return self.as_dict() == other.as_dict()
        if isinstance(other, Mapping):
            return self.as_dict() == {str(key): value for key, value in other.items()}
        return False


def coerce_registered_function_specs(
    items: Iterable[RegisteredFunctionSpec | RegisteredRuntimeFunction | Mapping[str, object]],
) -> list[RegisteredFunctionSpec]:
    specs: list[RegisteredFunctionSpec] = []
    for item in items:
        if isinstance(item, RegisteredFunctionSpec):
            specs.append(item)
            continue
        if isinstance(item, RegisteredRuntimeFunction):
            specs.append(RegisteredFunctionSpec.from_contract(item))
            continue
        if isinstance(item, Mapping):
            specs.append(RegisteredFunctionSpec.from_mapping(item))
    return specs


def registered_function_sql_enabled(spec: RegisteredFunctionSpec) -> bool:
    raw = spec.metadata.get("sql_enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


def coerce_registered_runtime_specs(
    items: Iterable[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]],
) -> list[RegisteredRuntimeFunction]:
    runtime_items: list[RegisteredRuntimeFunction | Mapping[str, object]] = []
    for item in items:
        if isinstance(item, RegisteredFunctionSpec):
            runtime_items.append(item.as_dict())
        else:
            runtime_items.append(item)
    return coerce_registered_runtime_functions(runtime_items)
