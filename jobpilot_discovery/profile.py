from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class CandidateEducation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    degree: str | None = None
    institution: str | None = None
    graduation_year: int | None = Field(default=None, ge=1900, le=2200)


class InternshipExperience(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization: str
    title: str
    description: str = ""
    start_date: str | None = None
    end_date: str | None = None


class CandidateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: HttpUrl | None = None
    github_url: HttpUrl | None = None
    portfolio_url: HttpUrl | None = None
    education: CandidateEducation = Field(default_factory=CandidateEducation)
    technical_skills: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    internship_experience: list[InternshipExperience] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    resume_path: Path | None = None
    resume_variants: dict[str, Path] = Field(default_factory=dict)
    cover_letter_template: str | None = None

    @field_validator(
        "full_name", "email", "phone", "location", "cover_letter_template",
        mode="before",
    )
    @classmethod
    def normalize_empty_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be a valid email address")
        return value

    @field_validator("linkedin_url", "github_url", "portfolio_url")
    @classmethod
    def require_https_profile_urls(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("Profile URLs must use HTTPS")
        return value


def load_candidate_profile(
    path: str | Path, search_config: dict[str, Any]
) -> CandidateProfile:
    candidate = search_config.get("candidate", {})
    defaults = CandidateProfile(
        education=CandidateEducation(
            degree=candidate.get("education"),
            graduation_year=candidate.get("graduation_year"),
        ),
        technical_skills=search_config.get("skills", []),
        projects=candidate.get("projects", []),
    ).model_dump(mode="json")

    profile_path = Path(path)
    if not profile_path.exists():
        return CandidateProfile.model_validate(defaults)

    with profile_path.open("r", encoding="utf-8") as handle:
        supplied = json.load(handle)
    if not isinstance(supplied, dict):
        raise ValueError("Candidate profile must be a JSON object")
    defaults.update(supplied)
    return CandidateProfile.model_validate(defaults)
