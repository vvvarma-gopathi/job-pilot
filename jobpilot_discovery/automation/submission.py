from __future__ import annotations

import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from ..config import AutomationSettings
from .exceptions import AutomationSafetyError, UserChallengeRequired
from .models import SubmissionVerification

_SUCCESS_PHRASES = (
    "thank you for applying",
    "thank you for your application",
    "application has been submitted",
    "application was submitted",
    "successfully submitted your application",
    "we have received your application",
    "we've received your application",
)
_REFERENCE_PATTERN = re.compile(
    r"(?:application|submission)\s+(?:reference|id|number)\s*[:#]\s*([A-Z0-9-]+)",
    re.IGNORECASE,
)


class ChallengeDetector:
    _CHALLENGE_TEXT = re.compile(
        r"\b(?:captcha|verify you are human|one.time password|otp|"
        r"two.factor authentication|authentication required|sign in to continue)\b",
        re.IGNORECASE,
    )

    def detect(self, page: Page) -> str | None:
        for frame in page.frames:
            frame_url = frame.url.casefold()
            if any(token in frame_url for token in ("captcha", "hcaptcha", "recaptcha")):
                return "CAPTCHA challenge detected"
        visible_text = page.locator("body").inner_text(timeout=3_000)
        match = self._CHALLENGE_TEXT.search(visible_text)
        return f"Security challenge detected: {match.group(0)}" if match else None

    def require_clear(self, page: Page) -> None:
        challenge = self.detect(page)
        if challenge:
            raise UserChallengeRequired(challenge)


class SubmissionVerifier:
    def verify(self, page: Page) -> SubmissionVerification:
        visible_text = page.locator("body").inner_text(timeout=5_000)
        normalized = " ".join(visible_text.casefold().split())
        if not any(phrase in normalized for phrase in _SUCCESS_PHRASES):
            return SubmissionVerification(
                verified=False,
                reason="No explicit application confirmation was detected.",
            )
        reference = _REFERENCE_PATTERN.search(visible_text)
        return SubmissionVerification(
            verified=True,
            reference=reference.group(1) if reference else None,
            reason="The page displayed an explicit application confirmation.",
        )


class ApplicationSubmitter:
    def __init__(self, settings: AutomationSettings):
        self.settings = settings

    def can_submit(
        self,
        *,
        user_approved: bool,
        eligibility_passed: bool,
        all_required_answered: bool,
        employer_verified: bool,
        not_previously_applied: bool,
        limits_available: bool,
        no_challenge: bool,
    ) -> bool:
        checks = (
            eligibility_passed,
            all_required_answered,
            employer_verified,
            not_previously_applied,
            limits_available,
            no_challenge,
        )
        if not all(checks):
            return False
        if self.settings.application_mode == "review":
            return user_approved
        return self.settings.auto_apply_enabled

    def submit(self, page: Page, button, *, permitted: bool) -> SubmissionVerification:
        if not permitted:
            raise AutomationSafetyError(
                "Application submission safety checks or approval are incomplete"
            )
        ChallengeDetector().require_clear(page)
        try:
            button.click()
        except PlaywrightError as exc:
            return SubmissionVerification(
                verified=False,
                reason=f"Submission click outcome is uncertain: {exc}",
            )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            return SubmissionVerification(
                verified=False,
                reason=f"Submission action may have occurred, but page confirmation timed out: {exc}",
            )
        return SubmissionVerifier().verify(page)
