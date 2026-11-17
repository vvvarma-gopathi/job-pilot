import tempfile
import sqlite3
import unittest
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter

from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.matcher import JobMatcher
from jobpilot_discovery.models import Job


CONFIG = {
    "roles": [{"name": "Python Backend Developer", "aliases": ["python backend", "python developer", "fastapi developer"]}],
    "skills": ["python", "fastapi", "postgresql", "docker"],
    "locations": ["india", "remote", "hyderabad", "bangalore"],
    "exclude_title_terms": ["senior", "lead", "principal"],
    "exclude_description_terms": ["unpaid internship"],
    "suspicious_url_hosts": ["bit.ly"],
    "allow_unknown_experience": True,
}
URL_ADAPTER = TypeAdapter(HttpUrl)


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.matcher = JobMatcher(CONFIG, minimum_score=40)

    def make_job(self, title, description, location="Hyderabad, India", workplace=None):
        return Job(source="test", title=title, company="Acme", location=location,
                   description=description,
                   apply_url=URL_ADAPTER.validate_python("https://example.com/apply"),
                   workplace_type=workplace)

    def test_fresher_role_is_relevant(self):
        result = self.matcher.evaluate(self.make_job("Python Backend Developer", "Freshers welcome. Python, FastAPI, PostgreSQL."))
        self.assertTrue(result.relevant)
        self.assertEqual(result.experience_status, "fresher")
        self.assertGreaterEqual(result.score, 40)

    def test_two_plus_years_is_rejected(self):
        result = self.matcher.evaluate(self.make_job("Python Backend Developer", "Requires 2+ years of Python experience."))
        self.assertFalse(result.relevant)
        self.assertEqual(result.experience_status, "two_plus_years")

    def test_preferred_two_years_is_not_a_hard_rejection(self):
        result = self.matcher.evaluate(
            self.make_job("Python Backend Developer", "2 years of experience preferred. Python and FastAPI.")
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.experience_status, "unknown")

    def test_mandatory_experience_overrides_fresher_language(self):
        result = self.matcher.evaluate(
            self.make_job("Python Backend Developer", "Freshers welcome, but 2+ years of experience required.")
        )
        self.assertFalse(result.relevant)
        self.assertEqual(result.experience_status, "two_plus_years")

    def test_senior_title_is_rejected(self):
        result = self.matcher.evaluate(self.make_job("Senior Python Backend Developer", "Python and FastAPI."))
        self.assertFalse(result.relevant)

    def test_unpaid_role_is_rejected(self):
        result = self.matcher.evaluate(self.make_job("Python Backend Developer Intern", "Unpaid internship. Python and FastAPI."))
        self.assertFalse(result.relevant)

    def test_remote_role_restricted_to_other_country_is_rejected(self):
        result = self.matcher.evaluate(
            self.make_job("Python Backend Developer", "Python and FastAPI.", location="United States", workplace="remote")
        )
        self.assertFalse(result.relevant)

    def test_unrelated_location_is_rejected(self):
        result = self.matcher.evaluate(
            self.make_job("Python Backend Developer", "Python and FastAPI.", location="Berlin, Germany")
        )
        self.assertFalse(result.relevant)

    def test_insecure_or_shortened_application_urls_are_rejected(self):
        http_job = self.make_job("Python Backend Developer", "Python and FastAPI.")
        http_job.apply_url = URL_ADAPTER.validate_python("http://example.com/job")
        short_link = self.make_job("Python Backend Developer", "Python and FastAPI.")
        short_link.apply_url = URL_ADAPTER.validate_python("https://bit.ly/job")
        expected = "Application URL is insecure or uses a URL shortener"
        self.assertEqual(self.matcher.evaluate(http_job).rejection_reason, expected)
        self.assertEqual(self.matcher.evaluate(short_link).rejection_reason, expected)


