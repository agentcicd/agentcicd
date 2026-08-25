"""
Tests for authentication configuration dataclasses.

Verifies all authentication types can be instantiated with correct
attributes and maintain immutability.
"""
import pytest

from agentcicd_fixtures_aisystem.auth_config import (
    ApiKeyAuth,
    AuthConfig,
    BasicAuth,
    BearerTokenAuth,
    CustomHeaderAuth,
    OAuth2ClientCredentialsAuth,
)


class TestAuthConfig:
    """Test base AuthConfig class."""

    def test_base_auth_config_defaults(self):
        """Test AuthConfig with default values."""
        auth = AuthConfig()

        assert auth.url is None
        assert auth.additional_params is None

    def test_base_auth_config_with_url(self):
        """Test AuthConfig with custom URL."""
        auth = AuthConfig(url="https://api.example.com")

        assert auth.url == "https://api.example.com"
        assert auth.additional_params is None

    def test_base_auth_config_with_additional_params(self):
        """Test AuthConfig with additional parameters."""
        params = {"timeout": 30, "retries": 3}
        auth = AuthConfig(url="https://api.example.com", additional_params=params)

        assert auth.url == "https://api.example.com"
        assert auth.additional_params == params

    def test_auth_config_immutable(self):
        """Test AuthConfig is immutable (frozen dataclass)."""
        auth = AuthConfig(url="https://api.example.com")

        with pytest.raises(AttributeError):
            auth.url = "https://new-url.com"


class TestBearerTokenAuth:
    """Test BearerTokenAuth configuration."""

    def test_bearer_token_auth_required_fields(self):
        """Test BearerTokenAuth with required token field."""
        auth = BearerTokenAuth(token="secret-bearer-token")

        assert auth.kind == "bearer"
        assert auth.token == "secret-bearer-token"
        assert auth.url is None
        assert auth.additional_params is None

    def test_bearer_token_auth_with_url(self):
        """Test BearerTokenAuth with custom URL."""
        auth = BearerTokenAuth(
            token="secret-bearer-token", url="https://api.example.com"
        )

        assert auth.token == "secret-bearer-token"
        assert auth.url == "https://api.example.com"

    def test_bearer_token_auth_kind_is_class_var(self):
        """Test that kind is a class variable, not instance attribute."""
        auth1 = BearerTokenAuth(token="token1")
        auth2 = BearerTokenAuth(token="token2")

        assert auth1.kind == auth2.kind == "bearer"

    def test_bearer_token_auth_immutable(self):
        """Test BearerTokenAuth is immutable."""
        auth = BearerTokenAuth(token="secret-token")

        with pytest.raises(AttributeError):
            auth.token = "new-token"


class TestApiKeyAuth:
    """Test ApiKeyAuth configuration."""

    def test_api_key_auth_required_fields(self):
        """Test ApiKeyAuth with required api_key field."""
        auth = ApiKeyAuth(api_key="my-api-key-123")

        assert auth.kind == "api_key"
        assert auth.api_key == "my-api-key-123"
        assert auth.header_name == "X-API-Key"

    def test_api_key_auth_custom_header_name(self):
        """Test ApiKeyAuth with custom header name."""
        auth = ApiKeyAuth(api_key="my-api-key-123", header_name="X-Custom-API-Key")

        assert auth.api_key == "my-api-key-123"
        assert auth.header_name == "X-Custom-API-Key"

    def test_api_key_auth_with_url(self):
        """Test ApiKeyAuth with custom URL."""
        auth = ApiKeyAuth(
            api_key="my-api-key-123", url="https://api.example.com"
        )

        assert auth.api_key == "my-api-key-123"
        assert auth.url == "https://api.example.com"

    def test_api_key_auth_immutable(self):
        """Test ApiKeyAuth is immutable."""
        auth = ApiKeyAuth(api_key="key-123")

        with pytest.raises(AttributeError):
            auth.api_key = "new-key"


