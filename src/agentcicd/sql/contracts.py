from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping

JsonObject = dict[str, object]
RegisteredFunctionKind = Literal["sql", "python", "pyudf", "py", "pydeps", "aisystems", "remote"]


@dataclass(frozen=True)
class RuntimeFunctionParameter:
    name: str
    type_sql: str = "ANY"
    has_default: bool = False
    default_value: object | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RuntimeFunctionParameter":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("Runtime function parameter is missing a name")
        return cls(
            name=name,
            type_sql=str(raw.get("type_sql") or "ANY"),
            has_default=bool(raw.get("has_default", False)),
            default_value=raw.get("default_value"),
        )

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "name": self.name,
            "type_sql": self.type_sql,
            "has_default": self.has_default,
        }
        if self.has_default:
            payload["default_value"] = self.default_value
        return payload


@dataclass(frozen=True)
class RuntimeFunctionOperation:
    id: str
    operation_type: str
    source_text: str = ""
    requirements_text: str | None = None
    input_schema: JsonObject = field(default_factory=dict)
    output_schema: JsonObject = field(default_factory=dict)
    name: str | None = None
    transport: str | None = None
    port: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RuntimeFunctionOperation":
        operation_type = str(raw.get("operation_type") or raw.get("operation") or "").strip()
        if not operation_type:
            raise ValueError("Runtime function operation is missing operation_type")
        input_schema = raw.get("input_schema")
        output_schema = raw.get("output_schema")
        return cls(
            id=str(raw.get("id") or "").strip(),
            operation_type=operation_type,
            source_text=str(raw.get("source_text") or ""),
            requirements_text=str(raw.get("requirements_text") or "").strip() or None,
            input_schema=dict(input_schema) if isinstance(input_schema, Mapping) else {},
            output_schema=dict(output_schema) if isinstance(output_schema, Mapping) else {},
            name=str(raw.get("name") or "").strip() or None,
            transport=str(raw.get("transport") or "").strip() or None,
            port=str(raw.get("port") or "").strip() or None,
        )

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "id": self.id,
            "operation_type": self.operation_type,
            "operation": self.operation_type,
            "source_text": self.source_text,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
        }
        if self.requirements_text:
            payload["requirements_text"] = self.requirements_text
        if self.name:
            payload["name"] = self.name
        if self.transport:
            payload["transport"] = self.transport
        if self.port:
            payload["port"] = self.port
        return payload


@dataclass(frozen=True)
class RegisteredRuntimeFunction:
    id: str
    name: str
    kind: RegisteredFunctionKind
    call_name: str
    runtime_alias: str
    signature: tuple[RuntimeFunctionParameter, ...] = ()
    input_schema: JsonObject = field(default_factory=dict)
    output_schema: JsonObject = field(default_factory=dict)
    source_text: str = ""
    requirements_text: str | None = None
    operation: RuntimeFunctionOperation | None = None
    operations: tuple[RuntimeFunctionOperation, ...] = ()
    base_url: str | None = None
    invoke_path: str | None = None
    entrypoint_name: str | None = None
    image_ref: str | None = None
    pool_kind: str | None = None
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RegisteredRuntimeFunction":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("Registered runtime function is missing a name")
        kind = str(raw.get("type") or raw.get("kind") or "remote").strip().lower()
        signature_payload = raw.get("signature")
        parameters: list[RuntimeFunctionParameter] = []
        if isinstance(signature_payload, Mapping):
            raw_parameters = signature_payload.get("parameters")
            if isinstance(raw_parameters, Iterable) and not isinstance(raw_parameters, (str, bytes, Mapping)):
                for item in raw_parameters:
                    if isinstance(item, Mapping):
                        parameters.append(RuntimeFunctionParameter.from_mapping(item))

        raw_operation = raw.get("operation")
        operation = (
            RuntimeFunctionOperation.from_mapping(raw_operation)
            if isinstance(raw_operation, Mapping)
            and str(raw_operation.get("operation_type") or raw_operation.get("operation") or "").strip()
            else None
        )
        raw_operations = raw.get("operations")
        operations: list[RuntimeFunctionOperation] = []
        if isinstance(raw_operations, Iterable) and not isinstance(raw_operations, (str, bytes, Mapping)):
            for item in raw_operations:
                if isinstance(item, Mapping) and str(item.get("operation_type") or item.get("operation") or "").strip():
                    operations.append(RuntimeFunctionOperation.from_mapping(item))
        if operation is not None and not operations:
            operations.append(operation)

        input_schema = raw.get("input_schema")
        output_schema = raw.get("output_schema")
        known_keys = {
            "id",
            "name",
            "type",
            "kind",
            "call_name",
            "runtime_alias",
            "signature",
            "input_schema",
            "output_schema",
            "source_text",
            "requirements_text",
            "operation",
            "operations",
            "base_url",
            "invoke_path",
            "entrypoint_name",
            "image_ref",
            "pool_kind",
            "extra",
        }
        extra = raw.get("extra")
        extra_payload = dict(extra) if isinstance(extra, Mapping) else {}
        extra_payload.update({str(key): value for key, value in raw.items() if str(key) not in known_keys})
        raw_pool = raw.get("pool")
        pool_payload = dict(raw_pool) if isinstance(raw_pool, Mapping) else {}
        pool_kind = str(raw.get("pool_kind") or pool_payload.get("kind") or "").strip().lower() or None
        return cls(
            id=str(raw.get("id") or "").strip(),
            name=name,
            kind=kind,  # type: ignore[arg-type]
            call_name=str(raw.get("call_name") or name).strip(),
            runtime_alias=str(raw.get("runtime_alias") or name.replace(".", "_")).strip(),
            signature=tuple(parameters),
            input_schema=dict(input_schema) if isinstance(input_schema, Mapping) else {},
            output_schema=dict(output_schema) if isinstance(output_schema, Mapping) else {},
            source_text=str(raw.get("source_text") or ""),
            requirements_text=str(raw.get("requirements_text") or "").strip() or None,
            operation=operation,
            operations=tuple(operations),
            base_url=str(raw.get("base_url") or "").strip() or None,
            invoke_path=str(raw.get("invoke_path") or "").strip() or None,
            entrypoint_name=str(raw.get("entrypoint_name") or "").strip() or None,
            image_ref=str(raw.get("image_ref") or "").strip() or None,
            pool_kind=pool_kind,
            extra=extra_payload,
        )

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "id": self.id,
            "name": self.name,
            "type": self.kind,
            "kind": self.kind,
            "call_name": self.call_name,
            "runtime_alias": self.runtime_alias,
            "signature": {"parameters": [parameter.to_dict() for parameter in self.signature]},
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "source_text": self.source_text,
            "operations": [operation.to_dict() for operation in self.operations],
        }
        if self.requirements_text:
            payload["requirements_text"] = self.requirements_text
        if self.operation is not None:
            payload["operation"] = self.operation.to_dict()
        if self.base_url:
            payload["base_url"] = self.base_url
        if self.invoke_path:
            payload["invoke_path"] = self.invoke_path
        if self.entrypoint_name:
            payload["entrypoint_name"] = self.entrypoint_name
        if self.image_ref:
            payload["image_ref"] = self.image_ref
        if self.pool_kind:
            payload["pool_kind"] = self.pool_kind
            payload["pool"] = {"kind": self.pool_kind}
        payload.update(dict(self.extra))
        return payload


