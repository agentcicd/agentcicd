from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any


class FixtureContractError(ValueError):
    """Raised when a Python fixture source cannot produce a AgentCICD contract."""


SCHEMA_SYMBOLS = {
    "function",
    "NamedStruct",
    "Str",
    "Int",
    "Float",
    "Bool",
    "Variant",
    "Array",
    "Map",
    "Required",
    "EnvSpec",
    "Session",
    "ShellEnv",
    "AgentHarnessEnv",
    "McpSpec",
    "DirectoryEntry",
    "Directory",
}

DIRECTORY_ENTRY_TYPE_SQL = (
    "STRUCT<"
    "schema_version: STRING, "
    "dataset_path: STRING, "
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


@dataclass(frozen=True)
class ParsedFixtureType:
    type_sql: str
    schema: dict[str, Any]
    json_schema: dict[str, Any]
    nullable: bool = True


@dataclass(frozen=True)
class FixtureContract:
    entrypoint: str
    is_async: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    metadata: dict[str, Any]

    def as_tuple(self) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
        return self.entrypoint, self.input_schema, self.output_schema, self.metadata


def extract_fixture_contract(source_text: str) -> FixtureContract:
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise FixtureContractError(f"Invalid python source: {exc.msg}") from exc
    try:
        compile(tree, "<fixture>", "exec")
    except SyntaxError as exc:
        raise FixtureContractError(f"Invalid python source: {exc.msg}") from exc

    _validate_top_level_fixture_statements(tree)
    aliases = _parse_import_aliases(tree)
    struct_nodes = _collect_named_struct_nodes(tree, aliases)
    exported = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_function_decorator(decorator) for decorator in node.decorator_list)
    ]
    if len(exported) != 1:
        raise FixtureContractError("Python function source must define exactly one top-level @function entrypoint")
    function_node = exported[0]
    if function_node.args.vararg or function_node.args.kwarg:
        raise FixtureContractError("Variadic arguments are not supported")
    if function_node.args.posonlyargs or function_node.args.kwonlyargs:
        raise FixtureContractError("Only positional-or-keyword fixture arguments are supported")

    parser = _TypeParser(aliases=aliases, struct_nodes=struct_nodes)
    default_offset = len(function_node.args.args) - len(function_node.args.defaults)
    properties: dict[str, Any] = {}
    signature_parameters: list[dict[str, Any]] = []
    injected_parameters: list[dict[str, str]] = []
    for index, arg in enumerate(function_node.args.args):
        parsed = parser.parse(arg.annotation)
        if parsed.schema.get("kind") == "session":
            injected_parameters.append({"name": arg.arg, "kind": "session"})
            continue
        has_default = index >= default_offset
        properties[arg.arg] = parsed.json_schema
        signature_parameters.append(
            {
                "name": arg.arg,
                "type_sql": parsed.type_sql,
                "nullable": parsed.nullable,
                "has_default": has_default,
            }
        )

    returned = parser.parse(function_node.returns)
    input_schema = {"type": "object", "properties": properties}
    output_schema = returned.json_schema
    signature = {
        "parameters": signature_parameters,
        "return": {
            "type_sql": returned.type_sql,
            "nullable": returned.nullable,
            "schema": returned.schema,
        },
    }
    metadata = {
        "entrypoint": function_node.name,
        "async": isinstance(function_node, ast.AsyncFunctionDef),
        "signature": signature,
        "contract": {
            "entrypoint": function_node.name,
            "async": isinstance(function_node, ast.AsyncFunctionDef),
            "signature": signature,
        },
        "return_type_sql": returned.type_sql,
    }
    if injected_parameters:
        metadata["injected_parameters"] = injected_parameters
    return FixtureContract(
        entrypoint=function_node.name,
        is_async=isinstance(function_node, ast.AsyncFunctionDef),
        input_schema=input_schema,
        output_schema=output_schema,
        metadata=metadata,
    )


