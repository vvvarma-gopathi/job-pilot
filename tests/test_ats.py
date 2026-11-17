import tempfile
import unittest
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter

from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.http import FetchError
from jobpilot_discovery.sources.ats import (
    ATSBoard,
    ATSBoardDiscovery,
    GreenhouseBoardAdapter,
    LeverBoardAdapter,
    discover_board_candidates,
)

URL_ADAPTER = TypeAdapter(HttpUrl)


class FixtureFetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_json(self, url, *, params=None):
        self.calls.append((url, params))
        return self.payloads[url]

    def get_text(self, url):
        return "User-agent: *\nAllow: /" if url.endswith("/robots.txt") else ""


class TransientFailureFetcher:
    def get_text(self, url):
        if url.endswith("/robots.txt"):
            return "User-agent: *\nAllow: /"
        return '<a href="https://boards.greenhouse.io/acme/jobs/123">Jobs</a>'

    def get_json(self, url):
        raise FetchError(f"temporary network failure: {url}")


class ATSAdapterTests(unittest.TestCase):
    def test_extracts_only_real_greenhouse_and_lever_board_links(self):
        boards = discover_board_candidates(
            [
                "https://boards.greenhouse.io/acme/jobs/123",
                "https://jobs.lever.co/example/abc",
                "https://example.com/careers",
                "https://boards.greenhouse.io/acme",
            ],
            company_name="Acme",
            career_page_url="https://acme.example/careers",
            discovery_source="talivy",
        )
        self.assertEqual(
            [(board.provider, board.board_identifier) for board in boards],
            [("greenhouse", "acme"), ("lever", "example")],
        )

    def test_greenhouse_normalizes_job_records(self):
        endpoint = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
        fetcher = FixtureFetcher({endpoint: {"jobs": [{
            "id": 123,
            "title": "Python Backend Developer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "content": "<p>Python FastAPI role</p>",
            "location": {"name": "Remote - India"},
            "updated_at": "2026-11-17T10:00:00Z",
            "departments": [{"name": "Engineering"}],
        }]}})
        board = ATSBoard(
            provider="greenhouse", board_identifier="acme", company_name="Acme",
            career_page_url=URL_ADAPTER.validate_python("https://acme.example/careers"),
        )
        adapter = GreenhouseBoardAdapter(fetcher)
        self.assertTrue(adapter.validate(board))
        jobs = adapter.fetch_jobs(board)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].description, "Python FastAPI role")
        self.assertEqual(jobs[0].tags, ["Engineering"])
        self.assertEqual(jobs[0].source_job_id, "123")

    def test_lever_normalizes_job_record_and_original_url(self):
        endpoint = "https://api.lever.co/v0/postings/example?mode=json"
        fetcher = FixtureFetcher({endpoint: [{
            "id": "l-1",
            "text": "Junior Python Developer",
            "hostedUrl": "https://jobs.lever.co/example/l-1",
            "applyUrl": "https://jobs.lever.co/example/l-1/apply",
            "descriptionPlain": "Python and REST APIs",
            "createdAt": 1794912000000,
            "categories": {"location": "India", "commitment": "Full-time", "team": "Platform"},
        }]})
        board = ATSBoard(
            provider="lever", board_identifier="example", company_name="Example",
            career_page_url=URL_ADAPTER.validate_python("https://example.com/careers"),
        )
        jobs = LeverBoardAdapter(fetcher).fetch_jobs(board)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(str(jobs[0].apply_url), "https://jobs.lever.co/example/l-1/apply")
        self.assertEqual(str(jobs[0].source_url), "https://jobs.lever.co/example/l-1")

    def test_discovered_boards_are_validated_and_persisted(self):
        endpoint = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
        fetcher = FixtureFetcher({endpoint: {"jobs": []}})
        with tempfile.TemporaryDirectory() as temp:
            repository = JobRepository(Path(temp) / "boards.db")
            discovery = ATSBoardDiscovery(repository, fetcher, "JobPilot test", request_delay_seconds=0)
            boards, failures = discovery.discover([(
                "Acme", "https://boards.greenhouse.io/acme/jobs/123", "talivy"
            )])
            self.assertEqual(failures, 0)
            self.assertEqual(len(boards), 1)
            persisted = repository.valid_ats_boards("greenhouse")
            self.assertEqual(persisted[0]["board_identifier"], "acme")
            self.assertEqual(repository.companies()[0]["name"], "Acme")

    def test_temporary_validation_failure_is_unverified_and_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = JobRepository(Path(temp) / "boards.db")
            discovery = ATSBoardDiscovery(
                repository, TransientFailureFetcher(), "JobPilot test",
                request_delay_seconds=0,
            )
            boards, failures = discovery.discover([(
                "Acme", "https://acme.example/careers", "talivy"
            )])

            self.assertEqual(boards, [])
            self.assertEqual(failures, 1)
            self.assertEqual(repository.company_boards()[0]["validation_status"], "unverified")
            self.assertEqual(repository.valid_ats_boards(), [])

    def test_expired_board_validation_refreshes_persisted_timestamp(self):
        endpoint = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
        fetcher = FixtureFetcher({endpoint: {"jobs": []}})
        with tempfile.TemporaryDirectory() as temp:
            repository = JobRepository(Path(temp) / "boards.db")
            repository.save_company_board(
                company_name="Acme",
                provider="greenhouse",
                board_identifier="acme",
                career_page_url="https://acme.example/careers",
                discovery_source="talivy",
                validation_status="valid",
                last_validated_at="2020-01-01T00:00:00+00:00",
            )
            discovery = ATSBoardDiscovery(repository, fetcher, "JobPilot test", request_delay_seconds=0)

            self.assertEqual(discovery.revalidate_expired(), 0)
            self.assertNotEqual(
                repository.company_boards()[0]["last_validated_at"],
                "2020-01-01T00:00:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
