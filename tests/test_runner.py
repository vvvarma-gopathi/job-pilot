import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import HttpUrl, TypeAdapter

from jobpilot_discovery.config import Settings
from jobpilot_discovery.models import Job
from jobpilot_discovery.runner import run_discovery


class RunnerTests(unittest.TestCase):
    def test_source_failure_does_not_prevent_other_sources(self):
        config = {
            "roles": [{"name": "Python Backend Developer", "aliases": ["python backend"]}],
            "skills": ["python", "fastapi"],
            "locations": ["india", "remote", "hyderabad"],
            "exclude_title_terms": [],
            "exclude_description_terms": [],
            "allow_unknown_experience": True,
        }
        good_job = Job(
            source="remotive", title="Python Backend Developer", company="Acme",
            location="Hyderabad, India", description="Freshers welcome. Python and FastAPI.",
            apply_url=TypeAdapter(HttpUrl).validate_python("https://example.com/job/1"),
        )

        class FakeSource:
            def __init__(self, fetcher, *args):
                self.name = "fake"
                self.fetcher = fetcher
                self.args = args

            def fetch_jobs(self):
                if self.name == "arbeitnow":
                    raise RuntimeError("source is down")
                return [good_job]

        class FakeFetcher:
            def __init__(self, settings):
                self.settings = settings

            def close(self):
                pass

        class FailingSource(FakeSource):
            def __init__(self, fetcher, *args):
                super().__init__(fetcher, *args)
                self.name = "arbeitnow"

        class WorkingSource(FakeSource):
            def __init__(self, fetcher):
                super().__init__(fetcher)
                self.name = "remotive"

        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(db_path=Path(temp) / "jobs.db")
            with patch("jobpilot_discovery.runner.Settings.from_env", return_value=settings), \
                    patch("jobpilot_discovery.runner.load_search_config", return_value=config), \
                    patch("jobpilot_discovery.runner.HttpFetcher", FakeFetcher), \
                    patch("jobpilot_discovery.runner.ArbeitnowSource", FailingSource), \
                    patch("jobpilot_discovery.runner.RemotiveSource", WorkingSource), \
                    patch("jobpilot_discovery.runner._remotive_is_due", return_value=True):
                results = run_discovery()
            self.assertTrue(results[0].failed)
            self.assertFalse(results[1].failed)
            self.assertEqual(results[1].inserted, 1)


if __name__ == "__main__":
    unittest.main()
