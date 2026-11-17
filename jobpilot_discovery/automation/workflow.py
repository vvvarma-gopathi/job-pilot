from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from playwright.sync_api import Error as PlaywrightError, Page

from ..answers import AnswerBank
from ..config import AutomationSettings, Settings, load_search_config
from ..db import JobRepository
from ..profile import CandidateProfile, load_candidate_profile
from .browser import BrowserManager
from .exceptions import AutomationSafetyError, UserChallengeRequired
from .forms import (
    ApplicationSiteDetector,
    FieldMapper,
    FormAnalyzer,
    FormFiller,
    is_unverified_sensitive_field,
)
from .models import FormFillResult, SiteInfo, SubmissionVerification
from .submission import ApplicationSubmitter, ChallengeDetector

logger = logging.getLogger("jobpilot.discovery.automation")


@dataclass
class PreparationReport:
    application_id: int
    site: SiteInfo
    fill_result: FormFillResult
    missing_required_fields: list[str] = field(default_factory=list)
    blocked_fields: list[str] = field(default_factory=list)
    status: str = "pending_review"
    submission: SubmissionVerification | None = None


ApprovalCallback = Callable[[PreparationReport, Page], bool]


class ApplicationPreparer:
    def __init__(
        self,
        repository: JobRepository,
        profile: CandidateProfile,
        answer_bank: AnswerBank,
        settings: AutomationSettings,
        *,
        browser_factory: Callable[[], BrowserManager] | None = None,
    ):
        self.repository = repository
        self.profile = profile
        self.answer_bank = answer_bank
        self.settings = settings
        self.browser_factory = browser_factory or BrowserManager
        self.site_detector = ApplicationSiteDetector()
        self.form_analyzer = FormAnalyzer()
        self.form_filler = FormFiller(FieldMapper(profile, answer_bank))
        self.submitter = ApplicationSubmitter(settings)

    @classmethod
    def from_settings(
        cls, repository: JobRepository, settings: Settings,
        automation_settings: AutomationSettings,
        *,
        browser_factory: Callable[[], BrowserManager] | None = None,
    ) -> "ApplicationPreparer":
        search_config = load_search_config(settings.search_config_path)
        return cls(
            repository,
            load_candidate_profile(settings.candidate_profile_path, search_config),
            AnswerBank.load(settings.verified_answers_path),
            automation_settings,
            browser_factory=browser_factory,
        )

    def prepare(
        self,
        application_id: int,
        *,
        confirm_submit: ApprovalCallback | None = None,
        resume_category: str | None = None,
    ) -> PreparationReport:
        application = self.repository.application(application_id)
        if application is None:
            raise KeyError(f"Application {application_id} does not exist")
        if application["application_status"] not in {
            "shortlisted", "pending_review", "ready_to_apply",
        }:
            raise AutomationSafetyError(
                f"Application status {application['application_status']} is not safe to prepare"
            )
        if application["eligibility_status"] != "eligible":
            raise AutomationSafetyError("Only deterministically eligible jobs can be prepared")

        if application["application_status"] == "shortlisted":
            self.repository.update_application_status(
                application_id, "pending_review",
                detail="Application preparation started in review mode",
            )

        with self.browser_factory() as browser:
            page = browser.new_page()
            browser.navigate(page, application["application_url"])
            ChallengeDetector().require_clear(page)
            site = self.site_detector.require_supported(page, application["application_url"])
            fields = self.form_analyzer.analyze(page)
            fill_result = self.form_filler.fill(page, fields, resume_category=resume_category)
            missing = self.form_filler.missing_required_fields(page, fields)
            blocked_fields = [
                field.semantic_text or f"Field {field.index + 1}"
                for field in fields
                if field.required and is_unverified_sensitive_field(field)
            ]
            required_resume = any(
                field.required and field.field_type == "file" for field in fields
            )
            has_configured_resume = bool(
                self.profile.resume_path or self.profile.resume_variants
            )
            if required_resume and not has_configured_resume:
                blocked_fields.append("No configured resume file is available")
            report = PreparationReport(
                application_id=application_id,
                site=site,
                fill_result=fill_result,
                missing_required_fields=missing,
                blocked_fields=blocked_fields,
            )
            if fill_result.resume_uploaded:
                configured_resume = self.form_filler.configured_resume(resume_category)
                self.repository.record_application_materials(
                    application_id,
                    resume_used=str(configured_resume) if configured_resume else None,
                    cover_letter_used=(
                        "profile or verified answer"
                        if any("cover letter" in item.casefold() for item in fill_result.filled_fields)
                        else None
                    ),
                )

            if self.settings.application_mode == "review" and confirm_submit is not None:
                report.status = "pending_review"
                if blocked_fields:
                    return report
                user_approved = confirm_submit(report, page)
                fields = self.form_analyzer.analyze(page)
                report.missing_required_fields = self.form_filler.missing_required_fields(
                    page, fields
                )
                blocked_fields = [
                    field.semantic_text or f"Field {field.index + 1}"
                    for field in fields
                    if field.required and is_unverified_sensitive_field(field)
                ]
                if required_resume and not has_configured_resume:
                    blocked_fields.append("No configured resume file is available")
                report.blocked_fields = blocked_fields
            else:
                user_approved = False

            if report.missing_required_fields or report.blocked_fields:
                self._set_status(application_id, "pending_review")
                report.status = "pending_review"
                return report

            self._set_status(application_id, "ready_to_apply")
            report.status = "ready_to_apply"

            has_similar_application = self.repository.has_other_company_title_application(
                application_id
            )
            duplicate_is_confirmed = bool(application["duplicate_confirmed"])
            not_previously_applied = (
                not self.repository.has_prior_application_attempt(application_id)
                and (
                    not has_similar_application
                    or (
                        self.settings.application_mode == "review"
                        and duplicate_is_confirmed
                    )
                )
            )
            limits_available = self.repository.application_limits_available(
                application["company_name"],
                max_per_day=self.settings.max_applications_per_day,
                max_per_company_per_day=self.settings.max_applications_per_company_per_day,
            )
            eligibility_passed = application["eligibility_status"] == "eligible"
            if self.settings.application_mode == "auto_apply":
                eligibility_passed = eligibility_passed and self._ai_approved(application)
            employer_verified = (
                site.platform in {"greenhouse", "lever"}
                and application["source"] == site.platform
            )
            if self.settings.application_mode == "review" and user_approved:
                employer_verified = site.supported
            permitted = self.submitter.can_submit(
                user_approved=user_approved,
                eligibility_passed=eligibility_passed,
                all_required_answered=not report.missing_required_fields,
                employer_verified=employer_verified,
                not_previously_applied=not_previously_applied,
                limits_available=limits_available,
                no_challenge=True,
            )
            if not permitted:
                if not eligibility_passed:
                    report.blocked_fields.append("Eligibility or AI approval checks did not pass")
                if not employer_verified:
                    report.blocked_fields.append("Employer/source could not be verified for this mode")
                if not not_previously_applied:
                    report.blocked_fields.append("A prior or similar application requires manual resolution")
                if not limits_available:
                    report.blocked_fields.append("Configured daily application limit has been reached")
                if self.settings.application_mode == "review" and not user_approved:
                    report.blocked_fields.append("Explicit submission approval was not given")
                if (
                    self.settings.application_mode == "auto_apply"
                    and not self.settings.auto_apply_enabled
                ):
                    report.blocked_fields.append("Auto-apply is disabled")
                return report

            buttons = page.locator("button[type='submit'], input[type='submit']")
            if buttons.count() != 1:
                report.missing_required_fields.append(
                    "A single unambiguous submit button could not be identified"
                )
                return report

            ChallengeDetector().require_clear(page)
            self.repository.update_application_status(
                application_id, "applying",
                detail="A guarded application submission attempt is starting",
            )
            self.repository.add_application_event(
                application_id,
                "submission_attempt_started",
                "Submission initiated after eligibility, form, duplicate, limit, and approval checks",
            )
            try:
                verification = self.submitter.submit(
                    page, buttons.first, permitted=True
                )
            except UserChallengeRequired as exc:
                self.repository.update_application_status(
                    application_id, "pending_review", error_message=str(exc),
                    detail="A security challenge appeared before the submit action",
                )
                report.status = "pending_review"
                return report
            except PlaywrightError as exc:
                verification = SubmissionVerification(
                    verified=False,
                    reason=f"Submission state could not be verified after browser error: {exc}",
                )

            report.submission = verification
            if verification.verified:
                self.repository.update_application_status(
                    application_id,
                    "submitted",
                    submission_verified=True,
                    submission_reference=verification.reference,
                    detail=verification.reason,
                )
                report.status = "submitted"
            else:
                self.repository.update_application_status(
                    application_id,
                    "submission_unverified",
                    error_message=verification.reason,
                    detail=verification.reason,
                )
                report.status = "submission_unverified"
            return report

    def _set_status(self, application_id: int, status: str) -> None:
        application = self.repository.application(application_id)
        if application is not None and application["application_status"] != status:
            self.repository.update_application_status(application_id, status)

    @staticmethod
    def _ai_approved(application: dict) -> bool:
        evaluation = application.get("ai_evaluation_json")
        if not evaluation:
            return False
        try:
            parsed = json.loads(evaluation)
        except json.JSONDecodeError:
            logger.warning(
                "Application %s has invalid stored evaluation JSON",
                application["id"],
            )
            return False
        return (
            parsed.get("decision") == "APPLY"
            and isinstance(parsed.get("overall_score"), int)
            and parsed["overall_score"] >= 85
        )
