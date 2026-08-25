"""
Tests for authentication header generation.

Verifies that different auth configurations generate correct HTTP headers,
including OAuth2 token fetching.
"""
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pytest

from agentcicd_fixtures_aisystem.auth_config import (
    ApiKeyAuth,
    AuthConfig,
    BasicAuth,
    BearerTokenAuth,
    CustomHeaderAuth,
    OAuth2ClientCredentialsAuth,
)
from agentcicd_fixtures_aisystem.auth_headers import auth_headers


class TestBearerTokenAuthHeaders:
    """Test header generation for bearer token authentication."""

    def test_bearer_token_header(self):
        """Test bearer token generates correct Authorization header."""
        auth = BearerTokenAuth(token="my-secret-token-123")
        headers = auth_headers(auth)

        assert headers == {"Authorization": "Bearer my-secret-token-123"}

    def test_bearer_token_with_special_chars(self):
        """Test bearer token with special characters."""
        auth = BearerTokenAuth(token="token.with-special_chars/123")
        headers = auth_headers(auth)

        assert headers == {"Authorization": "Bearer token.with-special_chars/123"}


class TestApiKeyAuthHeaders:
    """Test header generation for API key authentication."""

    def test_api_key_default_header_name(self):
        """Test API key with default header name."""
        auth = ApiKeyAuth(api_key="api-key-12345")
        headers = auth_headers(auth)

        assert headers == {"X-API-Key": "api-key-12345"}

    def test_api_key_custom_header_name(self):
        """Test API key with custom header name."""
        auth = ApiKeyAuth(api_key="api-key-12345", header_name="X-Custom-API-Key")
        headers = auth_headers(auth)

        assert headers == {"X-Custom-API-Key": "api-key-12345"}

    def test_api_key_with_special_chars(self):
        """Test API key with special characters."""
        auth = ApiKeyAuth(api_key="key_with-special.chars/123")
        headers = auth_headers(auth)

        assert headers == {"X-API-Key": "key_with-special.chars/123"}


class TestBasicAuthHeaders:
    """Test header generation for basic authentication."""

    def test_basic_auth_header(self):
        """Test basic auth generates correct encoded header."""
        auth = BasicAuth(username="admin", password="secret123")
        headers = auth_headers(auth)

        expected_token = base64.b64encode(b"admin:secret123").decode("utf-8")
        assert headers == {"Authorization": f"Basic {expected_token}"}

    def test_basic_auth_with_special_chars(self):
        """Test basic auth with special characters in credentials."""
        auth = BasicAuth(username="user@example.com", password="p@ssw0rd!#$")
        headers = auth_headers(auth)

        expected_token = base64.b64encode(b"user@example.com:p@ssw0rd!#$").decode(
            "utf-8"
        )
        assert headers == {"Authorization": f"Basic {expected_token}"}

    def test_basic_auth_empty_password(self):
        """Test basic auth with empty password."""
        auth = BasicAuth(username="admin", password="")
        headers = auth_headers(auth)

        expected_token = base64.b64encode(b"admin:").decode("utf-8")
        assert headers == {"Authorization": f"Basic {expected_token}"}


class TestCustomHeaderAuthHeaders:
    """Test header generation for custom header authentication."""

    def test_custom_header(self):
        """Test custom header auth."""
        auth = CustomHeaderAuth(header_name="X-Custom-Auth", value="custom-value-123")
        headers = auth_headers(auth)

        assert headers == {"X-Custom-Auth": "custom-value-123"}

    def test_custom_header_with_complex_value(self):
        """Test custom header with complex value."""
        auth = CustomHeaderAuth(
            header_name="X-Signature", value="sha256=abc123def456"
        )
        headers = auth_headers(auth)

        assert headers == {"X-Signature": "sha256=abc123def456"}


