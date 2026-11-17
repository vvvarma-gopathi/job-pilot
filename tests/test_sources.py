import unittest
from typing import Any

from jobpilot_discovery.sources.arbeitnow import ArbeitnowSource
from jobpilot_discovery.sources.remotive import RemotiveSource


class FakeFetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((url, params))
        if params and "page" in params:
            return self.payloads[params["page"]]
        return self.payloads


class SourceTests(unittest.TestCase):
    def test_remotive_normalizes_remote_job(self):
        fetcher = FakeFetcher({"jobs": [{
            "id": 10, "title": "Python Backend Developer", "company_name": "Acme",
            "candidate_required_location": "Worldwide", "description": "Python and FastAPI",
            "url": "https://remotive.com/remote-jobs/software-dev/python-backend-10",
            "publication_date": "2026-11-16T10:00:00", "job_type": "full_time"
        }]})
        jobs = RemotiveSource(fetcher).fetch_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Acme")
        self.assertEqual(jobs[0].workplace_type, "remote")
        self.assertEqual(jobs[0].source, "remotive")

    def test_remotive_skips_missing_invalid_and_duplicate_records(self):
        fetcher = FakeFetcher({"jobs": [
            {"id": 10, "title": "Python Developer", "company_name": "Acme",
             "url": "https://remotive.com/job/10?utm_source=first", "description": "<p>Python</p>"},
            {"id": 11, "title": "Missing URL", "company_name": "Acme"},
            {"id": 12, "title": "Invalid URL", "company_name": "Acme", "url": "not a URL"},
            {"id": 13, "title": "Python Developer", "company_name": "Acme",
             "url": "https://remotive.com/job/10", "description": "duplicate"},
        ]})
        jobs = RemotiveSource(fetcher).fetch_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].description, "Python")

    def test_arbeitnow_stops_when_no_next_page(self):
        fetcher = FakeFetcher({1: {
            "data": [{"slug": "python-acme", "title": "Python Developer", "company_name": "Acme",
                      "location": "Remote", "description": "Python", "url": "https://example.com/job"}],
            "links": {"next": None}
        }})
        jobs = ArbeitnowSource(fetcher, max_pages=5).fetch_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(jobs[0].source_job_id, "python-acme")

    def test_arbeitnow_follows_pages_and_deduplicates_urls(self):
        fetcher = FakeFetcher({
            1: {
                "data": [{"slug": "one", "title": "Python Developer", "company_name": "Acme",
                          "location": "Remote", "description": "Python", "url": "https://example.com/job/1"}],
                "links": {"next": "page-2"},
            },
            2: {
                "data": [
                    {"slug": "duplicate", "title": "Python Developer", "company_name": "Acme",
                     "location": "Remote", "description": "Python", "url": "https://example.com/job/1"},
                    {"slug": "two", "title": "Python Developer", "company_name": "Acme",
                     "location": "Remote", "description": "FastAPI", "url": "https://example.com/job/2"},
                ],
                "links": {"next": None},
            },
        })
        jobs = ArbeitnowSource(fetcher, max_pages=5).fetch_jobs()
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(len(jobs), 2)


if __name__ == "__main__":
    unittest.main()
