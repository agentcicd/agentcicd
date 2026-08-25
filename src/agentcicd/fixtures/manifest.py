from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import runpy
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Mapping, get_args, get_origin, get_type_hints

from agentcicd.fixtures.registry import EnvironmentRegistration, FunctionRegistration, REGISTRY, clear_registry
from agentcicd.fixtures.types import SCHEMA_VERSION, Environment, NamedStruct, Session, type_contract


class FixtureManifestError(ValueError):
    """Raised when a fixture manifest cannot be generated or validated."""


@dataclass(frozen=True)
class PackageIdentity:
    name: str
    version: str
    namespace: str


def generate_manifest_for_package(
    package: str,
    *,
    namespace: str | None = None,
    version: str | None = None,
    package_name: str | None = None,
) -> dict[str, Any]:
    module = importlib.import_module(package)
    _import_submodules(module)
    identity = PackageIdentity(
        name=package_name or package.replace("_", "-"),
        version=version or _module_version(module),
        namespace=namespace or package.split("_", 1)[0],
    )
    manifest = generate_manifest(identity)
    validate_manifest(manifest)
    return manifest


def generate_manifest_for_sources(
    source_paths: list[str | Path],
    *,
    namespace: str = "local",
    version: str = "0.0.0",
    package_name: str = "agentcicd-local-project",
) -> dict[str, Any]:
    normalized_paths = _normalize_source_paths(source_paths)
    with _isolated_registry():
        for index, source_path in enumerate(normalized_paths):
            _load_fixture_source(source_path, index=index)
        identity = PackageIdentity(name=package_name, version=version, namespace=namespace)
        manifest = generate_manifest(identity)
    validate_manifest(manifest)
    return manifest


def generate_manifest(identity: PackageIdentity) -> dict[str, Any]:
    functions = [_function_manifest(item, identity) for item in REGISTRY.functions]
    environments = [_environment_manifest(item, identity) for item in REGISTRY.environments]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package": {
            "name": identity.name,
            "version": identity.version,
            "namespace": identity.namespace,
        },
        "requires": {"agentcicd.fixtures": ">=0.1.0"},
        "functions": functions,
        "environments": environments,
        "product_types": [_directory_entry_product_type(), _directory_product_type()],
    }
    validate_manifest(manifest)
    return manifest


def generate_builtin_manifest() -> dict[str, Any]:
    """Generate a manifest for AgentCICD-authored built-in fixtures."""
    from agentcicd.fixtures.builtin_authoring import builtin_environment_registrations, builtin_function_registrations

    identity = PackageIdentity(
        name="agentcicd-fixtures",
        version=_module_version(importlib.import_module("agentcicd.fixtures")),
        namespace="agentcicd",
    )
    functions = [_function_manifest(item, identity) for item in builtin_function_registrations()]
    environments = [_environment_manifest(item, identity) for item in builtin_environment_registrations()]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package": {
            "name": identity.name,
            "version": identity.version,
            "namespace": identity.namespace,
        },
        "requires": {"agentcicd.fixtures": ">=0.1.0"},
        "functions": functions,
        "environments": environments,
        "product_types": [_directory_entry_product_type(), _directory_product_type()],
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise FixtureManifestError(f"Unsupported fixture manifest schema_version: {manifest.get('schema_version')!r}")
    package = manifest.get("package")
    if not isinstance(package, Mapping):
        raise FixtureManifestError("Fixture manifest requires package metadata")
    for key in ("name", "version", "namespace"):
        if not _non_empty_string(package.get(key)):
            raise FixtureManifestError(f"Fixture manifest package.{key} must be a non-empty string")
    functions = manifest.get("functions")
    if not isinstance(functions, list):
        raise FixtureManifestError("Fixture manifest functions must be a list")
    for item in functions:
        _validate_function(item)
    environments = manifest.get("environments")
    if not isinstance(environments, list):
        raise FixtureManifestError("Fixture manifest environments must be a list")
    for item in environments:
        _validate_environment(item)


