from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SelectOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    disabled: bool = False


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    tag: Literal["input", "textarea", "select"]
    field_type: str = "text"
    name: str | None = None
    field_id: str | None = None
    label: str | None = None
    group_label: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    required: bool = False
    accept: str | None = None
    value: str | None = None
    options: list[SelectOption] = Field(default_factory=list)

    @property
    def semantic_text(self) -> str:
        descriptive = [
            value for value in (
                self.group_label, self.label, self.aria_label, self.placeholder
            ) if value
        ]
        if not descriptive:
            descriptive = [value for value in (self.name, self.field_id) if value]
        return " ".join(dict.fromkeys(descriptive))


class SiteInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["greenhouse", "lever", "company", "unsupported"]
    supported: bool
    reason: str | None = None


class MappedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    source: Literal["profile", "answer_bank"]
    verified_answer_id: str | None = None


class FormFillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filled_fields: list[str] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    manual_fields: list[str] = Field(default_factory=list)
    resume_uploaded: str | None = None


class SubmissionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool
    reference: str | None = None
    reason: str
