from __future__ import annotations

from pydantic import HttpUrl, TypeAdapter, ValidationError

from ..models import Job
from ..utils import clean_html, normalize_url, parse_datetime
from .base import BaseSource


class RemotiveSource(BaseSource):
    name = "remotive"
    URL = "https://remotive.com/api/remote-jobs"
    url_adapter = TypeAdapter(HttpUrl)

    def fetch_jobs(self) -> list[Job]:
        payload = self.fetcher.get_json(self.URL)
        jobs: list[Job] = []
        seen: set[str] = set()
        for item in payload.get("jobs", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            url, title, company = item.get("url"), item.get("title"), item.get("company_name")
            if not (
                isinstance(url, str) and url.strip()
                and isinstance(title, str) and title.strip()
                and isinstance(company, str) and company.strip()
            ):
                continue
            try:
                canonical_url = normalize_url(url)
                stable_id = str(item.get("id")) if item.get("id") is not None else canonical_url
                if stable_id in seen or canonical_url in seen:
                    continue
                job = Job(
                    source=self.name, source_job_id=stable_id, title=title, company=company,
                    location=item.get("candidate_required_location") or "Remote",
                    description=clean_html(item.get("description")),
                    apply_url=self.url_adapter.validate_python(url),
                    source_url=self.url_adapter.validate_python(url),
                    employment_type=item.get("job_type"), workplace_type="remote",
                    posted_at=parse_datetime(item.get("publication_date")), department=item.get("category"),
                    tags=[item["category"]] if item.get("category") else [], raw_data=item,
                )
            except (TypeError, ValueError, ValidationError):
                continue
            seen.update((stable_id, canonical_url))
            jobs.append(job)
        return jobs
