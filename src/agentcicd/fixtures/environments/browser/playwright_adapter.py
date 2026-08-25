from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentcicd.fixtures.environments.browser.types import (
    BrowserObservation,
    BrowserObservationRequest,
    BrowserCookie,
    BrowserDomSummary,
    BrowserPolicy,
    BrowserScreenshot,
    BrowserSetupSpec,
    BrowserStep,
    BrowserStorageSummary,
    BrowserTeardownResult,
    ConsoleMessage,
    DownloadRecord,
    MouseButton,
    WaitUntil,
)
from agentcicd.fixtures.environments.core.errors import EnvironmentUnavailable, PolicyViolation, error_info
from agentcicd.fixtures.environments.core.lifecycle import TeardownReason


class PlaywrightBrowserEnvironment:
    async def setup(self, spec: BrowserSetupSpec) -> "PlaywrightBrowserSession":
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise EnvironmentUnavailable("playwright is not installed; install agentcicd.fixtures[browser]") from exc
        _enforce_origin(spec.start_url, spec.policy)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": spec.viewport.width, "height": spec.viewport.height},
            locale=spec.locale,
            timezone_id=spec.timezone_id,
            storage_state=spec.storage_state_path,
            accept_downloads=spec.policy.allow_downloads,
        )
        context.set_default_navigation_timeout(spec.policy.navigation_timeout_ms)
        context.set_default_timeout(spec.policy.action_timeout_ms)
        page = await context.new_page()
        session = PlaywrightBrowserSession(
            env_id=spec.env_id,
            session_id=spec.session_id,
            policy=spec.policy,
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            width=spec.viewport.width,
            height=spec.viewport.height,
        )
        await session.navigate(spec.start_url)
        return session


