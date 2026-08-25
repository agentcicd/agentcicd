from __future__ import annotations

from agentcicd.fixtures.environments.browser.environment import BrowserEnvironment, BrowserSession
from agentcicd.fixtures.environments.browser.playwright_adapter import PlaywrightBrowserEnvironment
from agentcicd.fixtures.environments.browser.types import (
    BrowserObservation,
    BrowserObservationField,
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
    Viewport,
    WaitUntil,
)

__all__ = [
    "BrowserEnvironment",
    "BrowserObservation",
    "BrowserObservationField",
    "BrowserObservationRequest",
    "BrowserCookie",
    "BrowserDomSummary",
    "BrowserPolicy",
    "BrowserScreenshot",
    "BrowserSession",
    "BrowserSetupSpec",
    "BrowserStep",
    "BrowserStorageSummary",
    "BrowserTeardownResult",
    "ConsoleMessage",
    "DownloadRecord",
    "MouseButton",
    "PlaywrightBrowserEnvironment",
    "Viewport",
    "WaitUntil",
]
