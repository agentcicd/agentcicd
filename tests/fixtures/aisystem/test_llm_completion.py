"""
Tests for LLM completion request and response handling.

Verifies request building, response coercion, and header merging logic.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch

from agentcicd_fixtures_aisystem.auth_config import (
    ApiKeyAuth,
    BearerTokenAuth,
    OAuth2ClientCredentialsAuth,
)
from agentcicd_fixtures_aisystem.llm_completion import (
    CompletionRequest,
    CompletionResponse,
    _build_completion_kwargs,
    _coerce_response,
    _merge_additional_params,
    _merge_headers,
    acompletion,
    completion,
)


class TestCompletionRequest:
    """Test CompletionRequest model."""

    def test_minimal_request(self):
        """Test creating request with only model."""
        req = CompletionRequest(model="gpt-4")

        assert req.model == "gpt-4"
        assert req.messages == []
        assert req.timeout is None
        assert req.temperature is None

    def test_request_with_messages(self):
        """Test creating request with messages."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        req = CompletionRequest(model="gpt-4", messages=messages)

        assert req.model == "gpt-4"
        assert req.messages == messages

    def test_request_with_all_common_fields(self):
        """Test creating request with commonly used fields."""
        req = CompletionRequest(
            model="gpt-4",
            messages=[{"role": "user", "content": "test"}],
            temperature=0.7,
            max_tokens=100,
            timeout=30.0,
            top_p=0.9,
        )

        assert req.model == "gpt-4"
        assert req.temperature == 0.7
        assert req.max_tokens == 100
        assert req.timeout == 30.0
        assert req.top_p == 0.9

    def test_request_extra_fields_allowed(self):
        """Test that extra fields are allowed due to Config.extra = 'allow'."""
        req = CompletionRequest(model="gpt-4", custom_field="custom_value")

        assert req.model == "gpt-4"
        assert hasattr(req, "custom_field")

    def test_request_model_dump_excludes_none(self):
        """Test that model_dump excludes None values."""
        req = CompletionRequest(model="gpt-4", temperature=0.7)
        dumped = req.model_dump(exclude_none=True)

        assert "model" in dumped
        assert "temperature" in dumped
        assert "max_tokens" not in dumped
        assert "top_p" not in dumped