class _TypeParser:
    def __init__(self, *, aliases: dict[str, str], struct_nodes: dict[str, ast.ClassDef]) -> None:
        self.aliases = aliases
        self.struct_nodes = struct_nodes
        self.struct_cache: dict[str, ParsedFixtureType] = {}
        self.resolving: set[str] = set()

    def parse(self, node: ast.expr | None) -> ParsedFixtureType:
        if node is None:
            raise FixtureContractError("Type annotations are required")
        if isinstance(node, ast.Name):
            return self._parse_name(node.id)
        if isinstance(node, ast.Attribute):
            return self._parse_name(_qualified_name(node) or node.attr)
        if isinstance(node, ast.Subscript):
            target = self._resolved_tail(_qualified_name(node.value) or "")
            child = _unwrap_slice(node.slice)
            if target == "Required":
                parsed = self.parse(child)
                return ParsedFixtureType(parsed.type_sql, parsed.schema, parsed.json_schema, nullable=False)
            if target == "Array":
                parsed = self.parse(child)
                return ParsedFixtureType(
                    f"ARRAY<{parsed.type_sql}>",
                    {"kind": "array", "element": parsed.schema},
                    {"type": "array", "items": parsed.json_schema},
                )
            if target == "Map":
                if not isinstance(child, ast.Tuple) or len(child.elts) != 2:
                    raise FixtureContractError("Map annotations must be Map[Str, T]")
                key = self.parse(child.elts[0])
                if key.type_sql != "STRING":
                    raise FixtureContractError("Map keys must be Str")
                value = self.parse(child.elts[1])
                return ParsedFixtureType(
                    f"MAP<STRING, {value.type_sql}>",
                    {"kind": "map", "key": {"kind": "scalar", "name": "Str"}, "value": value.schema},
                    {"type": "object", "additionalProperties": value.json_schema},
                )
            if target == "EnvSpec":
                if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
                    raise FixtureContractError('EnvSpec annotations must be EnvSpec["kind"]')
                return self._parse_env_spec(str(child.value))
            raise FixtureContractError(f"Unsupported type annotation: {target}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            raise FixtureContractError("Union annotations are not supported; use AgentCICD schema types or explicit Variant")
        if isinstance(node, ast.Constant) and node.value is None:
            raise FixtureContractError("None is not a valid fixture contract type")
        raise FixtureContractError("Unsupported type annotation")

    def _parse_name(self, raw_name: str) -> ParsedFixtureType:
        name = self._resolved_tail(raw_name)
        scalar_map = {
            "Str": ("STRING", {"type": "string"}),
            "Int": ("BIGINT", {"type": "integer", "format": "int64"}),
            "Float": ("DOUBLE", {"type": "number"}),
            "Bool": ("BOOLEAN", {"type": "boolean"}),
            "Variant": ("VARIANT", {"type": "variant"}),
        }
        if name in scalar_map:
            type_sql, json_schema = scalar_map[name]
            return ParsedFixtureType(type_sql, {"kind": "scalar", "name": name}, json_schema)
        if name == "Session":
            return ParsedFixtureType(
                "VARIANT",
                {"kind": "session"},
                {"type": "object", "additionalProperties": True, "x-agentcicd-type": "session"},
            )
        env_aliases = {
            "ShellEnv": "shell",
            "AgentHarnessEnv": "agent_harness",
        }
        if name in env_aliases:
            return self._parse_env_spec(env_aliases[name])
        if name == "McpSpec":
            return ParsedFixtureType(
                "VARIANT",
                {"kind": "mcp_spec"},
                {"type": "object", "additionalProperties": True, "x-agentcicd-type": "mcp_spec"},
            )
        if name == "DirectoryEntry":
            return ParsedFixtureType(
                DIRECTORY_ENTRY_TYPE_SQL,
                {
                    "kind": "directory_entry",
                    "fields": [
                        {"name": "schema_version", "type": {"kind": "scalar", "name": "Str"}, "nullable": False},
                        {"name": "dataset_path", "type": {"kind": "scalar", "name": "Str"}, "nullable": False},
                        {"name": "path", "type": {"kind": "scalar", "name": "Str"}, "nullable": False},
                        {"name": "name", "type": {"kind": "scalar", "name": "Str"}, "nullable": False},
                        {"name": "parent_path", "type": {"kind": "scalar", "name": "Str"}, "nullable": True},
                        {"name": "entry_type", "type": {"kind": "scalar", "name": "Str"}, "nullable": False},
                        {"name": "size_bytes", "type": {"kind": "scalar", "name": "Int"}, "nullable": True},
                        {"name": "content_type", "type": {"kind": "scalar", "name": "Str"}, "nullable": True},
                        {"name": "sha256", "type": {"kind": "scalar", "name": "Str"}, "nullable": True},
                        {"name": "object_uri", "type": {"kind": "scalar", "name": "Str"}, "nullable": True},
                        {"name": "is_empty_dir", "type": {"kind": "scalar", "name": "Bool"}, "nullable": False},
                    ],
                },
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "schema_version": {"type": "string"},
                        "dataset_path": {"type": "string"},
                        "name": {"type": "string"},
                        "parent_path": {"type": "string"},
                        "entry_type": {"type": "string", "enum": ["file", "directory"]},
                        "size_bytes": {"type": "integer", "format": "int64"},
                        "content_type": {"type": "string"},
                        "sha256": {"type": "string"},
                        "object_uri": {"type": "string"},
                        "is_empty_dir": {"type": "boolean"},
                    },
                    "required": ["path", "dataset_path", "name", "entry_type", "is_empty_dir"],
                    "additionalProperties": True,
                    "x-agentcicd-type": "directory_entry",
                },
            )
        if name == "Directory":
            entry = self._parse_name("DirectoryEntry")
            return ParsedFixtureType(
                f"ARRAY<{entry.type_sql}>",
                {"kind": "directory", "element": entry.schema},
                {"type": "array", "items": entry.json_schema, "x-agentcicd-type": "directory"},
            )
        if name in self.struct_nodes:
            return self._parse_struct(name)
        if name in {"str", "int", "float", "bool", "Any", "dict", "Dict", "list", "List", "Optional", "TypedDict"}:
            raise FixtureContractError(
                f"Unsupported Python annotation '{name}'. Use AgentCICD schema types or explicit Variant."
            )
        raise FixtureContractError(f"Unsupported type annotation: {name}")

    def _parse_env_spec(self, kind: str) -> ParsedFixtureType:
        normalized = kind.strip().lower()
        if normalized not in {"shell", "agent_harness", "browser"}:
            raise FixtureContractError(f"Unsupported EnvSpec kind: {kind}")
        return ParsedFixtureType(
            "VARIANT",
            {"kind": "env_spec", "environment_kind": normalized},
            {"type": "object", "additionalProperties": True, "x-agentcicd-type": "env_spec", "environment_kind": normalized},
        )

    def _parse_struct(self, name: str) -> ParsedFixtureType:
        if name in self.struct_cache:
            return self.struct_cache[name]
        if name in self.resolving:
            raise FixtureContractError(f"Recursive NamedStruct definitions are not supported: {name}")
        self.resolving.add(name)
        node = self.struct_nodes[name]
        fields: list[dict[str, Any]] = []
        properties: dict[str, Any] = {}
        required: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.Pass):
                continue
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    continue
                raise FixtureContractError(f"Unsupported NamedStruct statement in {name}")
            field_name = stmt.target.id
            parsed = self.parse(stmt.annotation)
            fields.append({"name": field_name, "type": parsed.schema, "nullable": parsed.nullable})
            properties[field_name] = parsed.json_schema
            if not parsed.nullable:
                required.append(field_name)
        type_sql = "STRUCT<" + ", ".join(f"{field['name']}: {self._field_type_sql(field['type'])}" for field in fields) + ">"
        schema: dict[str, Any] = {"kind": "struct", "fields": fields}
        json_schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            json_schema["required"] = required
        parsed_struct = ParsedFixtureType(type_sql, schema, json_schema)
        self.struct_cache[name] = parsed_struct
        self.resolving.remove(name)
        return parsed_struct

    def _field_type_sql(self, schema: dict[str, Any]) -> str:
        kind = schema.get("kind")
        if kind == "scalar":
            return {
                "Str": "STRING",
                "Int": "BIGINT",
                "Float": "DOUBLE",
                "Bool": "BOOLEAN",
                "Variant": "VARIANT",
            }[str(schema.get("name"))]
        if kind == "array":
            return f"ARRAY<{self._field_type_sql(dict(schema.get('element') or {}))}>"
        if kind == "map":
            return f"MAP<STRING, {self._field_type_sql(dict(schema.get('value') or {}))}>"
        if kind == "struct":
            return "STRUCT<" + ", ".join(
                f"{field['name']}: {self._field_type_sql(dict(field.get('type') or {}))}"
                for field in list(schema.get("fields") or [])
                if isinstance(field, dict)
            ) + ">"
        if kind == "directory_entry":
            return DIRECTORY_ENTRY_TYPE_SQL
        if kind == "directory":
            return f"ARRAY<{DIRECTORY_ENTRY_TYPE_SQL}>"
        raise FixtureContractError("Unsupported schema node")

    def _resolved_tail(self, name: str) -> str:
        resolved = self.aliases.get(name, name)
        return resolved.split(".")[-1]


