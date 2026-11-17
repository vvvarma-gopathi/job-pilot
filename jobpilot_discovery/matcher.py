from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import Job


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).strip()


def _contains(text: str, phrase: str) -> bool:
    haystack = f" {_normal(text)} "
    needle = f" {_normal(phrase)} "
    return bool(needle.strip()) and needle in haystack


@dataclass(frozen=True)
class MatchResult:
    relevant: bool
    score: int
    matched_role: str | None
    experience_status: str
    reasons: list[str]
    rejection_reason: str | None = None


class JobMatcher:
    """Transparent rule-based first-pass filter; Gemini can refine this later."""

    def __init__(self, config: dict, minimum_score: int = 40):
        self.roles = config["roles"]
        self.skills = config.get("skills", [])
        self.skill_weights = config.get("skill_weights", {})
        self.locations = config.get("locations", ["India", "Remote"])
        self.exclude_titles = config.get("exclude_title_terms", [])
        self.exclude_description = config.get("exclude_description_terms", [])
        self.suspicious_url_hosts = config.get("suspicious_url_hosts", [])
        self.allow_unknown_experience = bool(config.get("allow_unknown_experience", True))
        self.minimum_score = minimum_score

    def evaluate(self, job: Job) -> MatchResult:
        title_text = job.title
        full_text = f"{job.title} {job.description} {' '.join(job.tags)}"
        reasons: list[str] = []

        parsed_url = urlsplit(str(job.apply_url))
        host = (parsed_url.hostname or "").casefold()
        if parsed_url.scheme != "https" or any(
            host == blocked.casefold() or host.endswith("." + blocked.casefold())
            for blocked in self.suspicious_url_hosts
        ):
            return MatchResult(False, 0, None, "rejected", [], "Application URL is insecure or uses a URL shortener")

        excluded_title = next((term for term in self.exclude_titles if _contains(title_text, term)), None)
        if excluded_title:
            return MatchResult(False, 0, None, "rejected", [], f"Excluded seniority/title term: {excluded_title}")

        excluded_desc = next((term for term in self.exclude_description if _contains(job.description, term)), None)
        if excluded_desc is None and re.search(
            r"\b(unpaid|without (?:pay|compensation)|no compensation|volunteer(?:ing)?)\b",
            job.description,
            re.IGNORECASE,
        ):
            excluded_desc = "unpaid or volunteer opportunity"
        if re.search(r"\b(unpaid|volunteer)\b", job.employment_type or "", re.IGNORECASE):
            excluded_desc = "unpaid or volunteer opportunity"
        if excluded_desc:
            return MatchResult(False, 0, None, "rejected", [], f"Excluded role condition: {excluded_desc}")

        role_hits: list[tuple[int, str, str]] = []
        for role in self.roles:
            role_name = role["name"]
            aliases = role.get("aliases", [role_name])
            title_hit = any(_contains(title_text, alias) for alias in aliases)
            body_hit = any(_contains(full_text, alias) for alias in aliases)
            if title_hit or body_hit:
                role_hits.append((3 if title_hit else 1, role_name, "title" if title_hit else "description"))
        if not role_hits:
            return MatchResult(False, 0, None, "unknown", [], "No target-role match")
        role_hits.sort(key=lambda hit: hit[0], reverse=True)
        matched_role = role_hits[0][1]
        title_role_match = role_hits[0][2] == "title"
        score = 35 if title_role_match else 18
        reasons.append(f"Target role match: {matched_role}")

        skill_hits = [skill for skill in self.skills if _contains(full_text, skill)]
        skill_score = min(40, sum(max(1, int(self.skill_weights.get(skill, 1))) * 4 for skill in skill_hits))
        score += skill_score
        if skill_hits:
            reasons.append("Matching skills: " + ", ".join(skill_hits[:8]))

        # Prefer India/remote jobs. Do not discard a listing merely because location is vague.
        location_text = f"{job.location or ''} {job.workplace_type or ''}"
        location_terms = [loc for loc in self.locations if _normal(loc) != "remote"]
        location_match = any(_contains(location_text, loc) for loc in location_terms)
        remote_only = _normal(job.location or "") in {"remote", "remote only", "work from home"}
        remote_only = remote_only or (
            not job.location and _contains(job.workplace_type or "", "remote")
        )
        if location_match or remote_only:
            score += 15
            reasons.append("Location matches preference")
        elif not job.location:
            score += 5
            reasons.append("Location not specified; retained for review")
        else:
            # The initial feed mix includes global remote and Europe-oriented data.
            # Retain potentially remote roles only if they explicitly say remote.
            return MatchResult(False, score, matched_role, "unknown", reasons, "Location does not match India/Remote preference")

        experience_status = self._experience_status(full_text)
        if experience_status == "two_plus_years":
            return MatchResult(False, score, matched_role, experience_status, reasons, "Requires 2+ years of experience")
        if experience_status == "fresher":
            score += 10
            reasons.append("Fresher / entry-level language found")
        elif experience_status == "one_year_or_less":
            score += 8
            reasons.append("Experience requirement is 0–1 years")
        else:
            reasons.append("Experience requirement not stated; retained for review" if self.allow_unknown_experience else "Experience requirement unknown")
            if not self.allow_unknown_experience:
                return MatchResult(False, score, matched_role, experience_status, reasons, "Unknown experience requirement")

        score = min(100, score)
        return MatchResult(score >= self.minimum_score, score, matched_role, experience_status, reasons,
                           None if score >= self.minimum_score else f"Match score below {self.minimum_score}")

    @staticmethod
    def _experience_status(text: str) -> str:
        raw = text.casefold()
        normalized = _normal(text)
        patterns = (
            r"\b(?:2|3|4|5|two|three|four|five)\s*(?:\+|plus|or more|-|to)\s*(?:\d+\s*)?(?:years?|yrs?)\b",
            r"\b(?:at least|minimum(?: of)?)\s+(?:2|3|4|5|two|three|four|five)\s*(?:years?|yrs?)\b",
            r"\b(?:2|3|4|5|two|three|four|five)\s*(?:years?|yrs?)\s+(?:of\s+)?experience\b",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, raw):
                qualifier = raw[max(0, match.start() - 20):match.start()] + raw[match.end():match.end() + 32]
                if not re.search(r"\b(preferred|preferably|desired|nice to have|bonus|advantage)\b", qualifier):
                    return "two_plus_years"
        fresher_terms = ["fresher", "freshers", "entry level", "entrylevel", "new grad", "graduate role", "no experience", "zero experience"]
        if any(_contains(normalized, term) for term in fresher_terms):
            return "fresher"
        if re.search(r"\b(?:0|zero|one|1)\s*(?:-|to)\s*(?:1|one)\s*(?:years?|yrs?)\b", raw):
            return "one_year_or_less"
        return "unknown"
