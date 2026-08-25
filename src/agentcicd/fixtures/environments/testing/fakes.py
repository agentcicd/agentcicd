from __future__ import annotations

from collections.abc import Sequence

from agentcicd.fixtures.environments.browser.types import (
    BrowserObservation,
    BrowserObservationRequest,
    BrowserScreenshot,
    BrowserSetupSpec,
    BrowserStep,
    BrowserTeardownResult,
    MouseButton,
    WaitUntil,
)
from agentcicd.fixtures.environments.core.lifecycle import TeardownReason


class FakeBrowserEnvironment:
    async def setup(self, spec: BrowserSetupSpec) -> "FakeBrowserSession":
        return FakeBrowserSession(spec.env_id, spec.session_id, spec.start_url)


class FakeBrowserSession:
    def __init__(self, env_id: str, session_id: str, url: str) -> None:
        self.env_id = env_id
        self.session_id = session_id
        self.url = url
        self.actions: list[str] = []

    async def teardown(self, reason: TeardownReason) -> BrowserTeardownResult:
        return BrowserTeardownResult(session_id=self.session_id, env_id=self.env_id, ok=True, reason=reason)

    async def observe(self, request: BrowserObservationRequest) -> BrowserObservation:
        return BrowserObservation(url=self.url, title="Fake Browser", visible_text=" ".join(self.actions))

    async def navigate(self, url: str, *, wait_until: WaitUntil = "load") -> BrowserStep:
        self.url = url
        self.actions.append(f"navigate:{url}:{wait_until}")
        return BrowserStep(ok=True, summary=f"navigated to {url}", url=self.url)

    async def click(self, selector: str, *, button: MouseButton = "left", click_count: int = 1) -> BrowserStep:
        self.actions.append(f"click:{selector}:{button}:{click_count}")
        return BrowserStep(ok=True, summary=f"clicked {selector}", url=self.url)

    async def fill(self, selector: str, value: str) -> BrowserStep:
        self.actions.append(f"fill:{selector}:{value}")
        return BrowserStep(ok=True, summary=f"filled {selector}", url=self.url)

    async def select(self, selector: str, values: Sequence[str]) -> BrowserStep:
        self.actions.append(f"select:{selector}:{','.join(values)}")
        return BrowserStep(ok=True, summary=f"selected {selector}", url=self.url)

    async def press(self, key: str, *, selector: str | None = None) -> BrowserStep:
        self.actions.append(f"press:{selector or ''}:{key}")
        return BrowserStep(ok=True, summary=f"pressed {key}", url=self.url)

    async def hover(self, selector: str) -> BrowserStep:
        self.actions.append(f"hover:{selector}")
        return BrowserStep(ok=True, summary=f"hovered {selector}", url=self.url)

    async def wait_for(
        self,
        *,
        selector: str | None = None,
        text: str | None = None,
        url_pattern: str | None = None,
        timeout_ms: int | None = None,
    ) -> BrowserStep:
        self.actions.append(f"wait:{selector or text or url_pattern or ''}")
        return BrowserStep(ok=True, summary="wait condition satisfied", url=self.url)

    async def upload_file(self, selector: str, paths: Sequence[str]) -> BrowserStep:
        self.actions.append(f"upload:{selector}:{len(paths)}")
        return BrowserStep(ok=True, summary="uploaded file(s)", url=self.url)

    async def screenshot(self, *, full_page: bool = False) -> BrowserScreenshot:
        return BrowserScreenshot(path=f"/tmp/{self.session_id}.png")

    async def finish(self, reason: str | None = None) -> BrowserStep:
        return BrowserStep(ok=True, summary=reason or "finished", url=self.url)
