from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from agentcicd.fixtures.environments.core.errors import EnvironmentErrorInfo
from agentcicd.fixtures.environments.core.lifecycle import BaseEnvironmentSetupSpec, TeardownResult


WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
MouseButton = Literal["left", "right", "middle"]


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int


@dataclass(frozen=True)
class BrowserPolicy:
    allowed_origins: tuple[str, ...] = ()
    blocked_origins: tuple[str, ...] = ()
    allow_downloads: bool = True
    allow_uploads: bool = False
    allow_clipboard: bool = False
    allow_geolocation: bool = False
    navigation_timeout_ms: int = 30_000
    action_timeout_ms: int = 10_000


@dataclass(frozen=True)
class BrowserSetupSpec(BaseEnvironmentSetupSpec):
    start_url: str = "about:blank"
    viewport: Viewport = field(default_factory=lambda: Viewport(width=1280, height=720))
    locale: str | None = None
    timezone_id: str | None = None
    storage_state_path: str | None = None
    policy: BrowserPolicy = field(default_factory=BrowserPolicy)


BrowserObservationField = Literal[
    "url",
    "title",
    "visible_text",
    "accessibility_tree",
    "dom_summary",
    "console",
    "downloads",
    "storage",
]


@dataclass(frozen=True)
class BrowserObservationRequest:
    include: tuple[BrowserObservationField, ...] = (
        "url",
        "title",
        "visible_text",
        "accessibility_tree",
    )
    max_text_chars: int = 20_000


@dataclass(frozen=True)
class ConsoleMessage:
    level: str
    text: str
    location_url: str | None = None
    line_number: int | None = None
    column_number: int | None = None


@dataclass(frozen=True)
class DownloadRecord:
    suggested_filename: str
    path: str | None
    url: str


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str
    expires: float | int | None = None
    http_only: bool | None = None
    secure: bool | None = None
    same_site: str | None = None


@dataclass(frozen=True)
class BrowserDomSummary:
    forms: int
    links: int
    buttons: int
    inputs: int


@dataclass(frozen=True)
class BrowserStorageSummary:
    cookies: tuple[BrowserCookie, ...] = ()
    local_storage_keys: tuple[str, ...] = ()
    session_storage_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserObservation:
    url: str
    title: str | None
    visible_text: str | None = None
    accessibility_tree: Mapping[str, Any] | None = None
    dom_summary: BrowserDomSummary | None = None
    console: tuple[ConsoleMessage, ...] = ()
    downloads: tuple[DownloadRecord, ...] = ()
    storage: BrowserStorageSummary | None = None


@dataclass(frozen=True)
class BrowserStep:
    ok: bool
    summary: str
    url: str | None = None
    error: EnvironmentErrorInfo | None = None


@dataclass(frozen=True)
class BrowserScreenshot:
    path: str
    media_type: str = "image/png"
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class BrowserTeardownResult(TeardownResult):
    pass
