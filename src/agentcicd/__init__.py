from __future__ import annotations

from importlib import import_module

_PROJECT_EXPORTS = {
    "load_project": ("agentcicd.project", "load_project"),
    "run_project": ("agentcicd.runtime.local_runner", "run_project"),
    "validate_project": ("agentcicd.runtime.local_runner", "validate_project"),
}

_FIXTURE_EXPORT_NAMES = (
    "AgentHarnessSpec",
    "Array",
    "Bool",
    "BrowserSpec",
    "Directory",
    "DirectoryEntry",
    "EnvSpec",
    "EnvSpecValue",
    "Environment",
    "Float",
    "Int",
    "Map",
    "MaterializedDirectory",
    "McpHttpSpec",
    "McpPlaywrightSpec",
    "McpStdioSpec",
    "NamedStruct",
    "Optional",
    "Required",
    "SecretId",
    "Session",
    "ShellCommand",
    "ShellSpec",
    "Str",
    "Variant",
    "agent_harness",
    "env_specs",
    "envs",
    "function",
    "log",
    "mcps",
    "objectstore",
    "secrets",
    "tracing",
)

__all__ = [*_PROJECT_EXPORTS, *_FIXTURE_EXPORT_NAMES]


def __getattr__(name: str) -> object:
    project_export = _PROJECT_EXPORTS.get(name)
    if project_export is not None:
        module_name, attribute_name = project_export
        return getattr(import_module(module_name), attribute_name)
    if name in _FIXTURE_EXPORT_NAMES:
        return getattr(import_module("agentcicd.fixtures"), name)
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
