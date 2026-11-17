import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pydantic import HttpUrl, TypeAdapter

from jobpilot_discovery.agent import SingleInstanceLock
from jobpilot_discovery.agent.tasks import TaskOutcome
from jobpilot_discovery.cli import main
from jobpilot_discovery.config import AutomationSettings, Settings
from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.models import Job


URL_ADAPTER = TypeAdapter(HttpUrl)


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = JobRepository(Path(self.temp.name) / "jobs.db")

    def create_applying_application(self) -> int:
        self.repo.save_jobs([Job(
            source="test",
            source_job_id="python-backend-1",
            title="Python Backend Developer",
            company="Acme",
            location="India",
            description="Python backend role",
            apply_url=URL_ADAPTER.validate_python(
                "https://careers.example.com/jobs/1"
            ),
        )])
        job_id = self.repo.recent_jobs()[0]["id"]
        application_id = self.repo.create_application(job_id)["application_id"]
        self.repo.update_application_status(application_id, "pending_review")
        self.repo.update_application_status(application_id, "applying")
        return application_id

    def test_interrupted_submission_is_marked_unverified_and_never_retried(self):
        application_id = self.create_applying_application()
        task_id, created = self.repo.create_agent_task(
            "application_submission",
            f"application:{application_id}",
            related_application_id=application_id,
        )
        self.assertTrue(created)
        self.assertTrue(self.repo.claim_agent_task(task_id))

        result = self.repo.recover_agent_tasks()

        application = self.repo.application(application_id)
        task = self.repo.agent_task(task_id)
        assert application is not None
        assert task is not None
        self.assertEqual(result["manual_review"], 1)
        self.assertEqual(application["application_status"], "submission_unverified")
        self.assertFalse(application["submission_verified"])
        self.assertEqual(task["status"], "requires_manual_review")
        self.assertIn("never retried", task["error_details"])
        self.assertEqual(
            self.repo.recover_agent_tasks()["manual_review"],
            0,
        )

    def test_recovery_preserves_a_durably_verified_application(self):
        application_id = self.create_applying_application()
        task_id, _ = self.repo.create_agent_task(
            "application_submission",
            f"application:{application_id}",
            related_application_id=application_id,
        )
        self.repo.claim_agent_task(task_id)
        self.repo.update_application_status(
            application_id,
            "submitted",
            submission_verified=True,
            submission_reference="CONFIRM-1",
        )

        result = self.repo.recover_agent_tasks()

        application = self.repo.application(application_id)
        task = self.repo.agent_task(task_id)
        assert application is not None
        assert task is not None
        self.assertEqual(result["manual_review"], 0)
        self.assertEqual(application["application_status"], "submitted")
        self.assertTrue(application["submission_verified"])
        self.assertEqual(task["status"], "completed")

    def test_orphaned_applying_record_requires_manual_verification(self):
        application_id = self.create_applying_application()

        result = self.repo.recover_agent_tasks()

        application = self.repo.application(application_id)
        assert application is not None
        self.assertEqual(result["manual_review"], 1)
        self.assertEqual(application["application_status"], "submission_unverified")
        self.assertFalse(application["submission_verified"])

    def test_interrupted_non_submission_task_is_boundedly_rescheduled(self):
        task_id, _ = self.repo.create_agent_task("discovery", "discovery")
        self.repo.claim_agent_task(task_id)

        result = self.repo.recover_agent_tasks()

        task = self.repo.agent_task(task_id)
        assert task is not None
        self.assertEqual(result["rescheduled"], 1)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["retry_count"], 1)
        self.assertIsNone(self.repo.last_agent_task_attempt("discovery"))

    def test_single_instance_lock_reports_running_owner(self):
        lock_path = Path(self.temp.name) / "agent.lock"

        with SingleInstanceLock(lock_path):
            self.assertTrue(SingleInstanceLock.is_locked(lock_path))

        self.assertFalse(SingleInstanceLock.is_locked(lock_path))


class AgentCLITests(unittest.TestCase):
    def test_status_command_reports_idle_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(db_path=Path(temp) / "jobs.db")
            output = io.StringIO()
            with (
                patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings),
                patch(
                    "jobpilot_discovery.cli.AutomationSettings.from_env",
                    return_value=AutomationSettings(),
                ),
                patch("sys.argv", ["main.py", "agent", "status"]),
                redirect_stdout(output),
            ):
                main()

            self.assertIn("Agent running: no", output.getvalue())
            self.assertIn("No agent tasks recorded.", output.getvalue())

    def test_run_once_reports_scheduler_outcomes(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(db_path=Path(temp) / "jobs.db")
            outcome = TaskOutcome(task_id="task-1", status="completed")
            output = io.StringIO()
            with (
                patch("jobpilot_discovery.cli.Settings.from_env", return_value=settings),
                patch(
                    "jobpilot_discovery.cli.AutomationSettings.from_env",
                    return_value=AutomationSettings(),
                ),
                patch("jobpilot_discovery.cli.AgentScheduler.run_once", return_value={
                    "recovered": {},
                    "outcomes": {"discovery": outcome},
                }),
                patch("jobpilot_discovery.cli.WindowsNotifier"),
                patch("sys.argv", ["main.py", "agent", "run-once"]),
                redirect_stdout(output),
            ):
                main()

            self.assertIn("[completed] discovery", output.getvalue())


if __name__ == "__main__":
    unittest.main()
