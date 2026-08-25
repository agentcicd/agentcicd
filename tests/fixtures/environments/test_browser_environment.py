from __future__ import annotations

import pytest

from agentcicd.fixtures.environments.browser.playwright_adapter import _enforce_origin
from agentcicd.fixtures.environments.browser.types import BrowserObservationRequest, BrowserPolicy, BrowserSetupSpec
from agentcicd.fixtures.environments.core.errors import PolicyViolation
from agentcicd.fixtures.environments.testing.fakes import FakeBrowserEnvironment


def test_browser_origin_policy_allows_and_blocks() -> None:
    policy = BrowserPolicy(allowed_origins=("https://example.com",), blocked_origins=("https://blocked.test",))

    _enforce_origin("https://example.com/path", policy)
    with pytest.raises(PolicyViolation, match="blocked"):
        _enforce_origin("https://blocked.test/path", policy)
    with pytest.raises(PolicyViolation, match="not allowed"):
        _enforce_origin("https://other.test/path", policy)


@pytest.mark.asyncio
async def test_fake_browser_exercises_typed_session_api() -> None:
    session = await FakeBrowserEnvironment().setup(
        BrowserSetupSpec(env_id="env.browser", session_id="session.browser", start_url="https://example.com")
    )

    await session.navigate("https://example.com/form")
    await session.fill("#name", "AgentCICD")
    await session.click("button[type=submit]")
    observation = await session.observe(BrowserObservationRequest(include=("url", "title", "visible_text")))

    assert observation.url == "https://example.com/form"
    assert observation.title == "Fake Browser"
    assert observation.visible_text is not None
    assert "fill:#name:AgentCICD" in observation.visible_text
