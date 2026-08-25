from __future__ import annotations

import base64
import json
from typing import Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .auth_config import (
    ApiKeyAuth,
    AuthConfig,
    BasicAuth,
    BearerTokenAuth,
    CustomHeaderAuth,
    OAuth2ClientCredentialsAuth,
)


def _fetch_oauth2_access_token(auth: OAuth2ClientCredentialsAuth) -> str:
    payload: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": auth.client_id,
        "client_secret": auth.client_secret,
    }
    if auth.scopes:
        payload["scope"] = " ".join(auth.scopes)
    if auth.extra_params:
        payload.update(auth.extra_params)
    encoded = urlencode(payload).encode("utf-8")
    request = Request(
        auth.token_url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request) as response:
        body = response.read().decode("utf-8")
    token_payload = json.loads(body)
    access_token = token_payload.get("access_token")
    if not access_token:
        raise ValueError("OAuth2 token response did not include access_token")
    return access_token


def auth_headers(auth: AuthConfig) -> Mapping[str, str]:
    if isinstance(auth, BearerTokenAuth):
        return {"Authorization": f"Bearer {auth.token}"}
    if isinstance(auth, ApiKeyAuth):
        return {auth.header_name: auth.api_key}
    if isinstance(auth, BasicAuth):
        token = base64.b64encode(f"{auth.username}:{auth.password}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {token}"}
    if isinstance(auth, OAuth2ClientCredentialsAuth):
        token = _fetch_oauth2_access_token(auth)
        return {"Authorization": f"Bearer {token}"}
    if isinstance(auth, CustomHeaderAuth):
        return {auth.header_name: auth.value}
    return {}
