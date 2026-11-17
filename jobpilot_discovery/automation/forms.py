from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page

from ..answers import AnswerBank, VerifiedAnswer
from ..profile import CandidateProfile
from .exceptions import AutomationSafetyError, UnsupportedApplicationSite
from .models import FormField, FormFillResult, MappedField, SiteInfo

_FIELD_SCRIPT = r"""
elements => elements.map((el, index) => {
  const text = node => node ? (node.innerText || node.textContent || '').trim() : '';
  const labels = Array.from(el.labels || []).map(text).filter(Boolean);
  const labelledBy = (el.getAttribute('aria-labelledby') || '')
    .split(/\s+/).map(id => text(document.getElementById(id))).filter(Boolean);
  const fieldset = el.closest('fieldset');
  const legend = fieldset ? text(fieldset.querySelector('legend')) : '';
  const parentLabel = el.closest('label');
  const options = el.tagName === 'SELECT'
    ? Array.from(el.options).map(option => ({
        label: text(option), value: option.value, disabled: option.disabled
      }))
    : [];
  return {
    index,
    tag: el.tagName.toLowerCase(),
    field_type: (el.type || el.tagName).toLowerCase(),
    name: el.name || null,
    field_id: el.id || null,
    label: labels[0] || text(parentLabel) || labelledBy.join(' ') || null,
    group_label: legend || null,
    placeholder: el.getAttribute('placeholder'),
    aria_label: el.getAttribute('aria-label'),
    required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
    accept: el.getAttribute('accept'),
    value: el.value || null,
    options
  };
})
"""

_SENSITIVE_OR_LEGAL = (
    "legally authorized", "work authorization", "visa", "sponsorship",
    "agree to", "accept terms", "criminal history", "background check",
    "disability", "veteran status", "gender", "ethnicity", "race",
    "equal opportunity", "demographic", "date of birth",
)


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", value.casefold()).split())


def is_unverified_sensitive_field(field: FormField) -> bool:
    text = _normalize(field.semantic_text)
    return any(_normalize(term) in text for term in _SENSITIVE_OR_LEGAL)


class ApplicationSiteDetector:
    def detect(self, page: Page, url: str) -> SiteInfo:
        host = (urlsplit(url).hostname or "").casefold()
        if host == "jobs.lever.co" or host.endswith(".jobs.lever.co"):
            return SiteInfo(platform="lever", supported=True)
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
            return SiteInfo(platform="greenhouse", supported=True)
        if page.locator("form").count() and page.locator("input, textarea, select").count():
            return SiteInfo(platform="company", supported=True)
        return SiteInfo(
            platform="unsupported",
            supported=False,
            reason="No supported job application form was detected.",
        )

    def require_supported(self, page: Page, url: str) -> SiteInfo:
        site = self.detect(page, url)
        if not site.supported:
            raise UnsupportedApplicationSite(site.reason or "Unsupported application site")
        return site


class FormAnalyzer:
    def analyze(self, page: Page) -> list[FormField]:
        raw_fields = page.locator(
            "input:not([type='hidden']), textarea, select"
        ).evaluate_all(_FIELD_SCRIPT)
        fields: list[FormField] = []
        for raw in raw_fields:
            if not isinstance(raw, dict):
                raise AutomationSafetyError("The page returned malformed form field metadata")
            fields.append(FormField.model_validate(raw))
        return fields


