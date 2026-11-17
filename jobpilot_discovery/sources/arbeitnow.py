from __future__ import annotations

from pydantic import HttpUrl, TypeAdapter, ValidationError

from ..models import Job
from ..utils import clean_html, normalize_url, parse_datetime
from .base import BaseSource, JsonFetcher


class ArbeitnowSource(BaseSource):
    name = "arbeitnow"
    URL = "https://www.arbeitnow.com/api/job-board-api"
    url_adapter = TypeAdapter(HttpUrl)

    def __init__(self, fetcher: JsonFetcher, max_pages: int = 10):
        super().__init__(fetcher)
        self.max_pages = max_pages

    def fetch_jobs(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            payload = self.fetcher.get_json(self.URL, params={"page": page})
            items = payload.get("data", []) if isinstance(payload, dict) else []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                url, title, company = item.get("url"), item.get("title"), item.get("company_name")
                if not (
                    isinstance(url, str) and url.strip()
                    and isinstance(title, str) and title.strip()
                    and isinstance(company, str) and company.strip()
                ):
                    continue
                job_types = item.get("job_types") or []
                try:
                    canonical_url = normalize_url(url)
                    source_id = str(item.get("slug") or item.get("id") or canonical_url)
                    if source_id in seen or canonical_url in seen:
                        continue
                    job = Job(
                        source=self.name, source_job_id=source_id,
                        title=title, company=company, location=item.get("location"),
                        description=clean_html(item.get("description")),
                        apply_url=self.url_adapter.validate_python(url),
                        source_url=self.url_adapter.validate_python(url),
                        employment_type=job_types[0] if isinstance(job_types, list) and job_types else None,
                        workplace_type="remote" if item.get("remote") else None,
                        posted_at=parse_datetime(item.get("created_at")),
                        tags=[
                            tag for tag in item.get("tags", [])
                            if isinstance(tag, str)
                        ] if isinstance(item.get("tags"), list) else [],
                        raw_data=item,
                    )
                except (TypeError, ValueError, ValidationError):
                    continue
                seen.update((source_id, canonical_url))
                jobs.append(job)
            links = payload.get("links")
            if not isinstance(links, dict) or not links.get("next"):
                break
        return jobs