class PlaywrightBrowserSession:
    def __init__(
        self,
        *,
        env_id: str,
        session_id: str,
        policy: BrowserPolicy,
        playwright: Any,
        browser: Any,
        context: Any,
        page: Any,
        width: int,
        height: int,
    ) -> None:
        self.env_id = env_id
        self.session_id = session_id
        self.policy = policy
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._width = width
        self._height = height
        self._console: list[ConsoleMessage] = []
        self._downloads: list[DownloadRecord] = []
        page.on("console", self._on_console)
        page.on("download", self._on_download)

    async def teardown(self, reason: TeardownReason) -> BrowserTeardownResult:
        await self._context.close()
        await self._browser.close()
        await self._playwright.stop()
        return BrowserTeardownResult(session_id=self.session_id, env_id=self.env_id, ok=True, reason=reason)

    async def observe(self, request: BrowserObservationRequest) -> BrowserObservation:
        title: str | None = None
        visible_text: str | None = None
        dom_summary: BrowserDomSummary | None = None
        storage: BrowserStorageSummary | None = None
        if "title" in request.include:
            title = await self._page.title()
        if "visible_text" in request.include:
            text = await self._page.locator("body").inner_text(timeout=self.policy.action_timeout_ms)
            visible_text = text[: request.max_text_chars]
        if "dom_summary" in request.include:
            raw_summary = await self._page.evaluate(
                """() => ({
                    forms: document.forms.length,
                    links: document.links.length,
                    buttons: document.querySelectorAll('button,input[type=button],input[type=submit]').length,
                    inputs: document.querySelectorAll('input,textarea,select').length
                })"""
            )
            dom_summary = BrowserDomSummary(
                forms=int(raw_summary["forms"]),
                links=int(raw_summary["links"]),
                buttons=int(raw_summary["buttons"]),
                inputs=int(raw_summary["inputs"]),
            )
        if "storage" in request.include:
            storage = await self._storage_summary()
        return BrowserObservation(
            url=self._page.url,
            title=title,
            visible_text=visible_text,
            accessibility_tree=None,
            dom_summary=dom_summary,
            console=tuple(self._console) if "console" in request.include else (),
            downloads=tuple(self._downloads) if "downloads" in request.include else (),
            storage=storage,
        )

    async def navigate(self, url: str, *, wait_until: WaitUntil = "load") -> BrowserStep:
        try:
            _enforce_origin(url, self.policy)
            await self._page.goto(url, wait_until=wait_until)
            return self._step(True, f"navigated to {url}")
        except Exception as exc:
            return self._step(False, "navigation failed", exc)

    async def click(self, selector: str, *, button: MouseButton = "left", click_count: int = 1) -> BrowserStep:
        return await self._action(f"clicked {selector}", lambda: self._page.click(selector, button=button, click_count=click_count))

    async def fill(self, selector: str, value: str) -> BrowserStep:
        return await self._action(f"filled {selector}", lambda: self._page.fill(selector, value))

    async def select(self, selector: str, values: Sequence[str]) -> BrowserStep:
        return await self._action(f"selected {selector}", lambda: self._page.select_option(selector, list(values)))

    async def press(self, key: str, *, selector: str | None = None) -> BrowserStep:
        if selector is None:
            return await self._action(f"pressed {key}", lambda: self._page.keyboard.press(key))
        return await self._action(f"pressed {key} in {selector}", lambda: self._page.press(selector, key))

    async def hover(self, selector: str) -> BrowserStep:
        return await self._action(f"hovered {selector}", lambda: self._page.hover(selector))

    async def wait_for(
        self,
        *,
        selector: str | None = None,
        text: str | None = None,
        url_pattern: str | None = None,
        timeout_ms: int | None = None,
    ) -> BrowserStep:
        try:
            timeout = timeout_ms or self.policy.action_timeout_ms
            if selector is not None:
                await self._page.wait_for_selector(selector, timeout=timeout)
            elif text is not None:
                await self._page.get_by_text(text).wait_for(timeout=timeout)
            elif url_pattern is not None:
                await self._page.wait_for_url(url_pattern, timeout=timeout)
            else:
                raise PolicyViolation("wait_for requires selector, text, or url_pattern")
            return self._step(True, "wait condition satisfied")
        except Exception as exc:
            return self._step(False, "wait failed", exc)

    async def upload_file(self, selector: str, paths: Sequence[str]) -> BrowserStep:
        if not self.policy.allow_uploads:
            return self._step(False, "upload denied by policy", PolicyViolation("uploads denied by policy"))
        return await self._action(f"uploaded {len(paths)} file(s)", lambda: self._page.set_input_files(selector, list(paths)))

    async def screenshot(self, *, full_page: bool = False) -> BrowserScreenshot:
        path = Path(tempfile.gettempdir()) / f"{self.session_id}-screenshot.png"
        await self._page.screenshot(path=str(path), full_page=full_page)
        return BrowserScreenshot(path=str(path), width=self._width, height=self._height)

    async def finish(self, reason: str | None = None) -> BrowserStep:
        summary = "finished" if reason is None else f"finished: {reason}"
        return self._step(True, summary)

    async def _action(self, summary: str, call: Callable[[], Awaitable[Any]]) -> BrowserStep:
        try:
            await call()
            _enforce_origin(self._page.url, self.policy)
            return self._step(True, summary)
        except Exception as exc:
            return self._step(False, f"{summary} failed", exc)

    async def _storage_summary(self) -> BrowserStorageSummary:
        raw_cookies = await self._context.cookies()
        local_keys = await self._page.evaluate("() => Object.keys(window.localStorage)")
        session_keys = await self._page.evaluate("() => Object.keys(window.sessionStorage)")
        cookies: list[BrowserCookie] = []
        for cookie in raw_cookies:
            cookies.append(
                BrowserCookie(
                    name=str(cookie["name"]),
                    value=str(cookie["value"]),
                    domain=str(cookie["domain"]),
                    path=str(cookie["path"]),
                    expires=cookie.get("expires"),
                    http_only=cookie.get("httpOnly"),
                    secure=cookie.get("secure"),
                    same_site=cookie.get("sameSite"),
                )
            )
        return BrowserStorageSummary(
            cookies=tuple(cookies),
            local_storage_keys=tuple(str(item) for item in local_keys),
            session_storage_keys=tuple(str(item) for item in session_keys),
        )

    def _step(self, ok: bool, summary: str, exc: Exception | None = None) -> BrowserStep:
        return BrowserStep(
            ok=ok,
            summary=summary,
            url=self._page.url,
            error=error_info(exc) if exc is not None else None,
        )

    def _on_console(self, message: Any) -> None:
        location = message.location
        self._console.append(
            ConsoleMessage(
                level=message.type,
                text=message.text,
                location_url=location.get("url"),
                line_number=location.get("lineNumber"),
                column_number=location.get("columnNumber"),
            )
        )

    async def _on_download(self, download: Any) -> None:
        if not self.policy.allow_downloads:
            await download.cancel()
            return
        path = await download.path()
        self._downloads.append(
            DownloadRecord(
                suggested_filename=download.suggested_filename,
                path=path,
                url=download.url,
            )
        )


def _enforce_origin(url: str, policy: BrowserPolicy) -> None:
    if url == "about:blank":
        return
    origin = _origin(url)
    for blocked in policy.blocked_origins:
        if origin == blocked:
            raise PolicyViolation(f"origin blocked by policy: {origin}")
    if policy.allowed_origins:
        for allowed in policy.allowed_origins:
            if origin == allowed:
                return
        raise PolicyViolation(f"origin not allowed by policy: {origin}")


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise PolicyViolation(f"absolute URL is required: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"