class FieldMapper:
    def __init__(self, profile: CandidateProfile, answer_bank: AnswerBank):
        self.profile = profile
        self.answer_bank = answer_bank

    def map(self, field: FormField) -> MappedField | None:
        text = _normalize(field.semantic_text)
        if not text or is_unverified_sensitive_field(field):
            return None

        profile_value = self._profile_value(text)
        if profile_value:
            return MappedField(value=profile_value, source="profile")

        answer = self.answer_bank.exact_match(field.semantic_text)
        if answer is not None:
            return MappedField(
                value=answer.answer,
                source="answer_bank",
                verified_answer_id=answer.id,
            )
        return None

    def _profile_value(self, text: str) -> str | None:
        full_name = self.profile.full_name
        if full_name and text in {"name", "full name", "applicant name"}:
            return full_name
        if full_name and text in {"first name", "given name"}:
            return full_name.split()[0]
        if full_name and text in {"last name", "surname", "family name"}:
            parts = full_name.split()
            return parts[-1] if len(parts) > 1 else None
        direct_values = (
            (("email", "e mail"), self.profile.email),
            (("phone", "mobile", "telephone"), self.profile.phone),
            (("current location", "city"), self.profile.location),
            (("linkedin",), str(self.profile.linkedin_url) if self.profile.linkedin_url else None),
            (("github",), str(self.profile.github_url) if self.profile.github_url else None),
            (("portfolio", "personal website"), str(self.profile.portfolio_url) if self.profile.portfolio_url else None),
            (("degree", "education"), self.profile.education.degree),
            (("university", "college", "institution"), self.profile.education.institution),
            (("graduation year", "year of graduation"), str(self.profile.education.graduation_year)
                if self.profile.education.graduation_year else None),
            (("technical skills", "skills"), ", ".join(self.profile.technical_skills)
                if self.profile.technical_skills else None),
            (("project",), ", ".join(self.profile.projects) if self.profile.projects else None),
            (("certification",), ", ".join(self.profile.certifications)
                if self.profile.certifications else None),
            (("internship experience",), self._internship_summary()),
            (("cover letter",), self.profile.cover_letter_template),
        )
        for terms, value in direct_values:
            if any(_normalize(term) in text for term in terms) and value:
                return value
        return None

    def _internship_summary(self) -> str | None:
        if not self.profile.internship_experience:
            return None
        return "\n".join(
            f"{item.title} at {item.organization}: {item.description}".strip()
            for item in self.profile.internship_experience
        )

    def verified_answer_for_group(self, question: str) -> VerifiedAnswer | None:
        if any(_normalize(term) in _normalize(question) for term in _SENSITIVE_OR_LEGAL):
            return None
        return self.answer_bank.exact_match(question)


class ResumeUploader:
    _SUPPORTED = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    def upload(self, locator, configured_paths: set[Path], accept: str | None = None) -> Path:
        resolved_paths = {path.expanduser().resolve() for path in configured_paths}
        if len(resolved_paths) != 1:
            raise AutomationSafetyError("Choose exactly one configured resume path for this application")
        resume_path = next(iter(resolved_paths))
        if not resume_path.is_file():
            raise AutomationSafetyError("The configured resume file does not exist")
        extension = resume_path.suffix.casefold()
        mime_type = self._SUPPORTED.get(extension)
        if mime_type is None:
            raise AutomationSafetyError("Only configured PDF and DOCX resumes are supported")
        if accept:
            accepted = {item.strip().casefold() for item in accept.split(",") if item.strip()}
            if not any(
                item in {extension, mime_type}
                or (item.startswith(".") and item == extension)
                or (item.endswith("/*") and mime_type.startswith(item[:-1]))
                for item in accepted
            ):
                raise AutomationSafetyError("The application form does not accept the configured resume format")
        locator.set_input_files(str(resume_path))
        selected_name = locator.evaluate("(el) => el.files?.[0]?.name || ''")
        if selected_name != resume_path.name:
            raise AutomationSafetyError("Resume upload selection could not be verified")
        return resume_path


