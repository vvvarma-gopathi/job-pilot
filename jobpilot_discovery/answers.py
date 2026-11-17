from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerifiedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    category: str = Field(min_length=1)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    verification_status: Literal["verified", "unverified"] = "unverified"
    auto_reusable: bool = False

    @model_validator(mode="after")
    def require_verification_for_automatic_reuse(self) -> "VerifiedAnswer":
        if self.auto_reusable and self.verification_status != "verified":
            raise ValueError("Only verified answers can be marked for automatic reuse")
        return self


class AnswerBank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[VerifiedAnswer] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "AnswerBank":
        answer_path = Path(path)
        if not answer_path.exists():
            return cls()
        with answer_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            data = {"answers": data}
        return cls.model_validate(data)

    def exact_match(
        self, question: str, *, category: str | None = None
    ) -> VerifiedAnswer | None:
        normalized = _normalize_question(question)
        matches = [
            answer for answer in self.answers
            if answer.verification_status == "verified"
            and answer.auto_reusable
            and _normalize_question(answer.question) == normalized
            and (category is None or answer.category.casefold() == category.casefold())
        ]
        return matches[0] if len(matches) == 1 else None


def _normalize_question(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.casefold())).strip()