class TestBaseAuthConfigHeaders:
    """Test header generation for base AuthConfig."""

    def test_base_auth_config_returns_empty(self):
        """Test base AuthConfig returns empty headers."""
        auth = AuthConfig()
        headers = auth_headers(auth)

        assert headers == {}

    def test_base_auth_config_with_url_returns_empty(self):
        """Test base AuthConfig with URL still returns empty headers."""
        auth = AuthConfig(url="https://api.example.com")
        headers = auth_headers(auth)

        assert headers == {}


class MockOAuth2Server:
    """Mock OAuth2 server for testing token fetching."""

    def __init__(self, port: int = 0):
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[Thread] = None
        self.received_request: Optional[dict] = None

    def start(self):
        """Start the mock server in a background thread."""
        test_server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers["Content-Length"])
                body = self.rfile.read(content_length).decode("utf-8")
                parsed = parse_qs(body)
                test_server.received_request = {
                    key: value[0] if len(value) == 1 else value
                    for key, value in parsed.items()
                }

                response = {"access_token": "mock-access-token-xyz", "expires_in": 3600}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))

            def log_message(self, format, *args):
                pass

        self.server = HTTPServer(("localhost", self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the mock server."""
        if self.server:
            self.server.shutdown()


class TestOAuth2ClientCredentialsAuthHeaders:
    """Test header generation for OAuth2 client credentials authentication."""

    def test_oauth2_basic_flow(self):
        """Test OAuth2 client credentials flow with token fetch."""
        server = MockOAuth2Server()
        server.start()

        try:
            auth = OAuth2ClientCredentialsAuth(
                token_url=f"http://localhost:{server.port}/token",
                client_id="test-client-id",
                client_secret="test-client-secret",
            )
            headers = auth_headers(auth)

            assert headers == {"Authorization": "Bearer mock-access-token-xyz"}
            assert server.received_request is not None
            assert server.received_request["grant_type"] == "client_credentials"
            assert server.received_request["client_id"] == "test-client-id"
            assert server.received_request["client_secret"] == "test-client-secret"
        finally:
            server.stop()

    def test_oauth2_with_scopes(self):
        """Test OAuth2 with scopes."""
        server = MockOAuth2Server()
        server.start()

        try:
            auth = OAuth2ClientCredentialsAuth(
                token_url=f"http://localhost:{server.port}/token",
                client_id="test-client-id",
                client_secret="test-client-secret",
                scopes=["read", "write", "admin"],
            )
            headers = auth_headers(auth)

            assert headers == {"Authorization": "Bearer mock-access-token-xyz"}
            assert server.received_request["scope"] == "read write admin"
        finally:
            server.stop()

    def test_oauth2_with_extra_params(self):
        """Test OAuth2 with extra parameters."""
        server = MockOAuth2Server()
        server.start()

        try:
            auth = OAuth2ClientCredentialsAuth(
                token_url=f"http://localhost:{server.port}/token",
                client_id="test-client-id",
                client_secret="test-client-secret",
                extra_params={"audience": "api.example.com", "resource": "default"},
            )
            headers = auth_headers(auth)

            assert headers == {"Authorization": "Bearer mock-access-token-xyz"}
            assert server.received_request["audience"] == "api.example.com"
            assert server.received_request["resource"] == "default"
        finally:
            server.stop()

    def test_oauth2_missing_access_token(self):
        """Test OAuth2 with response missing access_token."""
        class BadTokenHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)
                response = {"expires_in": 3600}
                encoded_response = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded_response)))
                self.end_headers()
                self.wfile.write(encoded_response)

            def log_message(self, format, *args):
                pass

        bad_server = HTTPServer(("localhost", 0), BadTokenHandler)
        server_port = bad_server.server_address[1]
        thread = Thread(target=bad_server.serve_forever, daemon=True)
        thread.start()

        try:
            auth = OAuth2ClientCredentialsAuth(
                token_url=f"http://localhost:{server_port}/token",
                client_id="test-client-id",
                client_secret="test-client-secret",
            )

            with pytest.raises(ValueError, match="access_token"):
                auth_headers(auth)
        finally:
            bad_server.shutdown()
            bad_server.server_close()
