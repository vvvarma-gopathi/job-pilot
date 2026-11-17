import unittest
from unittest.mock import patch

from pydantic import ValidationError

from jobpilot_discovery.ai import (
    GeminiEvaluationError,
    GeminiJobEvaluator,
    JobEvaluation,
    calculate_final_score,
    evaluation_cache_key,
)


class GeminiEvaluationTests(unittest.TestCase):
    def test_response_schema_rejects_out_of_range_scores(self):
        with self.assertRaises(ValidationError):
            JobEvaluation(
                is_fresher_eligible=True,
                role_match_score=120,
                technical_skill_score=80,
                experience_match_score=90,
                overall_score=90,
                matching_skills=["Python"],
                missing_skills=[],
                required_experience="0-1 years",
                decision="APPLY",
                reason="Strong match",
            )

    def test_final_score_is_python_calculated_and_hard_eligibility_wins(self):
        result = JobEvaluation(
            is_fresher_eligible=False,
            role_match_score=100,
            technical_skill_score=100,
            experience_match_score=100,
            overall_score=100,
            matching_skills=["Python"],
            missing_skills=[],
            required_experience="3 years",
            decision="APPLY",
            reason="Strong technical match",
        )
        final = calculate_final_score(result, "India", None, "full_time", ["India", "Remote"])
        self.assertEqual(final.overall_score, 100)
        self.assertEqual(final.decision, "REJECT")

    def test_decision_thresholds_use_python_score(self):
        def result(score):
            return JobEvaluation(
                is_fresher_eligible=True,
                role_match_score=score,
                technical_skill_score=score,
                experience_match_score=score,
                overall_score=0,
                matching_skills=[],
                missing_skills=[],
                required_experience="0-1 years",
                decision="REJECT",
                reason="Test",
            )

        strong = calculate_final_score(result(100), "India", None, "full_time", ["India", "Remote"])
        potential = calculate_final_score(result(75), "India", None, "full_time", ["India", "Remote"])
        below_threshold = calculate_final_score(result(50), "India", None, "full_time", ["India", "Remote"])
        self.assertEqual((strong.overall_score, strong.decision), (100, "APPLY"))
        self.assertEqual((potential.overall_score, potential.decision), (78, "REVIEW"))
        self.assertEqual((below_threshold.overall_score, below_threshold.decision), (55, "REJECT"))

    def test_cache_key_includes_context_that_changes_final_score(self):
        config = {"skills": ["python"]}
        base = {"title": "Python Developer", "description": "Python", "location": "India"}
        remote = dict(base, location="Remote")
        self.assertNotEqual(
            evaluation_cache_key(base, "test-model", config),
            evaluation_cache_key(remote, "test-model", config),
        )

    def test_rate_limit_retries_then_reports_quota_status(self):
        class RateLimitError(Exception):
            code = 429

        calls = []

        class FakeModels:

            def generate_content(self, **kwargs):
                calls.append(kwargs)
                raise RateLimitError("quota exceeded")

        client = type("FakeClient", (), {"models": FakeModels()})()
        with patch("google.genai.Client", return_value=client):
            evaluator = GeminiJobEvaluator(api_key="test-key", model="test-model", max_retries=1)
        with patch("jobpilot_discovery.ai.time.sleep"):
            with self.assertRaises(GeminiEvaluationError) as caught:
                evaluator.evaluate(
                    {"title": "Python Developer", "description": "Python"},
                    {"roles": [], "skills": []},
                )
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["model"], "test-model")


if __name__ == "__main__":
    unittest.main()