def write_manifest(manifest: Mapping[str, Any], output: str | Path) -> None:
    validate_manifest(manifest)
    Path(output).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _function_manifest(item: FunctionRegistration, identity: PackageIdentity) -> dict[str, Any]:
    if item.manifest_entry is not None:
        return dict(item.manifest_entry)
    func = item.callable_object
    signature = inspect.signature(func)
    hints = get_type_hints(func, globalns=func.__globals__)
    parameters: list[dict[str, Any]] = []
    runtime_parameters: list[dict[str, Any]] = []
    injected_parameters: list[dict[str, Any]] = []
    for parameter_name, parameter in signature.parameters.items():
        if parameter.kind not in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}:
            raise FixtureManifestError(f"Function {func.__name__} uses unsupported variadic parameter {parameter_name}")
        if parameter_name not in hints:
            raise FixtureManifestError(f"Function {func.__name__} parameter {parameter_name} is missing a AgentCICD type annotation")
        if hints[parameter_name] is Session:
            injected_parameters.append({"name": parameter_name, "kind": "session"})
            continue
        contract = type_contract(hints[parameter_name], globalns=func.__globals__)
        has_default = parameter.default is not inspect.Parameter.empty
        parameters.append(
            {
                "name": parameter_name,
                "type": contract.manifest_type,
                "required": not has_default and contract.nullable,
                "nullable": contract.nullable,
                "has_default": has_default,
                "type_sql": contract.type_sql,
            }
        )
        runtime_parameters.append(
            {
                "name": parameter_name,
                "type_sql": contract.type_sql,
                "has_default": has_default,
                "nullable": contract.nullable,
            }
        )
    if "return" not in hints:
        raise FixtureManifestError(f"Function {func.__name__} is missing a return AgentCICD type annotation")
    returned = type_contract(hints["return"], globalns=func.__globals__)
    full_name = _qualified_function_name(item, identity)
    runtime_alias = full_name.replace(".", "_")
    metadata = {
        "execution_runtime": "function_runner",
        "entrypoint_name": func.__name__,
        "module": func.__module__,
        "object": func.__name__,
        "shape": "1:1",
        "return_type_sql": returned.type_sql,
        "output_schema": returned.json_schema,
        "signature": {
            "parameters": runtime_parameters,
            "return": {
                "type_sql": returned.type_sql,
                "nullable": returned.nullable,
                "schema": returned.schema,
            },
        },
    }
    if injected_parameters:
        metadata["injected_parameters"] = injected_parameters
    if item.requirements:
        metadata["runtime_imports"] = list(item.requirements)
    return {
        "name": full_name,
        "module": func.__module__,
        "object": func.__name__,
        "shape": "1:1",
        "async": inspect.iscoroutinefunction(func),
        "parameters": parameters,
        "returns": returned.manifest_type,
        "runtime": {
            "kind": "python",
            "runtime_alias": runtime_alias,
            "entrypoint": f"{func.__module__}:{func.__name__}",
        },
        "metadata": metadata,
    }


def _environment_manifest(item: EnvironmentRegistration, identity: PackageIdentity) -> dict[str, Any]:
    if item.manifest_entry is not None:
        return dict(item.manifest_entry)
    cls = item.class_object
    spec_type = _environment_spec_type(cls)
    if spec_type is None:
        raise FixtureManifestError(f"Environment {cls.__name__} must extend Environment[Spec]")
    if not isinstance(spec_type, type) or not issubclass(spec_type, NamedStruct):
        raise FixtureManifestError(f"Environment {cls.__name__} spec must be a NamedStruct")
    spec_module = importlib.import_module(spec_type.__module__)
    contract = type_contract(spec_type, globalns=spec_module.__dict__)
    module_namespace = _qualified_environment_module(item, identity)
    return {
        "name": f"{module_namespace}.{cls.__name__}",
        "spec_function": f"envs.{module_namespace}.spec",
        "module": cls.__module__,
        "class": cls.__name__,
        "spec": contract.manifest_type,
        "runtime": {
            "kind": "environment",
            "entrypoint": f"{cls.__module__}:{cls.__name__}",
        },
    }


