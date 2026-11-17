import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from jobpilot_discovery.config import Settings
from jobpilot_discovery.http import FetchError, HttpFetcher


class HttpFetcherTests(unittest.TestCase):
    def test_retries_rate_limit_and_honors_retry_after(self):
        settings = Settings(db_path=Path("test.db"), max_retries=1)
        fetcher = HttpFetcher(settings)
        request = httpx.Request("GET", "https://example.com/feed")
        limited = httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        success = httpx.Response(200, json={"ok": True}, request=request)
        with patch.object(fetcher.client, "get", side_effect=[limited, success]), patch(
            "jobpilot_discovery.http.time.sleep"
        ) as sleep:
            self.assertEqual(fetcher.get_json(str(request.url)), {"ok": True})
            sleep.assert_called_once()
        fetcher.close()

    def test_retries_exhausted_raise_fetch_error(self):
        settings = Settings(db_path=Path("test.db"), max_retries=1)
        fetcher = HttpFetcher(settings)
        request = httpx.Request("GET", "https://example.com/feed")
        responses = [
            httpx.Response(503, request=request),
            httpx.Response(503, request=request),
        ]
        with patch.object(fetcher.client, "get", side_effect=responses), patch(
            "jobpilot_discovery.http.time.sleep"
        ):
            with self.assertRaises(FetchError):
                fetcher.get_json(str(request.url))
        fetcher.close()

    def test_retry_after_supports_http_date(self):
        seconds = HttpFetcher._retry_after_seconds("Tue, 17 Nov 2026 12:00:00 GMT")
        self.assertIsNotNone(seconds)
        if seconds is not None:
            self.assertGreaterEqual(seconds, 0)


if __name__ == "__main__":
    unittest.main()
