from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class Settings:
    db_path: Path
    search_config_path: Path = Path("config/job_search.json")
    candidate_profile_path: Path = Path("data/candidate_profile.json")
    verified_answers_path: Path = Path("data/verified_answers.json")
    gmail_oauth_client_file: Path = Path("data/gmail_oauth_client.json")
    timeout_seconds: float = 25.0
    max_retries: int = 3
    user_agent: str = "JobPilotAI/0.2 (personal local job discovery)"
    remotive_min_interval_hours: int = 6
    arbeitnow_min_interval_hours: int = 6
    arbeitnow_max_pages: int = 10
    ats_min_interval_hours: int = 24
    min_match_score: int = 40
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_max_retries: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            db_path=Path(os.getenv("JOBPILOT_DB_PATH", "data/jobpilot.db")),
            search_config_path=Path(os.getenv("JOBPILOT_SEARCH_CONFIG", "config/job_search.json")),
            candidate_profile_path=Path(os.getenv(
                "CANDIDATE_PROFILE_PATH", "data/candidate_profile.json"
            )),
            verified_answers_path=Path(os.getenv(
                "VERIFIED_ANSWERS_PATH", "data/verified_answers.json"
            )),
            gmail_oauth_client_file=Path(os.getenv(
                "GMAIL_OAUTH_CLIENT_FILE", "data/gmail_oauth_client.json"
            )),
            timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "25")),
            max_retries=max(0, int(os.getenv("HTTP_MAX_RETRIES", "3"))),
            user_agent=os.getenv("HTTP_USER_AGENT", "JobPilotAI/0.2 (personal local job discovery)"),
            remotive_min_interval_hours=max(6, int(os.getenv("REMOTIVE_MIN_INTERVAL_HOURS", "6"))),
            arbeitnow_min_interval_hours=max(1, int(os.getenv("ARBEITNOW_MIN_INTERVAL_HOURS", "6"))),
            arbeitnow_max_pages=max(1, int(os.getenv("ARBEITNOW_MAX_PAGES", "10"))),
            ats_min_interval_hours=max(1, int(os.getenv("ATS_MIN_INTERVAL_HOURS", "24"))),
            min_match_score=max(1, min(100, int(os.getenv("MIN_JOB_MATCH_SCORE", "40")))),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            gemini_max_retries=max(0, int(os.getenv("GEMINI_MAX_RETRIES", "3"))),
        )


class AutomationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_mode: Literal["review", "auto_apply"] = "review"
    auto_apply_enabled: bool = False
    max_applications_per_day: int = Field(default=5, ge=1, le=50)
    max_applications_per_company_per_day: int = Field(default=2, ge=1, le=20)
    gmail_enabled: bool = False
    gmail_query: str = "newer_than:14d"
    gmail_max_messages: int = Field(default=100, ge=1, le=500)
    gemini_email_classification_enabled: bool = False
    notifications_enabled: bool = True
    discovery_interval_hours: int = Field(default=6, ge=1, le=168)
    evaluation_interval_hours: int = Field(default=6, ge=1, le=168)
    gmail_poll_interval_minutes: int = Field(default=20, ge=5, le=1440)

    @model_validator(mode="after")
    def validate_application_mode(self) -> "AutomationSettings":
        if self.auto_apply_enabled and self.application_mode != "auto_apply":
            raise ValueError("AUTO_APPLY_ENABLED requires APPLICATION_MODE=auto_apply")
        return self

    @classmethod
    def from_env(cls) -> "AutomationSettings":
        load_dotenv()
        names = {
            "application_mode": "APPLICATION_MODE",
            "auto_apply_enabled": "AUTO_APPLY_ENABLED",
            "max_applications_per_day": "MAX_APPLICATIONS_PER_DAY",
            "max_applications_per_company_per_day": "MAX_APPLICATIONS_PER_COMPANY_PER_DAY",
            "gmail_enabled": "GMAIL_ENABLED",
            "gmail_query": "GMAIL_SEARCH_QUERY",
            "gmail_max_messages": "GMAIL_MAX_MESSAGES",
            "gemini_email_classification_enabled": "GEMINI_EMAIL_CLASSIFICATION_ENABLED",
            "notifications_enabled": "NOTIFICATIONS_ENABLED",
            "discovery_interval_hours": "AGENT_DISCOVERY_INTERVAL_HOURS",
            "evaluation_interval_hours": "AGENT_EVALUATION_INTERVAL_HOURS",
            "gmail_poll_interval_minutes": "GMAIL_POLL_INTERVAL_MINUTES",
        }
        return cls.model_validate({
            field: os.environ[environment_name]
            for field, environment_name in names.items()
            if environment_name in os.environ
        })


def load_search_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Missing job search config: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not data.get("roles"):
        raise ValueError("Search config must contain at least one target role")
    data.setdefault("skills", [])
    data.setdefault("locations", ["India", "Remote"])
    data.setdefault("exclude_title_terms", ["senior", "staff", "principal", "lead", "manager", "director"])
    data.setdefault("exclude_description_terms", ["unpaid internship", "unpaid role", "volunteer position"])
    data.setdefault("allow_unknown_experience", True)
    return data
