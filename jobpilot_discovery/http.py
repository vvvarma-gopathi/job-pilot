from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import Settings


class FetchError(RuntimeError):
    pass


class HttpFetcher:
    """Small synchronous HTTP client with bounded retries and backoff."""

    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self._get(url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"Expected JSON from {response.url}, received invalid JSON") from exc

    def get_text(self, url: str) -> str:
        return self._get(url).text

    def _get(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.client.get(url, params=params)
                if response.status_code in self.RETRYABLE_STATUS:
                    if attempt >= self.settings.max_retries:
                        response.raise_for_status()
                    retry_after = response.headers.get("Retry-After")
                    delay = self._retry_after_seconds(retry_after)
                    if delay is None:
                        delay = min(2 ** attempt, 20)
                    delay = min(delay, 60)
                    time.sleep(delay + random.uniform(0, 0.25))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in self.RETRYABLE_STATUS:
                    break
                if attempt < self.settings.max_retries:
                    time.sleep(min(2 ** attempt, 20) + random.uniform(0, 0.25))
        raise FetchError(f"GET failed for {url}: {last_error}") from last_error

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None
