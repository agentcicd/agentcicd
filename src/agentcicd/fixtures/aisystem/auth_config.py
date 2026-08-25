from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Dict, List, Mapping, Optional, Sequence, Union
from agentcicd.fixtures.core.types import Json


@dataclass(frozen=True, kw_only=True)
class AuthConfig:
    url: Optional[str] = None
    additional_params: Optional[Mapping[str, Json]] = None


@dataclass(frozen=True, kw_only=True)
class BearerTokenAuth(AuthConfig):
    kind: ClassVar[str] = "bearer"
    token: str


@dataclass(frozen=True, kw_only=True)
class ApiKeyAuth(AuthConfig):
    kind: ClassVar[str] = "api_key"
    api_key: str
    header_name: str = "X-API-Key"


@dataclass(frozen=True, kw_only=True)
class BasicAuth(AuthConfig):
    kind: ClassVar[str] = "basic"
    username: str
    password: str


@dataclass(frozen=True, kw_only=True)
class OAuth2ClientCredentialsAuth(AuthConfig):
    kind: ClassVar[str] = "oauth2_client_credentials"
    token_url: str
    client_id: str
    client_secret: str
    scopes: Optional[Sequence[str]] = None
    extra_params: Optional[Mapping[str, str]] = None


@dataclass(frozen=True, kw_only=True)
class CustomHeaderAuth(AuthConfig):
    kind: ClassVar[str] = "custom_header"
    header_name: str
    value: str


AuthMetadata = AuthConfig
