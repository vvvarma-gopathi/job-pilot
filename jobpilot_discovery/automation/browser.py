from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from ..utils import is_public_https_url
from .exceptions import AutomationSafetyError


def validate_application_url(url: str, *, allow_local_urls: bool = False) -> None:
    parts = urlsplit(url)
    if allow_local_urls and parts.scheme in {"http", "https"} and parts.hostname:
        return
    if not is_public_https_url(url):
        raise AutomationSafetyError("Application navigation requires a public HTTPS URL")
    host = parts.hostname
    if host is None:
        raise AutomationSafetyError("Application URL has no hostname")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise AutomationSafetyError("Application hostname could not be verified") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise AutomationSafetyError("Application hostname resolves to a non-public network address")


class BrowserManager:
    def __init__(
        self,
        *,
        headless: bool = False,
        browser_channel: str | None = None,
        persistent_profile_path: Path | None = None,
        allow_local_urls: bool = False,
    ):
        self.headless = headless
        self.browser_channel = browser_channel
        self.persistent_profile_path = persistent_profile_path
        self.allow_local_urls = allow_local_urls
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.blocked_requests: list[str] = []
        self._validated_origins: set[str] = set()

    def __enter__(self) -> "BrowserManager":
        self._playwright = sync_playwright().start()
        try:
            chromium = self._playwright.chromium
            if self.persistent_profile_path is not None:
                self.persistent_profile_path.mkdir(parents=True, exist_ok=True)
                self._context = chromium.launch_persistent_context(
                    str(self.persistent_profile_path),
                    headless=self.headless,
                    channel=self.browser_channel,
                    accept_downloads=False,
                )
            else:
                self._browser = chromium.launch(
                    headless=self.headless,
                    channel=self.browser_channel,
                )
                self._context = self._browser.new_context(accept_downloads=False)
            self._context.route("**/*", self._guard_request)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("BrowserManager must be entered before creating a page")
        return self._context.new_page()

    def navigate(self, page: Page, url: str, *, timeout_ms: int = 30_000) -> None:
        validate_application_url(url, allow_local_urls=self.allow_local_urls)
        self.blocked_requests.clear()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if self.blocked_requests:
            raise AutomationSafetyError(
                "The page attempted to load a blocked or non-public resource"
            )

    def _guard_request(self, route) -> None:
        url = route.request.url
        try:
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in self._validated_origins:
                validate_application_url(url, allow_local_urls=self.allow_local_urls)
                self._validated_origins.add(origin)
        except AutomationSafetyError:
            self.blocked_requests.append(url)
            route.abort()
            return
        route.continue_()