def registered_function_specs(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    validate_manifest(manifest)
    specs: list[dict[str, Any]] = []
    for item in manifest["functions"]:
        metadata = dict(item["metadata"])
        specs.append(
            {
                "name": item["name"],
                "type": "remote",
                "call_name": item["name"],
                "runtime_alias": item["runtime"]["runtime_alias"],
                "signature": metadata["signature"],
                **metadata,
            }
        )
    for environment_item in manifest["environments"]:
        spec = environment_item["spec"]
        return_type_sql = "VARIANT"
        specs.append(
            {
                "name": environment_item["spec_function"],
                "type": "remote",
                "call_name": environment_item["spec_function"],
                "runtime_alias": environment_item["spec_function"].replace(".", "_"),
                "signature": {
                    "parameters": [
                        {
                            "name": field["name"],
                            "type_sql": _manifest_type_sql(field["type"]),
                            "has_default": not field["required"],
                        }
                        for field in spec["fields"]
                    ]
                },
                "execution_runtime": "fixture_env_spec_builder",
                "entrypoint_name": "spec",
                "return_type_sql": return_type_sql,
                "output_schema": {
                    "type": "object",
                    "additionalProperties": True,
                    "x-agentcicd-type": "env_spec",
                    "spec": spec["name"],
                },
            }
        )
    return specs


def _normalize_source_paths(source_paths: list[str | Path]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for raw_path in source_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists() or not path.is_file() or path.suffix != ".py":
            raise FixtureManifestError(f"Fixture source must be a Python file: {path}")
        normalized.append(path)
    return tuple(normalized)


@contextmanager
def _isolated_registry() -> Iterator[None]:
    previous_functions = tuple(REGISTRY.functions)
    previous_environments = tuple(REGISTRY.environments)
    clear_registry()
    try:
        yield
    finally:
        clear_registry()
        REGISTRY.functions.extend(previous_functions)
        REGISTRY.environments.extend(previous_environments)


def _load_fixture_source(source_path: Path, *, index: int) -> None:
    import agentcicd.fixtures as authoring

    globals_payload = {
        name: authoring.__dict__[name]
        for name in authoring.__all__
        if name in authoring.__dict__
    }
    parent = str(source_path.parent)
    inserted = False
    if parent not in sys.path:
        sys.path.insert(0, parent)
        inserted = True
    try:
        runpy.run_path(
            str(source_path),
            init_globals=globals_payload,
            run_name=f"agentcicd_local_fixture_{index}",
        )
    finally:
        if inserted:
            sys.path.remove(parent)


def _qualified_function_name(item: FunctionRegistration, identity: PackageIdentity) -> str:
    if item.name and "." in item.name:
        return item.name
    leaf = item.name or item.callable_object.__name__
    namespace = item.namespace or identity.namespace
    module_tail = _module_tail(item.callable_object.__module__)
    return ".".join(part for part in (namespace, module_tail, leaf) if part)


def _qualified_environment_module(item: EnvironmentRegistration, identity: PackageIdentity) -> str:
    if item.name and "." in item.name:
        return item.name
    leaf = item.name or _module_tail(item.class_object.__module__) or item.class_object.__name__.lower()
    namespace = item.namespace or identity.namespace
    return ".".join(part for part in (namespace, leaf) if part)


def _module_tail(module_name: str) -> str:
    parts = module_name.split(".")
    if len(parts) <= 1:
        return ""
    return parts[-1]


def _environment_spec_type(cls: type) -> Any | None:
    bases = cls.__dict__.get("__orig_bases__", ())
    for base in bases:
        if get_origin(base) is Environment:
            arguments = get_args(base)
            return arguments[0] if arguments else None
    init = cls.__dict__.get("__init__")
    if init is None:
        return None
    signature = inspect.signature(init)
    hints = get_type_hints(init, globalns=init.__globals__)
    for parameter_name in signature.parameters:
        if parameter_name == "self":
            continue
        return hints.get(parameter_name)
    return None


def _manifest_type_sql(raw_type: Mapping[str, Any]) -> str:
    type_name = str(raw_type["type"])
    if type_name in {"Str", "SecretId"}:
        return "STRING"
    if type_name == "Int":
        return "BIGINT"
    if type_name == "Float":
        return "DOUBLE"
    if type_name == "Bool":
        return "BOOLEAN"
    if type_name == "Variant":
        return "VARIANT"
    if type_name == "Directory":
        return "ARRAY<STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>>"
    if type_name == "DirectoryEntry":
        return "STRUCT<path: STRING, name: STRING, parent_path: STRING, entry_type: STRING, size_bytes: BIGINT, content_type: STRING, sha256: STRING, object_uri: STRING, is_empty_dir: BOOLEAN>"
    if type_name == "Array":
        return f"ARRAY<{_manifest_type_sql(raw_type['element'])}>"
    if type_name == "Map":
        return f"MAP<{_manifest_type_sql(raw_type['key'])}, {_manifest_type_sql(raw_type['value'])}>"
    if type_name == "EnvSpec":
        return "VARIANT"
    if type_name == "NamedStruct":
        return "STRUCT<" + ", ".join(
            f"{field['name']}: {_manifest_type_sql(field['type'])}" for field in raw_type["fields"]
        ) + ">"
    raise FixtureManifestError(f"Unsupported manifest type: {type_name}")


def _validate_function(item: Any) -> None:
    if not isinstance(item, Mapping):
        raise FixtureManifestError("Fixture manifest function entries must be objects")
    for key in ("name", "module", "object", "shape"):
        if not _non_empty_string(item.get(key)):
            raise FixtureManifestError(f"Fixture manifest function.{key} must be a non-empty string")
    if item["shape"] != "1:1":
        raise FixtureManifestError("Only 1:1 fixture functions are supported")
    if not isinstance(item.get("parameters"), list):
        raise FixtureManifestError("Fixture manifest function.parameters must be a list")
    for parameter in item["parameters"]:
        if not isinstance(parameter, Mapping) or not _non_empty_string(parameter.get("name")):
            raise FixtureManifestError("Fixture manifest function parameters require names")
        _validate_type(parameter.get("type"))
    _validate_type(item.get("returns"))


def _validate_environment(item: Any) -> None:
    if not isinstance(item, Mapping):
        raise FixtureManifestError("Fixture manifest environment entries must be objects")
    for key in ("name", "spec_function", "module", "class"):
        if not _non_empty_string(item.get(key)):
            raise FixtureManifestError(f"Fixture manifest environment.{key} must be a non-empty string")
    _validate_type(item.get("spec"))


def _validate_type(raw_type: Any) -> None:
    if not isinstance(raw_type, Mapping):
        raise FixtureManifestError("Fixture manifest type entries must be objects")
    type_name = raw_type.get("type")
    if not _non_empty_string(type_name):
        raise FixtureManifestError("Fixture manifest type requires a type name")
    if type_name in {"Str", "Int", "Float", "Bool", "Variant", "DirectoryEntry", "Directory", "EnvSpec"}:
        return
    if type_name == "Array":
        _validate_type(raw_type.get("element"))
        return
    if type_name == "Map":
        _validate_type(raw_type.get("key"))
        _validate_type(raw_type.get("value"))
        return
    if type_name == "NamedStruct":
        fields = raw_type.get("fields")
        if not isinstance(fields, list):
            raise FixtureManifestError("NamedStruct manifest types require fields")
        for field in fields:
            if not isinstance(field, Mapping) or not _non_empty_string(field.get("name")):
                raise FixtureManifestError("NamedStruct fields require names")
            _validate_type(field.get("type"))
        return
    raise FixtureManifestError(f"Unsupported fixture manifest type: {type_name}")


def _directory_entry_product_type() -> dict[str, Any]:
    return {"name": "DirectoryEntry", "type": "DirectoryEntry"}


def _directory_product_type() -> dict[str, Any]:
    return {"name": "Directory", "type": "Directory"}


def _module_version(module: ModuleType) -> str:
    raw_version = module.__dict__.get("__version__", "0.0.0")
    return str(raw_version)


def _import_submodules(module: ModuleType) -> None:
    raw_path = module.__dict__.get("__path__")
    if raw_path is None:
        return
    prefix = module.__name__ + "."
    for info in pkgutil.walk_packages(raw_path, prefix):
        importlib.import_module(info.name)


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
