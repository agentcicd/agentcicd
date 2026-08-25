from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str
    endpoint: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    required: bool = False
    secret_id: str | None = None
    allow_tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()
    default_tools_approval_mode: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


def coerce_mcp_servers(value: Any) -> tuple[McpServerConfig, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        raw_items = value.items()
    elif isinstance(value, (list, tuple)):
        raw_items = ((None, item) for item in value)
    else:
        raise ValueError("mcps must be a map or array")

    servers: list[McpServerConfig] = []
    names: set[str] = set()
    for raw_name, item in raw_items:
        server = _coerce_mcp_server(raw_name, item)
        if server.name in names:
            raise ValueError(f"duplicate MCP server name '{server.name}'")
        names.add(server.name)
        servers.append(server)
    return tuple(servers)


def _coerce_mcp_server(raw_name: Any, item: Any) -> McpServerConfig:
    if not isinstance(item, Mapping):
        raise ValueError("each MCP spec must be an object")
    payload = dict(item)
    if raw_name is not None and not payload.get("name"):
        payload["name"] = raw_name

    spec_type = str(payload.get("spec_type") or "").strip().lower()
    transport = str(payload.get("transport") or "").strip().lower()
    if spec_type != "mcp" or transport not in {"http", "stdio"}:
        raise ValueError("only envs.mcp.http.spec and envs.mcp.stdio.spec MCP specs are supported")

    name = _coerce_mcp_name(payload.get("name"))
    common = {
        "name": name,
        "transport": transport,
        "required": bool(payload.get("required")),
        "allow_tools": _coerce_string_tuple(payload.get("allow_tools"), field_name="allow_tools"),
        "deny_tools": _coerce_string_tuple(payload.get("deny_tools"), field_name="deny_tools"),
        "default_tools_approval_mode": _coerce_mcp_tools_approval_mode(
            payload.get("default_tools_approval_mode")
        ),
        "metadata": _coerce_mcp_metadata(payload),
    }
    if transport == "http":
        endpoint = str(payload.get("endpoint") or payload.get("url") or "").strip()
        if not endpoint:
            raise ValueError(f"MCP server '{name}' requires endpoint")
        return McpServerConfig(
            **common,
            endpoint=endpoint,
            secret_id=_optional_nonempty_string(payload.get("secret_id")),
            headers=_coerce_string_mapping(payload.get("headers"), field_name="headers"),
        )

    command = str(payload.get("command") or payload.get("program") or "").strip()
    if not command:
        raise ValueError(f"MCP server '{name}' requires command")
    return McpServerConfig(
        **common,
        command=command,
        args=_coerce_string_tuple(payload.get("args"), field_name="args"),
        env=_coerce_string_mapping(payload.get("env"), field_name="env"),
    )


def _coerce_mcp_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    playwright = item.get("playwright")
    if isinstance(playwright, Mapping):
        metadata["playwright"] = dict(playwright)
    return metadata


def _coerce_mcp_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("MCP server name is required")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if any(char not in allowed for char in name):
        raise ValueError(f"MCP server name '{name}' may contain only letters, numbers, underscores, and dashes")
    return name


def _optional_nonempty_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_mcp_tools_approval_mode(value: Any) -> str | None:
    text = _optional_nonempty_string(value)
    if text is None:
        return None
    if text not in {"auto", "prompt", "approve"}:
        raise ValueError("default_tools_approval_mode must be one of auto, prompt, or approve")
    return text


def _coerce_string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return tuple(result)


def _coerce_string_mapping(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        text_key = str(key or "").strip()
        text_value = str(item or "").strip()
        if text_key and text_value:
            result[text_key] = text_value
    return result
