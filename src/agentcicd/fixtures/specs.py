from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from agentcicd.fixtures.types import (
    AgentHarnessSpec,
    BrowserSpec,
    McpHttpSpec,
    McpPlaywrightSpec,
    McpStdioSpec,
    ShellSpec,
)


@dataclass
class EnvSpecValue:
    kind: str
    spec_type: str
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def config(self) -> "EnvSpecConfig":
        return EnvSpecConfig(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_type": "environment",
            "kind": self.kind,
            "type": self.spec_type,
            "config": dict(self.settings),
        }


class EnvSpecConfig:
    def __init__(self, spec: EnvSpecValue) -> None:
        self.spec = spec

    def to_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.spec.settings))

    def get(self, name: str, default: Any = None) -> Any:
        return self.spec.settings.get(name, default)


class AgentHarnessConfig(EnvSpecConfig):
    def add_mcp(self, key: str, mcp: EnvSpecValue) -> EnvSpecValue:
        if mcp.kind not in {"mcp.http", "mcp.stdio", "mcp.playwright"}:
            raise TypeError("agent harness MCP attachments must be MCP EnvSpec values")
        name = str(key or "").strip()
        if not name:
            raise ValueError("MCP server key is required")
        current = self.spec.settings.get("mcps")
        if isinstance(current, dict):
            mcps = current
        else:
            mcps = {}
            self.spec.settings["mcps"] = mcps
        payload = mcp.to_dict()
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        config["name"] = name
        payload["config"] = config
        mcps[name] = payload
        return self.spec

    def set_timeout(self, timeout_seconds: float) -> EnvSpecValue:
        self.spec.settings["timeout_seconds"] = timeout_seconds
        return self.spec


class AgentHarnessEnvSpecValue(EnvSpecValue):
    def __init__(self, kind: str, spec_type: str, settings: dict[str, Any]) -> None:
        super().__init__(kind=kind, spec_type=spec_type, settings=settings)
        self.config_helpers = AgentHarnessConfig(self)

    @property
    def config(self) -> AgentHarnessConfig:
        return self.config_helpers


class _SpecBuilder:
    def __init__(self, *, kind: str, spec_type: str, agent_harness: bool = False) -> None:
        self.kind = kind
        self.spec_type = spec_type
        self.agent_harness = agent_harness

    def spec(self, **config: Any) -> EnvSpecValue:
        if self.agent_harness:
            return AgentHarnessEnvSpecValue(self.kind, self.spec_type, _normalize_agent_harness_config(config))
        return EnvSpecValue(kind=self.kind, spec_type=self.spec_type, settings=dict(config))


def _normalize_agent_harness_config(config: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    if "mcps" in normalized and normalized["mcps"] is not None:
        normalized["mcps"] = _normalize_mcp_map(normalized["mcps"])
    return normalized


def _normalize_mcp_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError("agent harness mcps must be a map of key to MCP spec")
    mcps: dict[str, dict[str, Any]] = {}
    for raw_key, raw_spec in value.items():
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("MCP server key is required")
        payload = _env_spec_payload(raw_spec)
        config = payload.get("config") if isinstance(payload.get("config"), dict) else None
        if config is not None:
            config["name"] = key
            payload["config"] = config
        else:
            payload["name"] = key
        mcps[key] = payload
    return mcps


def _env_spec_payload(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    payload = to_dict() if callable(to_dict) else value
    if not isinstance(payload, Mapping):
        raise TypeError("agent harness mcps values must be MCP spec objects")
    return dict(payload)


class _McpBuilders:
    http = _SpecBuilder(kind="mcp.http", spec_type=McpHttpSpec.__name__)
    stdio = _SpecBuilder(kind="mcp.stdio", spec_type=McpStdioSpec.__name__)
    playwright = _SpecBuilder(kind="mcp.playwright", spec_type=McpPlaywrightSpec.__name__)


class EnvSpecBuilders:
    browser = _SpecBuilder(kind="browser", spec_type=BrowserSpec.__name__)
    shell = _SpecBuilder(kind="shell", spec_type=ShellSpec.__name__)
    agent_harness = _SpecBuilder(kind="agent_harness", spec_type=AgentHarnessSpec.__name__, agent_harness=True)
    mcp = _McpBuilders()


env_specs = EnvSpecBuilders()
agent_harness = env_specs.agent_harness
mcps = env_specs.mcp