class TestCompletionResponse:
    """Test CompletionResponse model."""

    def test_empty_response(self):
        """Test creating empty response."""
        resp = CompletionResponse()

        assert resp.id is None
        assert resp.choices is None
        assert resp.usage is None

    def test_response_with_fields(self):
        """Test creating response with fields."""
        resp = CompletionResponse(
            id="resp-123",
            model="gpt-4",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Hi!"}}],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

        assert resp.id == "resp-123"
        assert resp.model == "gpt-4"
        assert len(resp.choices) == 1
        assert resp.usage["total_tokens"] == 15

    def test_response_extra_fields_allowed(self):
        """Test that extra fields are allowed in response."""
        resp = CompletionResponse(id="resp-123", custom_field="value")

        assert resp.id == "resp-123"
        assert hasattr(resp, "custom_field")


class TestCoerceResponse:
    """Test response coercion logic."""

    def test_coerce_completion_response(self):
        """Test coercing CompletionResponse returns as-is."""
        original = CompletionResponse(id="resp-123", model="gpt-4")
        result = _coerce_response(original)

        assert result is original

    def test_coerce_pydantic_model_with_model_dump(self):
        """Test coercing object with model_dump method."""
        mock_obj = Mock()
        mock_obj.model_dump.return_value = {
            "id": "resp-456",
            "model": "gpt-3.5-turbo",
        }

        result = _coerce_response(mock_obj)

        assert isinstance(result, CompletionResponse)
        assert result.id == "resp-456"
        assert result.model == "gpt-3.5-turbo"

    def test_coerce_object_with_dict_method(self):
        """Test coercing object with dict method."""
        mock_obj = Mock(spec=[])
        mock_obj.dict = lambda: {"id": "resp-789", "model": "claude-2"}

        result = _coerce_response(mock_obj)

        assert isinstance(result, CompletionResponse)
        assert result.id == "resp-789"
        assert result.model == "claude-2"

    def test_coerce_dict(self):
        """Test coercing plain dict."""
        data = {"id": "resp-999", "model": "llama-2", "choices": []}
        result = _coerce_response(data)

        assert isinstance(result, CompletionResponse)
        assert result.id == "resp-999"
        assert result.model == "llama-2"

    def test_coerce_invalid_object(self):
        """Test coercing invalid object returns empty response."""
        result = _coerce_response("invalid")

        assert isinstance(result, CompletionResponse)
        assert result.id is None


class TestMergeHeaders:
    """Test header merging logic."""

    def test_merge_no_auth_no_extra(self):
        """Test merging with no auth and no extra headers."""
        result = _merge_headers(None, None)

        assert result is None

    def test_merge_only_extra_headers(self):
        """Test merging with only extra headers."""
        extra = {"X-Custom": "value", "User-Agent": "test"}
        result = _merge_headers(None, extra)

        assert result == extra

    def test_merge_only_auth_headers(self):
        """Test merging with only auth headers."""
        auth = BearerTokenAuth(token="secret-token")
        result = _merge_headers(auth, None)

        assert result == {"Authorization": "Bearer secret-token"}

    def test_merge_both_headers(self):
        """Test merging both auth and extra headers."""
        auth = ApiKeyAuth(api_key="key-123")
        extra = {"User-Agent": "test-client"}
        result = _merge_headers(auth, extra)

        assert isinstance(result, dict)
        assert result["X-API-Key"] == "key-123"
        assert result["User-Agent"] == "test-client"

    def test_merge_auth_overrides_extra(self):
        """Test that auth headers override extra headers."""
        auth = BearerTokenAuth(token="auth-token")
        extra = {"Authorization": "Bearer old-token", "X-Custom": "value"}
        result = _merge_headers(auth, extra)

        assert result["Authorization"] == "Bearer auth-token"
        assert result["X-Custom"] == "value"


class TestMergeAdditionalParams:
    """Test additional parameter merging."""

    def test_no_auth_config(self):
        """Test with no auth config."""
        kwargs = {"model": "gpt-4"}
        _merge_additional_params(None, kwargs)

        assert kwargs == {"model": "gpt-4"}

    def test_no_additional_params(self):
        """Test with auth config but no additional params."""
        auth = BearerTokenAuth(token="token")
        kwargs = {"model": "gpt-4"}
        _merge_additional_params(auth, kwargs)

        assert kwargs == {"model": "gpt-4"}

    def test_merge_simple_params(self):
        """Test merging simple additional parameters."""
        auth = BearerTokenAuth(
            token="token", additional_params={"timeout": 60, "retries": 3}
        )
        kwargs = {"model": "gpt-4"}
        _merge_additional_params(auth, kwargs)

        assert kwargs["model"] == "gpt-4"
        assert kwargs["timeout"] == 60
        assert kwargs["retries"] == 3

    def test_merge_extra_headers_when_none_exist(self):
        """Test merging extra_headers when none exist in kwargs."""
        auth = BearerTokenAuth(
            token="token",
            additional_params={"extra_headers": {"X-Custom": "value"}},
        )
        kwargs = {"model": "gpt-4"}
        _merge_additional_params(auth, kwargs)

        assert kwargs["extra_headers"] == {"X-Custom": "value"}

    def test_merge_extra_headers_when_exist(self):
        """Test merging extra_headers when they exist in kwargs."""
        auth = BearerTokenAuth(
            token="token",
            additional_params={"extra_headers": {"X-Custom": "value"}},
        )
        kwargs = {"model": "gpt-4", "extra_headers": {"User-Agent": "test"}}
        _merge_additional_params(auth, kwargs)

        assert kwargs["extra_headers"]["X-Custom"] == "value"
        assert kwargs["extra_headers"]["User-Agent"] == "test"

    def test_merge_extra_headers_overwrite_existing(self):
        """Test that auth extra_headers override existing headers."""
        auth = BearerTokenAuth(
            token="token",
            additional_params={"extra_headers": {"X-Custom": "new-value"}},
        )
        kwargs = {"extra_headers": {"X-Custom": "old-value", "Other": "kept"}}
        _merge_additional_params(auth, kwargs)

        assert kwargs["extra_headers"]["X-Custom"] == "new-value"
        assert kwargs["extra_headers"]["Other"] == "kept"

    def test_merge_non_mapping_extra_headers_in_kwargs(self):
        """Test merging when existing extra_headers is not a mapping."""
        auth = BearerTokenAuth(
            token="token",
            additional_params={"extra_headers": {"X-Custom": "value"}},
        )
        kwargs = {"extra_headers": "not-a-dict"}
        _merge_additional_params(auth, kwargs)

        assert kwargs["extra_headers"] == {"X-Custom": "value"}


class TestBuildCompletionKwargs:
    """Test completion kwargs building."""

    def test_minimal_request_no_auth(self):
        """Test building kwargs from minimal request without auth."""
        req = CompletionRequest(model="gpt-4", messages=[{"role": "user", "content": "hi"}])
        kwargs = _build_completion_kwargs(req, None)

        assert kwargs["model"] == "gpt-4"
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["stream"] is False
        assert "extra_headers" not in kwargs

    def test_request_with_auth(self):
        """Test building kwargs with auth config."""
        req = CompletionRequest(model="gpt-4", messages=[])
        auth = BearerTokenAuth(token="secret")
        kwargs = _build_completion_kwargs(req, auth)

        assert kwargs["extra_headers"]["Authorization"] == "Bearer secret"

    def test_request_with_base_url(self):
        """Test that base_url is mapped to api_base."""
        req = CompletionRequest(
            model="gpt-4", messages=[], base_url="https://api.custom.com"
        )
        kwargs = _build_completion_kwargs(req, None)

        assert kwargs["api_base"] == "https://api.custom.com"
        assert "base_url" not in kwargs

    def test_request_with_api_base(self):
        """Test that api_base is preserved."""
        req = CompletionRequest(
            model="gpt-4", messages=[], api_base="https://api.custom.com"
        )
        kwargs = _build_completion_kwargs(req, None)

        assert kwargs["api_base"] == "https://api.custom.com"

    def test_auth_url_fallback(self):
        """Test that auth config URL is used as fallback for api_base."""
        req = CompletionRequest(model="gpt-4", messages=[])
        auth = BearerTokenAuth(token="secret", url="https://auth.api.com")
        kwargs = _build_completion_kwargs(req, auth)

        assert kwargs["api_base"] == "https://auth.api.com"

    def test_request_url_overrides_auth_url(self):
        """Test that request base_url overrides auth URL."""
        req = CompletionRequest(
            model="gpt-4", messages=[], base_url="https://request.api.com"
        )
        auth = BearerTokenAuth(token="secret", url="https://auth.api.com")
        kwargs = _build_completion_kwargs(req, auth)

        assert kwargs["api_base"] == "https://request.api.com"

    def test_stream_defaults_to_false(self):
        """Test that stream defaults to False."""
        req = CompletionRequest(model="gpt-4", messages=[])
        kwargs = _build_completion_kwargs(req, None)

        assert kwargs["stream"] is False

    def test_stream_preserved_when_true(self):
        """Test that stream is preserved when True."""
        req = CompletionRequest(model="gpt-4", messages=[], stream=True)
        kwargs = _build_completion_kwargs(req, None)

        assert kwargs["stream"] is True

    def test_none_values_excluded(self):
        """Test that None values are excluded from kwargs."""
        req = CompletionRequest(model="gpt-4", messages=[], temperature=None, max_tokens=100)
        kwargs = _build_completion_kwargs(req, None)

        assert "temperature" not in kwargs
        assert kwargs["max_tokens"] == 100

    def test_additional_params_merged(self):
        """Test that additional params from auth are merged."""
        req = CompletionRequest(model="gpt-4", messages=[])
        auth = BearerTokenAuth(
            token="secret", additional_params={"timeout": 120, "custom": "value"}
        )
        kwargs = _build_completion_kwargs(req, auth)

        assert kwargs["timeout"] == 120
        assert kwargs["custom"] == "value"


class TestCompletion:
    """Test synchronous completion function."""

    @patch("agentcicd_fixtures_aisystem.llm_completion.litellm.completion")
    def test_completion_basic(self, mock_litellm):
        """Test basic completion call."""
        mock_litellm.return_value = {
            "id": "resp-123",
            "model": "gpt-4",
            "choices": [{"index": 0, "message": {"content": "Hello!"}}],
        }

        req = CompletionRequest(model="gpt-4", messages=[{"role": "user", "content": "hi"}])
        response = completion(req)

        assert isinstance(response, CompletionResponse)
        assert response.id == "resp-123"
        mock_litellm.assert_called_once()

    @patch("agentcicd_fixtures_aisystem.llm_completion.litellm.completion")
    def test_completion_with_auth(self, mock_litellm):
        """Test completion with auth config."""
        mock_litellm.return_value = {"id": "resp-456"}

        req = CompletionRequest(model="gpt-4", messages=[])
        auth = ApiKeyAuth(api_key="key-123")
        response = completion(req, auth)

        assert isinstance(response, CompletionResponse)
        call_kwargs = mock_litellm.call_args[1]
        assert call_kwargs["extra_headers"]["X-API-Key"] == "key-123"


class TestAcompletion:
    """Test asynchronous completion function."""

    @pytest.mark.asyncio
    @patch("agentcicd_fixtures_aisystem.llm_completion.litellm.acompletion")
    async def test_acompletion_basic(self, mock_litellm):
        """Test basic async completion call."""
        mock_litellm.return_value = {
            "id": "resp-789",
            "model": "gpt-3.5-turbo",
            "choices": [{"index": 0, "message": {"content": "Async response"}}],
        }

        req = CompletionRequest(
            model="gpt-3.5-turbo", messages=[{"role": "user", "content": "test"}]
        )
        response = await acompletion(req)

        assert isinstance(response, CompletionResponse)
        assert response.id == "resp-789"
        mock_litellm.assert_called_once()

    @pytest.mark.asyncio
    @patch("agentcicd_fixtures_aisystem.llm_completion.litellm.acompletion")
    async def test_acompletion_with_auth(self, mock_litellm):
        """Test async completion with auth config."""
        mock_litellm.return_value = {"id": "resp-async"}

        req = CompletionRequest(model="gpt-4", messages=[])
        auth = BearerTokenAuth(token="async-token")
        response = await acompletion(req, auth)

        assert isinstance(response, CompletionResponse)
        call_kwargs = mock_litellm.call_args[1]
        assert call_kwargs["extra_headers"]["Authorization"] == "Bearer async-token"