@dataclass(frozen=True)
class ExecutionContext:
    fixtures: tuple[RegisteredRuntimeFunction, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ExecutionContext":
        raw_fixtures = raw.get("fixtures")
        fixtures: list[RegisteredRuntimeFunction] = []
        if isinstance(raw_fixtures, Iterable) and not isinstance(raw_fixtures, (str, bytes, Mapping)):
            for item in raw_fixtures:
                if isinstance(item, RegisteredRuntimeFunction):
                    fixtures.append(item)
                elif isinstance(item, Mapping):
                    fixtures.append(RegisteredRuntimeFunction.from_mapping(item))
        return cls(fixtures=tuple(fixtures))

    def to_dict(self) -> JsonObject:
        return {"fixtures": [fixture.to_dict() for fixture in self.fixtures]}


def coerce_registered_runtime_functions(
    items: Iterable[RegisteredRuntimeFunction | Mapping[str, object]],
) -> list[RegisteredRuntimeFunction]:
    functions: list[RegisteredRuntimeFunction] = []
    for item in items:
        if isinstance(item, RegisteredRuntimeFunction):
            functions.append(item)
        elif isinstance(item, Mapping):
            functions.append(RegisteredRuntimeFunction.from_mapping(item))
    return functions


@dataclass(frozen=True)
class DatasetRegisterRequest:
    organization_id: str
    name: str
    description: str | None = None
    format: str | None = None
    tags: JsonObject = field(default_factory=dict)
    ingestion_mode: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "organization_id": self.organization_id,
            "name": self.name,
            "description": self.description,
            "format": self.format,
            "tags": dict(self.tags),
            "ingestion_mode": self.ingestion_mode,
        }


@dataclass(frozen=True)
class DatasetStatusRequest:
    status: str
    error: str | None = None
    format: str | None = None
    schema: JsonObject = field(default_factory=dict)
    storage_uri: str | None = None
    ingestion_mode: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "status": self.status,
            "error": self.error,
            "format": self.format,
            "schema": dict(self.schema),
            "storage_uri": self.storage_uri,
            "ingestion_mode": self.ingestion_mode,
        }


@dataclass(frozen=True)
class DatasetActivateRequest:
    storage_uri: str
    format: str | None = None
    schema: JsonObject = field(default_factory=dict)
    ingestion_mode: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "storage_uri": self.storage_uri,
            "format": self.format,
            "schema": dict(self.schema),
            "ingestion_mode": self.ingestion_mode,
        }


@dataclass(frozen=True)
class ProgressCallbackEvent:
    step_type: str
    step_name: str
    status: str
    error: str | None = None
    metadata: JsonObject = field(default_factory=dict)