def _parse_import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound_name = alias.asname or alias.name
                aliases[bound_name] = f"{module}.{alias.name}" if module else alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound_name] = alias.name
    return aliases


def _collect_named_struct_nodes(tree: ast.Module, aliases: dict[str, str]) -> dict[str, ast.ClassDef]:
    nodes: dict[str, ast.ClassDef] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        base_tails = {_resolved_tail(_qualified_name(base) or "", aliases) for base in node.bases}
        if "TypedDict" in base_tails:
            raise FixtureContractError("TypedDict fixture contracts are no longer supported; use NamedStruct")
        if "NamedStruct" in base_tails:
            nodes[node.name] = node
    return nodes


def _is_function_decorator(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "function"
    if isinstance(node, ast.Attribute):
        return node.attr == "function"
    if isinstance(node, ast.Call):
        return _is_function_decorator(node.func)
    return False


def _validate_top_level_fixture_statements(tree: ast.Module) -> None:
    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in tree.body:
        if isinstance(node, allowed):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Assign) and _is_simple_constant_assignment(node.value):
            continue
        if isinstance(node, ast.AnnAssign) and (node.value is None or _is_simple_constant_assignment(node.value)):
            continue
        line = getattr(node, "lineno", "?")
        raise FixtureContractError(f"Unsupported top-level fixture statement at line {line}")


def _is_simple_constant_assignment(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool, type(None)))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_simple_constant_assignment(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_simple_constant_assignment(key) and _is_simple_constant_assignment(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def _qualified_name(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _resolved_tail(name: str, aliases: dict[str, str]) -> str:
    return aliases.get(name, name).split(".")[-1]


def _unwrap_slice(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Index) else node  # type: ignore[attr-defined]