class FormFiller:
    def __init__(self, mapper: FieldMapper, resume_uploader: ResumeUploader | None = None):
        self.mapper = mapper
        self.resume_uploader = resume_uploader or ResumeUploader()

    def fill(
        self, page: Page, fields: list[FormField], *, resume_category: str | None = None
    ) -> FormFillResult:
        result = FormFillResult()
        controls = page.locator("input:not([type='hidden']), textarea, select")
        processed_radio_groups: set[str] = set()

        for field in fields:
            semantic_label = field.semantic_text.strip() or f"Field {field.index + 1}"
            if field.field_type == "file":
                resume_path = self.configured_resume(resume_category)
                if resume_path is None:
                    if field.required:
                        result.missing_required_fields.append(semantic_label)
                    result.manual_fields.append(semantic_label)
                    continue
                self.resume_uploader.upload(
                    controls.nth(field.index),
                    {resume_path},
                    field.accept,
                )
                result.filled_fields.append(semantic_label)
                result.resume_uploaded = resume_path.name
                continue

            if field.field_type == "radio":
                group = field.name or field.group_label or str(field.index)
                if group in processed_radio_groups:
                    continue
                processed_radio_groups.add(group)
                answer = self.mapper.verified_answer_for_group(
                    field.group_label or field.semantic_text
                )
                if answer is None:
                    if field.required:
                        result.missing_required_fields.append(field.group_label or semantic_label)
                    result.manual_fields.append(field.group_label or semantic_label)
                    continue
                group_fields = [
                    item for item in fields
                    if item.field_type == "radio"
                    and (
                        item.name == field.name if field.name
                        else item.group_label == field.group_label
                    )
                ]
                choice = next(
                    (item for item in group_fields
                     if _normalize(item.label or item.value or "") == _normalize(answer.answer)),
                    None,
                )
                if choice is None:
                    if field.required:
                        result.missing_required_fields.append(field.group_label or semantic_label)
                    result.manual_fields.append(field.group_label or semantic_label)
                    continue
                controls.nth(choice.index).check()
                result.filled_fields.append(field.group_label or semantic_label)
                continue

            mapping = self.mapper.map(field)
            if mapping is None:
                if field.required:
                    result.missing_required_fields.append(semantic_label)
                if field.required or field.field_type in {"checkbox", "password"}:
                    result.manual_fields.append(semantic_label)
                continue

            locator = controls.nth(field.index)
            if field.tag == "select":
                option = self._select_option(field, mapping.value)
                if option is None:
                    if field.required:
                        result.missing_required_fields.append(semantic_label)
                    result.manual_fields.append(semantic_label)
                    continue
                locator.select_option(value=option.value)
            elif field.field_type == "checkbox":
                if _normalize(mapping.value) not in {"yes", "true", "i agree"}:
                    if field.required:
                        result.missing_required_fields.append(semantic_label)
                    result.manual_fields.append(semantic_label)
                    continue
                locator.check()
            else:
                locator.fill(mapping.value)
            result.filled_fields.append(semantic_label)
        return result

    @staticmethod
    def missing_required_fields(page: Page, fields: list[FormField]) -> list[str]:
        controls = page.locator("input:not([type='hidden']), textarea, select")
        missing: list[str] = []
        seen_radio_groups: set[str] = set()
        for field in fields:
            if not field.required:
                continue
            label = field.semantic_text.strip() or f"Field {field.index + 1}"
            locator = controls.nth(field.index)
            if field.field_type == "radio":
                group = field.name or field.group_label or str(field.index)
                if group in seen_radio_groups:
                    continue
                seen_radio_groups.add(group)
                satisfied = locator.evaluate("""el => {
                    const scope = el.form || document;
                    return Array.from(scope.querySelectorAll('input[type="radio"]'))
                      .some(item => item.name === el.name && item.checked);
                }""")
            elif field.field_type == "checkbox":
                satisfied = locator.is_checked()
            elif field.field_type == "file":
                satisfied = bool(locator.evaluate("(el) => el.files?.length"))
            else:
                satisfied = bool(locator.evaluate("""el => {
                    if (el.tagName === 'SELECT') return Boolean(el.value);
                    return Boolean((el.value || '').trim());
                }"""))
            if not satisfied:
                missing.append(field.group_label or label)
        return list(dict.fromkeys(missing))

    def configured_resume(self, category: str | None = None) -> Path | None:
        normalized_category = _normalize(category or "")
        selected = next(
            (path for key, path in self.mapper.profile.resume_variants.items()
             if normalized_category and _normalize(key) in normalized_category),
            self.mapper.profile.resume_path,
        )
        if selected is None:
            return None
        return selected

    @staticmethod
    def _select_option(field: FormField, value: str):
        target = _normalize(value)
        return next(
            (
                option for option in field.options
                if not option.disabled
                and (
                    _normalize(option.label) == target
                    or _normalize(option.value) == target
                )
            ),
            None,
        )
