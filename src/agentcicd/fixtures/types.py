from __future__ import annotations

from dataclasses import dataclass
from types import UnionType
from typing import Any, ClassVar, Generic, TypeVar, get_args, get_origin, get_type_hints


class FixtureTypeError(TypeError):
    """Raised when an annotation cannot be expressed as a AgentCICD fixture type."""


T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")
TSpec = TypeVar("TSpec")


@dataclass(frozen=True)
class TypeContract:
    type_sql: str
    schema: dict[str, Any]
    json_schema: dict[str, Any]
    manifest_type: dict[str, Any]
    nullable: bool = True


class Str:
    pass


class SecretId:
    pass


class Session:
    @property
    def workspace_dir(self) -> Any:  # pragma: no cover - runtime object supplies Path
        raise RuntimeError("Session is only available during fixture execution")


class Int:
    pass


class Float:
    pass


class Bool:
    pass


class Variant:
    pass


class Array(Generic[T]):
    pass


class Map(Generic[K, V]):
    pass


class Optional(Generic[T]):
    pass


class Required(Generic[T]):
    pass


class EnvSpec(Generic[TSpec]):
    pass


class Environment(Generic[TSpec]):
    pass


class NamedStruct:
    __agentcicd_named_struct__: ClassVar[bool] = True

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def to_dict(self) -> dict[str, Any]:
        return {name: _to_jsonable(value) for name, value in self.__dict__.items()}


class BrowserSpec(NamedStruct):
    pass


class ShellSpec(NamedStruct):
    pass


class AgentHarnessSpec(NamedStruct):
    pass


class McpHttpSpec(NamedStruct):
    pass


class McpStdioSpec(NamedStruct):
    pass


class McpPlaywrightSpec(NamedStruct):
    pass


class DirectoryEntry(NamedStruct):
    path: Required[Str]
    name: Required[Str]
    parent_path: Str
    entry_type: Required[Str]
    size_bytes: Int
    content_type: Str
    sha256: Str
    object_uri: Str
    is_empty_dir: Required[Bool]


Directory = Array[DirectoryEntry]


class MaterializedDirectory(NamedStruct):
    root: Required[Str]
    target_dir: Required[Str]
    target_path: Required[Str]
    entries: Required[Directory]


SCHEMA_VERSION = "agentcicd.fixtures.manifest.v1"
DIRECTORY_ENTRY_TYPE_SQL = (
    "STRUCT<"
    "path: STRING, "
    "name: STRING, "
    "parent_path: STRING, "
    "entry_type: STRING, "
    "size_bytes: BIGINT, "
    "content_type: STRING, "
    "sha256: STRING, "
    "object_uri: STRING, "
    "is_empty_dir: BOOLEAN"
    ">"
)


def type_contract(annotation: Any, *, globalns: dict[str, Any] | None = None) -> TypeContract:
    return _TypeContractBuilder(globalns=globalns or {}).build(annotation)


