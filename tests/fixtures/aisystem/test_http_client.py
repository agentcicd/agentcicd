"""
Tests for HTTP client utilities.

Verifies aiohttp session and timeout configuration.
"""
import pytest
from aiohttp import ClientSession, ClientTimeout

from agentcicd_fixtures_aisystem.http_client import build_aiohttp_timeout, create_aiohttp_session
from agentcicd_fixtures_core.timeout import TimeoutConfig


class TestBuildAiohttpTimeout:
    """Test aiohttp timeout builder."""

    def test_none_config_returns_none(self):
        """Test that None config returns None timeout."""
        result = build_aiohttp_timeout(None)

        assert result is None

    def test_timeout_config_all_fields(self):
        """Test timeout config with all fields set."""
        config = TimeoutConfig(
            timeout=120.0,
            read=90.0,
            connect=30.0,
        )
        result = build_aiohttp_timeout(config)

        assert isinstance(result, ClientTimeout)
        assert result.total == 120.0
        assert result.sock_read == 90.0
        assert result.connect == 30.0

    def test_timeout_config_defaults(self):
        """Test timeout config with default values."""
        config = TimeoutConfig()
        result = build_aiohttp_timeout(config)

        assert isinstance(result, ClientTimeout)
        assert result.total == 60
        assert result.sock_read == 60
        assert result.connect == 20

    def test_timeout_config_partial_values(self):
        """Test timeout config with some custom values."""
        config = TimeoutConfig(timeout=100.0, connect=15.0)
        result = build_aiohttp_timeout(config)

        assert isinstance(result, ClientTimeout)
        assert result.total == 100.0
        assert result.connect == 15.0
        assert result.sock_read == 60

    def test_timeout_config_none_values(self):
        """Test timeout config with None values."""
        config = TimeoutConfig(timeout=None, read=None, connect=None)
        result = build_aiohttp_timeout(config)

        assert isinstance(result, ClientTimeout)
        assert result.total is None
        assert result.sock_read is None
        assert result.connect is None


class TestCreateAiohttpSession:
    """Test aiohttp session creation."""

    @pytest.mark.asyncio
    async def test_session_no_config(self):
        """Test creating session without any configuration."""
        session = create_aiohttp_session()

        assert isinstance(session, ClientSession)
        # aiohttp defaults to 300 seconds when no timeout is provided
        assert session.timeout.total == 300
        await session.close()

    @pytest.mark.asyncio
    async def test_session_with_timeout_config(self):
        """Test creating session with timeout config."""
        config = TimeoutConfig(timeout=90.0, read=60.0, connect=25.0)
        session = create_aiohttp_session(timeout_config=config)

        assert isinstance(session, ClientSession)
        assert session.timeout.total == 90.0
        assert session.timeout.sock_read == 60.0
        assert session.timeout.connect == 25.0
        await session.close()

    @pytest.mark.asyncio
    async def test_session_with_headers(self):
        """Test creating session with custom headers."""
        headers = {"User-Agent": "TestClient/1.0", "X-Custom-Header": "value"}
        session = create_aiohttp_session(headers=headers)

        assert isinstance(session, ClientSession)
        assert session.headers.get("User-Agent") == "TestClient/1.0"
        assert session.headers.get("X-Custom-Header") == "value"
        await session.close()

    @pytest.mark.asyncio
    async def test_session_with_timeout_and_headers(self):
        """Test creating session with both timeout config and headers."""
        config = TimeoutConfig(timeout=120.0, connect=30.0)
        headers = {"Authorization": "Bearer test-token"}
        session = create_aiohttp_session(timeout_config=config, headers=headers)

        assert isinstance(session, ClientSession)
        assert session.timeout.total == 120.0
        assert session.timeout.connect == 30.0
        assert session.headers.get("Authorization") == "Bearer test-token"
        await session.close()

    @pytest.mark.asyncio
    async def test_session_with_none_timeout_config(self):
        """Test creating session with explicit None timeout config."""
        session = create_aiohttp_session(timeout_config=None)

        assert isinstance(session, ClientSession)
        # aiohttp defaults to 300 seconds when no timeout is provided
        assert session.timeout.total == 300
        await session.close()

    @pytest.mark.asyncio
    async def test_session_with_empty_headers(self):
        """Test creating session with empty headers dict."""
        session = create_aiohttp_session(headers={})

        assert isinstance(session, ClientSession)
        await session.close()