class DatabaseTests(unittest.TestCase):
    def test_saves_only_passed_job_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = JobRepository(Path(temp) / "jobs.db")
            job = Job(source="test", title="Python Backend Developer", company="Acme",
                      location="Remote", description="Python FastAPI",
                      apply_url=URL_ADAPTER.validate_python("https://example.com/job/1"),
                      match_score=80, matched_role="Python Backend Developer", experience_status="fresher",
                      match_reasons=["Role match"])
            first = repo.save_jobs([job])
            second = repo.save_jobs([job])
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(repo.count_jobs(), 1)
            self.assertEqual(len(repo.recent_jobs()), 1)
            self.assertEqual(second["updated"], 1)

    def test_canonical_url_deduplicates_changed_title(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = JobRepository(Path(temp) / "jobs.db")
            first = Job(
                source="feed-a", source_job_id="a1", title="Python Developer", company="Acme",
                location="India", description="Python",
                apply_url=URL_ADAPTER.validate_python("https://example.com/job/1?utm_source=a"),
                match_score=60, eligibility_status="eligible",
            )
            duplicate = Job(
                source="feed-b", source_job_id="b9", title="Junior Python Engineer", company="Acme",
                location="India", description="Python",
                apply_url=URL_ADAPTER.validate_python("https://example.com/job/1"),
                match_score=60, eligibility_status="eligible",
            )
            repo.save_jobs([first])
            repo.save_jobs([duplicate])
            self.assertEqual(repo.count_jobs(), 1)
            self.assertEqual(len(repo.recent_jobs()), 1)
            conn = sqlite3.connect(Path(temp) / "jobs.db")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0], 2)
            finally:
                conn.close()

    def test_persists_rejection_reason_and_ai_evaluation(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = JobRepository(Path(temp) / "jobs.db")
            rejected = Job(
                source="test", title="Senior Python Developer", company="Acme",
                description="Python", apply_url=URL_ADAPTER.validate_python("https://example.com/senior"),
                eligibility_status="rejected", processing_status="rejected",
                rejection_reason="Requires 2+ years of experience",
            )
            repo.save_jobs([rejected])
            self.assertEqual(repo.rejected_jobs()[0]["rejection_reason"], "Requires 2+ years of experience")

            eligible = Job(
                source="test", title="Python Developer", company="Acme", description="Python",
                apply_url=URL_ADAPTER.validate_python("https://example.com/junior"),
                eligibility_status="eligible", processing_status="pending_ai",
            )
            repo.save_jobs([eligible])
            job_id = repo.pending_ai_jobs()[0]["id"]
            evaluation = {"decision": "REVIEW", "reason": "Potential match"}
            repo.update_ai_evaluation(job_id, evaluation, 77, ["Docker"])
            conn = sqlite3.connect(Path(temp) / "jobs.db")
            try:
                row = conn.execute(
                    "SELECT ai_evaluation_json, missing_skills_json, processing_status FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertIn('"decision": "REVIEW"', row[0])
            self.assertEqual(row[1], '["Docker"]')
            self.assertEqual(row[2], "evaluated")

    def test_migration_preserves_existing_rows_and_adds_evaluation_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript("""
                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
                        title TEXT NOT NULL, company TEXT NOT NULL, location TEXT,
                        description TEXT NOT NULL DEFAULT '', apply_url TEXT NOT NULL,
                        employment_type TEXT, workplace_type TEXT, posted_at TEXT, department TEXT,
                        tags_json TEXT NOT NULL DEFAULT '[]', match_score INTEGER NOT NULL DEFAULT 0,
                        matched_role TEXT, experience_status TEXT NOT NULL DEFAULT 'unknown',
                        match_reasons_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'shortlisted',
                        first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO jobs(fingerprint,title,company,description,apply_url)
                    VALUES('legacy-fp','Python Developer','Acme','Python','https://example.com/job');
                """)
                conn.commit()
            finally:
                conn.close()
            repo = JobRepository(db_path)
            self.assertEqual(repo.count_jobs(), 1)
            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertIn("ai_evaluation_json", columns)
                self.assertIn("description_hash", columns)
                self.assertEqual(conn.execute("SELECT title FROM jobs").fetchone()[0], "Python Developer")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