def secret_parameter_names(annotations: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for name, annotation in annotations.items():
        if name == "return":
            continue
        if _contains_secret_id(annotation):
            names.append(name)
    return tuple(names)


def session_parameter_names(annotations: dict[str, Any]) -> tuple[str, ...]:
    return tuple(name for name, annotation in annotations.items() if name != "return" and annotation is Session)


def _contains_secret_id(annotation: Any) -> bool:
    if annotation is SecretId:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(_contains_secret_id(argument) for argument in get_args(annotation))


class _TypeContractBuilder:
    def __init__(self, *, globalns: dict[str, Any]) -> None:
        self.globalns = globalns
        self.struct_cache: dict[type[NamedStruct], TypeContract] = {}

    def build(self, annotation: Any) -> TypeContract:
        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if annotation in {Str, SecretId}:
            return _scalar("Str", "STRING", {"type": "string"})
        if annotation is Session:
            return TypeContract(
                "VARIANT",
                {"kind": "session"},
                {"type": "object", "additionalProperties": True, "x-agentcicd-type": "session"},
                {"type": "Session"},
            )
        if annotation is Int:
            return _scalar("Int", "BIGINT", {"type": "integer", "format": "int64"})
        if annotation is Float:
            return _scalar("Float", "DOUBLE", {"type": "number"})
        if annotation is Bool:
            return _scalar("Bool", "BOOLEAN", {"type": "boolean"})
        if annotation is Variant:
            return _scalar("Variant", "VARIANT", {"type": "variant"})
        if annotation is DirectoryEntry:
            return self._directory_entry()
        if annotation == Directory:
            return self._directory()
        if origin is Required:
            child = self.build(arguments[0])
            return TypeContract(child.type_sql, child.schema, child.json_schema, child.manifest_type, nullable=False)
        if origin is Optional:
            child = self.build(arguments[0])
            return TypeContract(child.type_sql, child.schema, child.json_schema, child.manifest_type, nullable=True)
        if origin is Array:
            child = self.build(arguments[0])
            return TypeContract(
                f"ARRAY<{child.type_sql}>",
                {"kind": "array", "element": child.schema},
                {"type": "array", "items": child.json_schema},
                {"type": "Array", "element": child.manifest_type},
            )
        if origin is Map:
            key = self.build(arguments[0])
            if key.type_sql not in {"STRING", "BIGINT", "INT", "DOUBLE", "BOOLEAN"}:
                raise FixtureTypeError("Map keys must lower to a primitive comparable SQL type")
            value = self.build(arguments[1])
            return TypeContract(
                f"MAP<{key.type_sql}, {value.type_sql}>",
                {"kind": "map", "key": key.schema, "value": value.schema},
                {"type": "object", "additionalProperties": value.json_schema},
                {"type": "Map", "key": key.manifest_type, "value": value.manifest_type},
            )
        if origin is EnvSpec:
            spec_type = arguments[0]
            return TypeContract(
                "VARIANT",
                {"kind": "env_spec", "spec": _type_name(spec_type)},
                {"type": "object", "additionalProperties": True, "x-agentcicd-type": "env_spec", "spec": _type_name(spec_type)},
                {"type": "EnvSpec", "spec": _type_name(spec_type)},
            )
        if isinstance(annotation, type) and issubclass(annotation, NamedStruct):
            return self._named_struct(annotation)
        if origin is UnionType or str(annotation).startswith("typing.Union"):
            raise FixtureTypeError("Union annotations are not supported; use Optional[T] or Variant")
        raise FixtureTypeError(f"Unsupported fixture annotation: {annotation!r}")

    def _named_struct(self, annotation: type[NamedStruct]) -> TypeContract:
        cached = self.struct_cache.get(annotation)
        if cached is not None:
            return cached
        hints = get_type_hints(annotation, globalns={**globals(), **self.globalns})
        fields: list[dict[str, Any]] = []
        properties: dict[str, Any] = {}
        required: list[str] = []
        manifest_fields: list[dict[str, Any]] = []
        for field_name, field_annotation in hints.items():
            if get_origin(field_annotation) is ClassVar:
                continue
            child = self.build(field_annotation)
            fields.append({"name": field_name, "type": child.schema, "nullable": child.nullable})
            properties[field_name] = child.json_schema
            manifest_fields.append({"name": field_name, "type": child.manifest_type, "required": not child.nullable})
            if not child.nullable:
                required.append(field_name)
        json_schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            json_schema["required"] = required
        contract = TypeContract(
            "STRUCT<" + ", ".join(f"{field['name']}: {_schema_type_sql(field['type'])}" for field in fields) + ">",
            {"kind": "struct", "name": annotation.__name__, "fields": fields},
            json_schema,
            {"type": "NamedStruct", "name": annotation.__name__, "fields": manifest_fields},
        )
        self.struct_cache[annotation] = contract
        return contract

    def _directory_entry(self) -> TypeContract:
        fields = [
            {"name": "path", "type": {"type": "Str"}, "required": True},
            {"name": "name", "type": {"type": "Str"}, "required": True},
            {"name": "parent_path", "type": {"type": "Str"}, "required": False},
            {"name": "entry_type", "type": {"type": "Str"}, "required": True},
            {"name": "size_bytes", "type": {"type": "Int"}, "required": False},
            {"name": "content_type", "type": {"type": "Str"}, "required": False},
            {"name": "sha256", "type": {"type": "Str"}, "required": False},
            {"name": "object_uri", "type": {"type": "Str"}, "required": False},
            {"name": "is_empty_dir", "type": {"type": "Bool"}, "required": True},
        ]
        return TypeContract(
            DIRECTORY_ENTRY_TYPE_SQL,
            {"kind": "directory_entry"},
            {
                "type": "object",
                "properties": {field["name"]: {"type": "string"} for field in fields},
                "required": ["path", "name", "entry_type", "is_empty_dir"],
                "additionalProperties": True,
                "x-agentcicd-type": "directory_entry",
            },
            {"type": "DirectoryEntry", "fields": fields},
        )

    def _directory(self) -> TypeContract:
        entry = self._directory_entry()
        return TypeContract(
            f"ARRAY<{entry.type_sql}>",
            {"kind": "directory", "element": entry.schema},
            {"type": "array", "items": entry.json_schema, "x-agentcicd-type": "directory"},
            {"type": "Directory", "element": entry.manifest_type},
        )


def _scalar(name: str, type_sql: str, json_schema: dict[str, Any]) -> TypeContract:
    return TypeContract(type_sql, {"kind": "scalar", "name": name}, json_schema, {"type": name})


def _schema_type_sql(schema: dict[str, Any]) -> str:
    kind = schema["kind"]
    if kind == "scalar":
        return {"Str": "STRING", "Int": "BIGINT", "Float": "DOUBLE", "Bool": "BOOLEAN", "Variant": "VARIANT"}[
            str(schema["name"])
        ]
    if kind == "array":
        return f"ARRAY<{_schema_type_sql(schema['element'])}>"
    if kind == "map":
        return f"MAP<{_schema_type_sql(schema['key'])}, {_schema_type_sql(schema['value'])}>"
    if kind == "struct":
        return "STRUCT<" + ", ".join(f"{field['name']}: {_schema_type_sql(field['type'])}" for field in schema["fields"]) + ">"
    if kind == "directory_entry":
        return DIRECTORY_ENTRY_TYPE_SQL
    if kind == "directory":
        return f"ARRAY<{DIRECTORY_ENTRY_TYPE_SQL}>"
    raise FixtureTypeError(f"Unsupported schema kind: {kind}")


def _type_name(value: Any) -> str:
    if isinstance(value, type):
        return value.__name__
    return str(value)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, NamedStruct):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
