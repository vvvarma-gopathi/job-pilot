import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jobpilot_discovery.cli import main
from jobpilot_discovery.config import Settings
from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.models import Job
from pydantic import HttpUrl, TypeAdapter

URL_ADAPTER = TypeAdapter(HttpUrl)


class ExtendedCLITests(unittest.TestCase):
    def test_sources_reports_disabled_talivy_and_ats_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(db_path=Path(temp) / "jobs.db")
            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "sources"]), redirect_stdout(output):
                main()
            text = output.getvalue()
            self.assertIn("remotive: enabled", text)
            self.assertIn("greenhouse: waiting for validated boards", text)
            self.assertIn("talivy: disabled", text)

    def test_companies_and_discovery_stats_report_empty_state(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(db_path=Path(temp) / "jobs.db")
            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "companies"]), redirect_stdout(output):
                main()
            self.assertIn("No companies discovered yet.", output.getvalue())

            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "discovery-stats"]), redirect_stdout(output):
                main()
            self.assertIn("No discovery runs recorded yet.", output.getvalue())

    def test_application_add_list_show_and_stats_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(db_path=Path(temp) / "jobs.db")
            repository = JobRepository(settings.db_path)
            repository.save_jobs([Job(
                source="test",
                title="Python Developer",
                company="Acme",
                location="India",
                description="Python",
                apply_url=URL_ADAPTER.validate_python("https://careers.example.com/jobs/1"),
            )])

            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "application", "add", "--job-id", "1"]), \
                    redirect_stdout(output):
                main()
            self.assertIn("Created application #1", output.getvalue())

            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "applications", "--status", "shortlisted"]), \
                    redirect_stdout(output):
                main()
            self.assertIn("Python Developer | Acme", output.getvalue())

            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "application", "show", "--id", "1"]), \
                    redirect_stdout(output):
                main()
            self.assertIn("Application #1: Python Developer | Acme", output.getvalue())

            output = io.StringIO()
            with patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings), \
                    patch("sys.argv", ["main.py", "application", "stats"]), redirect_stdout(output):
                main()
            self.assertIn("shortlisted: 1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
