import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from jobpilot_discovery.answers import AnswerBank, VerifiedAnswer
from jobpilot_discovery.automation import (
    ApplicationSiteDetector,
    ApplicationSubmitter,
    BrowserManager,
    ChallengeDetector,
    FieldMapper,
    FormAnalyzer,
    FormFiller,
)
from jobpilot_discovery.automation.browser import validate_application_url
from jobpilot_discovery.automation.exceptions import AutomationSafetyError, UserChallengeRequired
from jobpilot_discovery.config import AutomationSettings
from jobpilot_discovery.profile import CandidateProfile


class BrowserAutomationTests(unittest.TestCase):
    def open_local_page(self):
        manager = BrowserManager(
            headless=True,
            browser_channel="chrome",
            allow_local_urls=True,
        )
        try:
            manager.__enter__()
        except PlaywrightError as exc:
            self.skipTest(f"Install Playwright Chromium or Chrome to run browser tests: {exc}")
        self.addCleanup(manager.__exit__, None, None, None)
        page = manager.new_page()
        return manager, page

    def test_semantic_mapping_resume_upload_and_unknown_legal_field(self):
        with tempfile.TemporaryDirectory() as temp:
            resume = Path(temp) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.7 mock resume")
            profile = CandidateProfile(
                full_name="Candidate Example",
                email="candidate@example.com",
                resume_path=resume,
            )
            answers = AnswerBank(answers=[VerifiedAnswer(
                id="remote-eligibility",
                question="Can you work remotely?",
                answer="Yes",
                category="work_preference",
                verification_status="verified",
                auto_reusable=True,
            )])
            _, page = self.open_local_page()
            page.set_content("""
                <form>
                  <label for="name">Full name</label>
                  <input id="name" name="name" required>
                  <label for="email">Email address</label>
                  <input id="email" name="email" type="email" required>
                  <fieldset>
                    <legend>Can you work remotely?</legend>
                    <label><input type="radio" name="remote" value="Yes"> Yes</label>
                    <label><input type="radio" name="remote" value="No"> No</label>
                  </fieldset>
                  <label for="resume">Resume</label>
                  <input id="resume" name="resume" type="file" accept=".pdf,.docx" required>
                  <label for="terms">I accept the terms</label>
                  <input id="terms" name="terms" type="checkbox" required>
                </form>
            """)

            fields = FormAnalyzer().analyze(page)
            result = FormFiller(FieldMapper(profile, answers)).fill(page, fields)
            site = ApplicationSiteDetector().detect(page, "https://jobs.example.org/apply")

            self.assertTrue(site.supported)
            self.assertEqual(site.platform, "company")
            self.assertEqual(page.locator("#name").input_value(), "Candidate Example")
            self.assertEqual(page.locator("#email").input_value(), "candidate@example.com")
            self.assertTrue(page.locator('input[name="remote"][value="Yes"]').is_checked())
            self.assertEqual(page.locator("#resume").evaluate("(el) => el.files[0].name"), "resume.pdf")
            self.assertTrue(any("terms" in name.lower() for name in result.missing_required_fields))
            self.assertEqual(result.resume_uploaded, "resume.pdf")

    def test_unknown_required_question_is_not_fabricated(self):
        _, page = self.open_local_page()
        page.set_content("""
            <label for="salary">What is your expected salary?</label>
            <input id="salary" required>
        """)
        fields = FormAnalyzer().analyze(page)
        result = FormFiller(
            FieldMapper(CandidateProfile(), AnswerBank())
        ).fill(page, fields)

        self.assertEqual(result.missing_required_fields, ["What is your expected salary?"])
        self.assertEqual(page.locator("#salary").input_value(), "")

    def test_challenges_and_unsafe_urls_stop_automation(self):
        _, page = self.open_local_page()
        page.set_content("<main>Verify you are human to continue</main>")

        with self.assertRaises(UserChallengeRequired):
            ChallengeDetector().require_clear(page)
        with self.assertRaises(AutomationSafetyError):
            validate_application_url("http://127.0.0.1:8080/apply")

    def test_submission_requires_review_approval_and_explicit_confirmation(self):
        _, page = self.open_local_page()
        settings = AutomationSettings()
        submitter = ApplicationSubmitter(settings)
        checks = {
            "eligibility_passed": True,
            "all_required_answered": True,
            "employer_verified": True,
            "not_previously_applied": True,
            "limits_available": True,
            "no_challenge": True,
        }
        self.assertFalse(submitter.can_submit(user_approved=False, **checks))
        permitted = submitter.can_submit(user_approved=True, **checks)

        page.set_content("""
            <form id="apply">
              <button type="submit">Submit application</button>
            </form>
            <script>
              document.querySelector('#apply').addEventListener('submit', event => {
                event.preventDefault();
                document.body.innerText =
                  'Thank you for applying. Application reference: ABC-123';
              });
            </script>
        """)
        verification = submitter.submit(
            page, page.locator("button[type=submit]"), permitted=permitted
        )

        self.assertTrue(verification.verified)
        self.assertEqual(verification.reference, "ABC-123")


if __name__ == "__main__":
    unittest.main()
