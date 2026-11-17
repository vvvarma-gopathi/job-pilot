import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from jobpilot_discovery.answers import AnswerBank, VerifiedAnswer
from jobpilot_discovery.profile import load_candidate_profile


class CandidateProfileTests(unittest.TestCase):
    def test_profile_uses_existing_facts_but_does_not_invent_contact_details(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_candidate_profile(
                Path(temp) / "missing-profile.json",
                {
                    "candidate": {
                        "education": "B.Sc Computer Science",
                        "graduation_year": 2027,
                        "projects": ["AI Pulse"],
                    },
                    "skills": ["Python", "FastAPI"],
                },
            )

        self.assertIsNone(profile.full_name)
        self.assertIsNone(profile.email)
        self.assertIsNone(profile.resume_path)
        self.assertEqual(profile.education.graduation_year, 2027)
        self.assertEqual(profile.technical_skills, ["Python", "FastAPI"])

    def test_local_profile_overrides_defaults_and_rejects_invalid_email(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "candidate.json"
            path.write_text(json.dumps({"full_name": "Candidate", "email": "not-an-email"}))

            with self.assertRaises(ValidationError):
                load_candidate_profile(path, {"candidate": {}, "skills": []})

    def test_only_exact_verified_answers_marked_reusable_are_selected(self):
        bank = AnswerBank(answers=[
            VerifiedAnswer(
                id="python-experience",
                question="Describe your Python experience",
                answer="I have built Python projects.",
                category="technical_experience",
                verification_status="verified",
                auto_reusable=True,
            )
        ])

        answer = bank.exact_match(
            "Describe your Python experience?", category="technical_experience"
        )
        self.assertIsNotNone(answer)
        self.assertIsNone(bank.exact_match("What is your expected salary?"))

    def test_unverified_answer_cannot_be_reused_automatically(self):
        with self.assertRaises(ValidationError):
            VerifiedAnswer(
                id="availability",
                question="When can you start?",
                answer="Immediately",
                category="availability",
                verification_status="unverified",
                auto_reusable=True,
            )


if __name__ == "__main__":
    unittest.main()