class TestBasicAuth:
    """Test BasicAuth configuration."""

    def test_basic_auth_required_fields(self):
        """Test BasicAuth with required username and password."""
        auth = BasicAuth(username="admin", password="secret123")

        assert auth.kind == "basic"
        assert auth.username == "admin"
        assert auth.password == "secret123"

    def test_basic_auth_with_url(self):
        """Test BasicAuth with custom URL."""
        auth = BasicAuth(
            username="admin", password="secret123", url="https://api.example.com"
        )

        assert auth.username == "admin"
        assert auth.password == "secret123"
        assert auth.url == "https://api.example.com"

    def test_basic_auth_immutable(self):
        """Test BasicAuth is immutable."""
        auth = BasicAuth(username="admin", password="secret123")

        with pytest.raises(AttributeError):
            auth.username = "newuser"


class TestOAuth2ClientCredentialsAuth:
    """Test OAuth2ClientCredentialsAuth configuration."""

    def test_oauth2_required_fields(self):
        """Test OAuth2ClientCredentialsAuth with required fields."""
        auth = OAuth2ClientCredentialsAuth(
            token_url="https://auth.example.com/token",
            client_id="client-123",
            client_secret="secret-456",
        )

        assert auth.kind == "oauth2_client_credentials"
        assert auth.token_url == "https://auth.example.com/token"
        assert auth.client_id == "client-123"
        assert auth.client_secret == "secret-456"
        assert auth.scopes is None
        assert auth.extra_params is None

    def test_oauth2_with_scopes(self):
        """Test OAuth2ClientCredentialsAuth with scopes."""
        scopes = ["read", "write", "admin"]
        auth = OAuth2ClientCredentialsAuth(
            token_url="https://auth.example.com/token",
            client_id="client-123",
            client_secret="secret-456",
            scopes=scopes,
        )

        assert auth.scopes == scopes

    def test_oauth2_with_extra_params(self):
        """Test OAuth2ClientCredentialsAuth with extra parameters."""
        extra = {"audience": "api.example.com", "resource": "default"}
        auth = OAuth2ClientCredentialsAuth(
            token_url="https://auth.example.com/token",
            client_id="client-123",
            client_secret="secret-456",
            extra_params=extra,
        )

        assert auth.extra_params == extra

    def test_oauth2_with_all_fields(self):
        """Test OAuth2ClientCredentialsAuth with all fields."""
        auth = OAuth2ClientCredentialsAuth(
            token_url="https://auth.example.com/token",
            client_id="client-123",
            client_secret="secret-456",
            scopes=["read", "write"],
            extra_params={"audience": "api.example.com"},
            url="https://api.example.com",
        )

        assert auth.token_url == "https://auth.example.com/token"
        assert auth.scopes == ["read", "write"]
        assert auth.extra_params == {"audience": "api.example.com"}
        assert auth.url == "https://api.example.com"

    def test_oauth2_immutable(self):
        """Test OAuth2ClientCredentialsAuth is immutable."""
        auth = OAuth2ClientCredentialsAuth(
            token_url="https://auth.example.com/token",
            client_id="client-123",
            client_secret="secret-456",
        )

        with pytest.raises(AttributeError):
            auth.client_id = "new-client"


class TestCustomHeaderAuth:
    """Test CustomHeaderAuth configuration."""

    def test_custom_header_auth_required_fields(self):
        """Test CustomHeaderAuth with required fields."""
        auth = CustomHeaderAuth(header_name="X-Custom-Auth", value="custom-value-123")

        assert auth.kind == "custom_header"
        assert auth.header_name == "X-Custom-Auth"
        assert auth.value == "custom-value-123"

    def test_custom_header_auth_with_url(self):
        """Test CustomHeaderAuth with custom URL."""
        auth = CustomHeaderAuth(
            header_name="X-Custom-Auth",
            value="custom-value-123",
            url="https://api.example.com",
        )

        assert auth.header_name == "X-Custom-Auth"
        assert auth.value == "custom-value-123"
        assert auth.url == "https://api.example.com"

    def test_custom_header_auth_immutable(self):
        """Test CustomHeaderAuth is immutable."""
        auth = CustomHeaderAuth(header_name="X-Custom-Auth", value="value-123")

        with pytest.raises(AttributeError):
            auth.value = "new-value"
