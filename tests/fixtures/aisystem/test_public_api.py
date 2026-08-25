"""
Tests for agentcicd_fixtures.aisystem public API exports and error paths.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

import agentcicd.fixtures.aisystem as aisystem
from agentcicd.fixtures.aisystem.auth_config import OAuth2ClientCredentialsAuth
from agentcicd.fixtures.aisystem.auth_headers import auth_headers


def test_public_exports_present() -> None:
    exported_names = set(aisystem.__all__)
    required = {
        "ApiKeyAuth",
        "AuthConfig",
        "AuthMetadata",
        "BasicAuth",
        "BearerTokenAuth",
        "CompletionRequest",
        "CompletionResponse",
        "CustomHeaderAuth",
        "OAuth2ClientCredentialsAuth",
        "auth_headers",
        "build_aiohttp_timeout",
        "acompletion",
        "completion",
        "create_aiohttp_session",
    }

    assert required.issubset(exported_names)
    for name in required:
        assert hasattr(aisystem, name)


def test_oauth2_invalid_json_response_raises() -> None:
    class BadJsonHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length:
                self.rfile.read(content_length)
            response_body = b"not-json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format, *args):  # noqa: D401, A003
            pass

    server = HTTPServer(("localhost", 0), BadJsonHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        auth = OAuth2ClientCredentialsAuth(
            token_url=f"http://localhost:{port}/token",
            client_id="client-id",
            client_secret="client-secret",
        )
        with pytest.raises(json.JSONDecodeError):
            auth_headers(auth)
    finally:
        server.shutdown()
        server.server_close()
