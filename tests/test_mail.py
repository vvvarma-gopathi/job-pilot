import base64
import tempfile
import unittest
from pathlib import Path

from pydantic import HttpUrl, TypeAdapter

from jobpilot_discovery.db import JobRepository
from jobpilot_discovery.gmail import (
    GMAIL_READONLY_SCOPE,
    GmailReadOnlyClient,
    WindowsCredentialStore,
)
from jobpilot_discovery.mail import (
    ApplicationEmailMatcher,
    DeterministicEmailClassifier,
    GmailMessage,
    RecruiterResponseProcessor,
)
from jobpilot_discovery.models import Job


URL_ADAPTER = TypeAdapter(HttpUrl)


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[(service, username)] = value


class FakeRequest:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeMessages:
    def __init__(self):
        encoded = base64.urlsafe_b64encode(
            b"Thank you for applying to Acme Python Developer."
        ).decode("ascii").rstrip("=")
        self.payload = {
            "id": "gmail-1",
            "snippet": "confirmation",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Hiring Team <jobs@acme.example>"},
                    {"name": "Subject", "value": "Application received"},
                    {"name": "Date", "value": "Tue, 17 Nov 2026 10:00:00 +0000"},
                ],
                "parts": [{
                    "mimeType": "text/plain",
                    "body": {"data": encoded},
                }],
            },
        }

    def list(self, **kwargs):
        assert kwargs["userId"] == "me"
        assert kwargs["q"] == "newer_than:14d"
        return FakeRequest({
            "messages": [{"id": "gmail-1"}],
        })

    def get(self, **kwargs):
        assert kwargs["userId"] == "me"
        assert kwargs["format"] == "full"
        return FakeRequest(self.payload)


class FakeUsers:
    def __init__(self):
        self._messages = FakeMessages()

    def messages(self):
        return self._messages


class FakeService:
    def users(self):
        return FakeUsers()


class MailIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = JobRepository(Path(self.temp.name) / "jobs.db")
        self.repository.save_jobs([Job(
            source="greenhouse",
            source_job_id="acme-1",
            title="Python Developer",
            company="Acme",
            location="India",
            description="Entry-level Python",
            apply_url=URL_ADAPTER.validate_python(
                "https://boards.greenhouse.io/acme/jobs/1"
            ),
            eligibility_status="eligible",
        )])
        self.job_id = self.repository.recent_jobs()[0]["id"]
        self.application_id = self.repository.create_application(self.job_id)["application_id"]

    def test_oauth_scope_is_read_only_and_credentials_store_uses_injected_keyring(self):
        keyring = FakeKeyring()
        store = WindowsCredentialStore(keyring_module=keyring)

        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
        self.assertIsNone(store.get())
        store.set("serialized-token")
        self.assertEqual(store.get(), "serialized-token")

    def test_gmail_message_parser_extracts_plain_text_and_headers(self):
        client = GmailReadOnlyClient("unused.json", service=FakeService())

        messages = client.fetch_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "gmail-1")
        self.assertEqual(messages[0].sender, "Hiring Team <jobs@acme.example>")
        self.assertEqual(messages[0].subject, "Application received")
        self.assertIn("Thank you for applying", messages[0].body)
        self.assertEqual(messages[0].received_at, "2026-11-17T10:00:00+00:00")

    def test_confirmation_and_interview_email_update_only_linked_application(self):
        confirmation = GmailMessage(
            message_id="message-confirm",
            sender="jobs@acme.example",
            subject="Application received: Python Developer at Acme",
            received_at="2026-11-17T10:00:00+00:00",
            body="Thank you for applying to Acme for Python Developer.",
        )
        interview = GmailMessage(
            message_id="message-interview",
            sender="recruiter@acme.example",
            subject="Interview invitation: Python Developer at Acme",
            received_at="2026-11-18T10:00:00+00:00",
            body="Acme would like to schedule an interview for Python Developer.",
        )
        processor = RecruiterResponseProcessor(self.repository)

        counts = processor.process([confirmation, interview])
        application = self.repository.application(self.application_id)

        assert application is not None
        self.assertEqual(counts["matched"], 2)
        self.assertEqual(application["application_status"], "interview")
        self.assertTrue(application["submission_verified"])
        self.assertEqual(len(self.repository.recruiter_emails()), 2)

    def test_duplicate_messages_are_not_processed_twice_and_unknowns_are_queued(self):
        message = GmailMessage(
            message_id="unique-message",
            sender="recruiter@acme.example",
            subject="Python Developer application update",
            received_at=None,
            body="Acme Python Developer application status update.",
        )
        processor = RecruiterResponseProcessor(self.repository)

        first = processor.process([message])
        second = processor.process([message])

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.repository.recruiter_emails()), 1)
        self.assertEqual(
            self.repository.recruiter_emails(processing_status="review")[0]["classification"],
            "unknown",
        )

    def test_ambiguous_company_title_matches_are_sent_to_review(self):
        second_job = Job(
            source="lever",
            source_job_id="acme-2",
            title="Python Developer",
            company="Acme",
            location="Hyderabad, India",
            description="Entry-level Python",
            apply_url=URL_ADAPTER.validate_python("https://jobs.lever.co/acme/2"),
            eligibility_status="eligible",
        )
        self.repository.save_jobs([second_job])
        second_job_id = next(
            job["id"] for job in self.repository.recent_jobs()
            if str(job["apply_url"]).endswith("/2")
        )
        self.repository.create_application(
            second_job_id, confirm_possible_duplicate=True
        )
        message = GmailMessage(
            message_id="ambiguous-message",
            sender="jobs@acme.example",
            subject="Python Developer at Acme",
            received_at=None,
            body="Interview invitation for Python Developer at Acme.",
        )

        match = ApplicationEmailMatcher(self.repository).match(message)

        self.assertIsNone(match.application_id)
        self.assertTrue(match.ambiguous)

    def test_classifier_covers_offer_assessment_rejection_and_unrelated(self):
        classifier = DeterministicEmailClassifier()
        cases = {
            "Pleased to offer": "offer",
            "Technical assessment": "technical_assessment",
            "We are not moving forward": "application_rejection",
            "Family newsletter": "unrelated",
        }
        for subject, expected in cases.items():
            with self.subTest(subject=subject):
                classification = classifier.classify(GmailMessage(
                    message_id=subject,
                    sender="someone@example.com",
                    subject=subject,
                    received_at=None,
                    body="",
                ))
                self.assertEqual(classification.category, expected)


if __name__ == "__main__":
    unittest.main()
