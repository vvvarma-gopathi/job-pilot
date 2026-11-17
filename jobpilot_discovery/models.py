from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from pydantic import field_validator

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """Normalized job posting shared by every discovery source."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    source: str
    source_job_id: str | None = None
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str | None = None
    description: str = ""
    apply_url: HttpUrl
    source_url: HttpUrl | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    posted_at: datetime | None = None
    department: str | None = None
    tags: list[str] = Field(default_factory=list)
    match_score: int = 0
    matched_role: str | None = None
    experience_status: str = "unknown"
    match_reasons: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
    eligibility_status: str = "pending"
    processing_status: str = "pending_ai"
    raw_data: dict[str, Any] = Field(default_factory=dict, exclude=True)
    discovered_at: datetime = Field(default_factory=utc_now)

    @field_validator("apply_url", "source_url")
    @classmethod
    def validate_public_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme not in {"http", "https"}:
            raise ValueError("Job URLs must use HTTP or HTTPS")
        return value


class SourceRunResult(BaseModel):
    source: str
    fetched: int = 0
    relevant: int = 0
    inserted: int = 0
    updated: int = 0
    filtered_out: int = 0
    failed: bool = False
    skipped: bool = False
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
