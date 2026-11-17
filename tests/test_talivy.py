import tempfile
import unittest
from pathlib import Path
from typing import Any

from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.sources.talivy import SearchQueryGenerator, TalivyAdapter


class FixtureTalivyClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, query, *, limit):
        self.calls.append((query, limit))
        return self.results


class FailingTalivyClient:
    def search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError(f"service unavailable for {query} (limit={limit})")


class TalivyAdapterTests(unittest.TestCase):
    def test_query_generation_covers_roles_locations_and_experience_terms(self):
        config = {
            "roles": [{"name": "Python Backend"}, {"name": "LLM Developer"}, {"name": "FastAPI Developer"}],
            "locations": ["India", "Remote"],
            "talivy": {"search_terms": ["fresher", "graduate", "entry-level", "0-1 years"]},
        }
        queries = SearchQueryGenerator(config, max_queries=8).generate()
        self.assertEqual(len(queries), len(set(query.casefold() for query in queries)))
        self.assertTrue(any("Remote" in query for query in queries))
        self.assertTrue(any("graduate" in query for query in queries))
        self.assertTrue(any("LLM Developer" in query for query in queries))

    def test_search_cache_avoids_repeating_identical_query(self):
        config = {
            "roles": [{"name": "Python Backend Developer"}],
            "locations": ["India"],
            "talivy": {"search_terms": ["fresher"]},
        }
        client = FixtureTalivyClient([{
            "result_type": "job",
            "url": "https://jobs.example.com/python",
            "title": "Python Backend Developer",
            "company": "Example",
            "description": "<p>Python FastAPI</p>",
            "description_complete": True,
        }])
        with tempfile.TemporaryDirectory() as temp:
            repository = JobRepository(Path(temp) / "talivy.db")
            adapter = TalivyAdapter(
                client, repository, config, max_queries=1, request_delay_seconds=0
            )
            first = adapter.fetch_jobs()
            second = adapter.fetch_jobs()
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(len(client.calls), 1)

    def test_search_snippet_is_never_used_as_complete_job_description(self):
        config = {
            "roles": [{"name": "Python Developer"}],
            "locations": ["India"],
            "talivy": {"search_terms": ["fresher"]},
        }
        client = FixtureTalivyClient([{
            "result_type": "job",
            "url": "https://jobs.example.com/python",
            "title": "Python Developer",
            "company": "Example",
            "snippet": "A short search result",
            "description_complete": False,
        }])
        with tempfile.TemporaryDirectory() as temp:
            repository = JobRepository(Path(temp) / "talivy.db")
            adapter = TalivyAdapter(
                client, repository, config, max_queries=1, request_delay_seconds=0
            )
            self.assertEqual(adapter.fetch_jobs(), [])

    def test_search_failure_is_counted_without_aborting_discovery(self):
        config = {
            "roles": [{"name": "Python Developer"}],
            "locations": ["India"],
            "talivy": {"search_terms": ["fresher"]},
        }
        with tempfile.TemporaryDirectory() as temp:
            repository = JobRepository(Path(temp) / "talivy.db")
            adapter = TalivyAdapter(
                FailingTalivyClient(), repository, config,
                max_queries=1, request_delay_seconds=0,
            )

            self.assertEqual(adapter.fetch_jobs(), [])
            self.assertEqual(adapter.failed_queries, 1)


if __name__ == "__main__":
    unittest.main()
