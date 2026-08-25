"""AgentCICD SQL engine package root.

Keep package import lightweight so non-engine consumers can use package
metadata and file paths without importing optional runtime dependencies.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "declared_inputs_from_sql",
    "discover_registered_function_references",
    "validate_script_text",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    module = import_module("agentcicd.sql.integration")
    return getattr(module, name)
