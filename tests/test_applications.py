import csv
import tempfile
import unittest
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter

from jobpilot_discovery.cli import _export_applications
from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.models import Job


URL_ADAPTER = TypeAdapter(HttpUrl)


class ApplicationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = JobRepository(Path(self.temp.name) / "jobs.db")

    def add_job(self, *, title="Python Developer", company="Acme", location="India", url):
        job = Job(
            source="test",
            source_job_id=f"{title}-{location}",
            title=title,
            company=company,
            location=location,
            description="Python role",
            apply_url=URL_ADAPTER.validate_python(url),
        )
        self.repo.save_jobs([job])

    def test_application_creation_is_idempotent_for_same_job_and_url(self):
        self.add_job(url="https://careers.example.com/jobs/1?utm_source=feed")
        job_id = self.repo.recent_jobs()[0]["id"]

        first = self.repo.create_application(job_id)
        second = self.repo.create_application(job_id)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["application_id"], second["application_id"])
        self.assertEqual(len(self.repo.applications()), 1)

    def test_possible_company_title_duplicate_requires_explicit_confirmation(self):
        self.add_job(location="India", url="https://careers.example.com/jobs/1")
        first_job_id = self.repo.recent_jobs()[0]["id"]
        first = self.repo.create_application(first_job_id)

        self.add_job(location="Hyderabad, India", url="https://careers.example.com/jobs/2")
        second_job_id = next(
            job["id"] for job in self.repo.recent_jobs()
            if job["apply_url"].endswith("/jobs/2")
        )
        blocked = self.repo.create_application(second_job_id)
        confirmed = self.repo.create_application(
            second_job_id, confirm_possible_duplicate=True
        )

        self.assertTrue(first["created"])
        self.assertEqual(blocked["possible_duplicate_ids"], [first["application_id"]])
        self.assertFalse(blocked["created"])
        self.assertTrue(confirmed["created"])

    def test_submission_requires_confirmation_and_preserves_verified_state(self):
        self.add_job(url="https://careers.example.com/jobs/1")
        job_id = self.repo.recent_jobs()[0]["id"]
        application_id = self.repo.create_application(job_id)["application_id"]
        self.repo.update_application_status(application_id, "pending_review")
        self.repo.update_application_status(application_id, "applying")

        with self.assertRaises(ValueError):
            self.repo.update_application_status(application_id, "submitted")

        self.repo.update_application_status(
            application_id,
            "submitted",
            submission_verified=True,
            submission_reference="REF-1",
        )
        self.repo.update_application_status(application_id, "interview")
        application = self.repo.application(application_id)

        assert application is not None
        self.assertTrue(application["submission_verified"])
        self.assertEqual(application["submission_reference"], "REF-1")
        self.assertEqual(application["application_status"], "interview")

    def test_failed_retry_returns_to_review_without_submitting(self):
        self.add_job(url="https://careers.example.com/jobs/1")
        job_id = self.repo.recent_jobs()[0]["id"]
        application_id = self.repo.create_application(job_id)["application_id"]
        self.repo.update_application_status(application_id, "pending_review")
        self.repo.update_application_status(application_id, "failed", error_message="Before submit")

        self.repo.retry_application(application_id)

        application = self.repo.application(application_id)
        assert application is not None
        self.assertEqual(application["application_status"], "pending_review")
        self.assertFalse(application["submission_verified"])

    def test_csv_export_escapes_spreadsheet_formulas(self):
        self.add_job(company="=SUM(1,1)", url="https://careers.example.com/jobs/1")
        job_id = self.repo.recent_jobs()[0]["id"]
        self.repo.create_application(job_id)
        output = Path(self.temp.name) / "applications.csv"

        count = _export_applications(self.repo, output)

        with output.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(count, 1)
        self.assertEqual(row["company_name"], "'=SUM(1,1)")


if __name__ == "__main__":
    unittest.main()
