from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from ..automation import ApplicationPreparer, BrowserManager
from ..config import AutomationSettings, Settings
from ..db import JobRepository
from ..gmail import GmailReadOnlyClient
from ..mail import GeminiEmailClassifier, RecruiterResponseProcessor
from ..models import SourceRunResult
from ..notifications import WindowsNotifier
from ..runner import run_discovery, run_evaluation
from .tasks import AgentTaskManager, TaskOutcome


class AgentOrchestrator:
    def __init__(
        self,
        repository: JobRepository,
        settings: Settings,
        automation_settings: AutomationSettings,
        *,
        notifier: WindowsNotifier | None = None,
    ):
        self.repository = repository
        self.settings = settings
        self.automation_settings = automation_settings
        self.notifier = notifier or WindowsNotifier(
            enabled=automation_settings.notifications_enabled
        )
        self.tasks = AgentTaskManager(repository)

    def run_cycle(self, *, force: bool = False) -> dict[str, TaskOutcome]:
        outcomes: dict[str, TaskOutcome] = {}
        schedules = (
            ("discovery", self.automation_settings.discovery_interval_hours * 3600,
             lambda: run_discovery()),
            ("evaluation", self.automation_settings.evaluation_interval_hours * 3600,
             run_evaluation),
            ("application_queue", self.automation_settings.evaluation_interval_hours * 3600,
             self._queue_applications),
        )
        for task_type, interval_seconds, action in schedules:
            if force or self._is_due(task_type, interval_seconds):
                outcome = self.tasks.execute(
                    task_type,
                    self._dedupe_key(task_type),
                    action,
                )
                outcomes[task_type] = outcome
                if outcome.status == "failed":
                    self.notifier.notify(
                        "JobPilot needs attention",
                        f"The {task_type} task failed. Check local agent status.",
                    )
                elif task_type == "discovery" and outcome.status == "completed":
                    source_results = outcome.result
                    inserted = (
                        sum(
                            result.inserted
                            for result in source_results
                            if isinstance(result, SourceRunResult)
                        )
                        if isinstance(source_results, list)
                        else 0
                    )
                    if inserted:
                        self.notifier.notify(
                            "New JobPilot matches",
                            f"{inserted} shortlisted job(s) are ready for review.",
                        )

        if self.automation_settings.gmail_enabled and (
            force or self._is_due(
                "gmail_sync",
                self.automation_settings.gmail_poll_interval_minutes * 60,
            )
        ):
            outcomes["gmail_sync"] = self.tasks.execute(
                "gmail_sync",
                self._dedupe_key("gmail_sync"),
                self._sync_gmail,
            )

        if (
            self.automation_settings.application_mode == "auto_apply"
            and self.automation_settings.auto_apply_enabled
        ):
            outcomes.update(self._submit_approved_applications())
        return outcomes

    def _queue_applications(self) -> dict[str, int]:
        jobs = self.repository.eligible_jobs_without_applications(limit=100)
        created = duplicates = needs_review = 0
        for job in jobs:
            result = self.repository.create_application(job["id"])
            if result["created"]:
                created += 1
            elif result["duplicate"]:
                duplicates += 1
            else:
                needs_review += 1
        if created:
            self.notifier.notify(
                "Applications ready for review",
                f"{created} eligible job(s) were added to the application dashboard.",
            )
        return {"created": created, "duplicates": duplicates, "needs_review": needs_review}

    def _sync_gmail(self) -> dict[str, int]:
        client = GmailReadOnlyClient(
            self.settings.gmail_oauth_client_file,
            query=self.automation_settings.gmail_query,
            max_results=self.automation_settings.gmail_max_messages,
        )
        client.authorize()
        messages = client.fetch_messages()
        gemini_classifier = None
        if self.automation_settings.gemini_email_classification_enabled:
            if not self.settings.gemini_api_key:
                raise ValueError(
                    "Gemini email classification is enabled but no API key is configured"
                )
            gemini_classifier = GeminiEmailClassifier(
                self.settings.gemini_api_key, self.settings.gemini_model
            )
        return RecruiterResponseProcessor(
            self.repository,
            notifier=self.notifier,
            gemini_classifier=gemini_classifier,
        ).process(messages)

    def _submit_approved_applications(self) -> dict[str, TaskOutcome]:
        outcomes: dict[str, TaskOutcome] = {}
        browser_channel = os.getenv("PLAYWRIGHT_BROWSER_CHANNEL")
        applications = self.repository.applications(status="shortlisted", limit=20)
        for application in applications:
            if not self._ai_approved(application):
                continue
            if not self.repository.application_limits_available(
                application["company_name"],
                max_per_day=self.automation_settings.max_applications_per_day,
                max_per_company_per_day=(
                    self.automation_settings.max_applications_per_company_per_day
                ),
            ):
                continue
            task_type = "application_submission"
            outcome = self.tasks.execute(
                task_type,
                f"application:{application['id']}",
                lambda app_id=application["id"]: self._prepare_automatically(
                    app_id, browser_channel
                ),
                related_job_id=application["job_id"],
                related_application_id=application["id"],
            )
            outcomes[f"application_{application['id']}"] = outcome
            if outcome.status == "failed":
                self.notifier.notify(
                    "JobPilot application needs review",
                    "An application preparation task failed; no automatic retry was made.",
                )
            elif outcome.status == "completed" and outcome.result is not None:
                status = outcome.result.status
                if status in {"submitted", "submission_unverified"}:
                    self.notifier.notify(
                        "JobPilot application update",
                        f"Application status: {status}. Verify it in the local dashboard.",
                    )
        return outcomes

    def _prepare_automatically(self, application_id: int, browser_channel: str | None):
        preparer = ApplicationPreparer.from_settings(
            self.repository,
            self.settings,
            self.automation_settings,
            browser_factory=lambda: BrowserManager(
                headless=True,
                browser_channel=browser_channel,
            ),
        )
        return preparer.prepare(application_id)

    @staticmethod
    def _ai_approved(application: dict[str, Any]) -> bool:
        raw = application.get("ai_evaluation_json")
        if not raw:
            return False
        try:
            evaluation = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return (
            evaluation.get("decision") == "APPLY"
            and isinstance(evaluation.get("overall_score"), int)
            and evaluation["overall_score"] >= 85
        )

    def _is_due(self, task_type: str, interval_seconds: int) -> bool:
        last = self.repository.last_agent_task_attempt(task_type)
        if last is None:
            return True
        last_at = datetime.fromisoformat(last)
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_at).total_seconds() >= interval_seconds

    @staticmethod
    def _dedupe_key(task_type: str) -> str:
        return task_type
