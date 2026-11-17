from __future__ import annotations

import logging
import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, HttpUrl, ValidationError, field_validator

from ..db import JobRepository
from ..models import Job
from ..utils import clean_html, is_public_https_url, normalize_url

logger = logging.getLogger("jobpilot.discovery.talivy")


class TalivySearchClient(Protocol):
    """Injection point for a client implemented against Talivy's verified API contract."""

    def search(self, query: str, *, limit: int) -> list[dict[str, Any]]: ...


class OriginalPostingResolver(Protocol):
    """Optional, policy-aware retriever for full descriptions from source job pages."""

    def get_job_description(self, url: str) -> str | None: ...


class TalivyIntegrationUnavailable(RuntimeError):
    pass


class TalivySearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    result_type: Literal["job", "career_page"]
    url: HttpUrl
    title: str | None = None
    company: str | None = None
    description: str | None = None
    description_complete: bool = False
    snippet: str | None = None
    source_job_id: str | None = None

    @field_validator("url")
    @classmethod
    def validate_external_url(cls, value: HttpUrl) -> HttpUrl:
        if not is_public_https_url(str(value)):
            raise ValueError("Talivy results must point to a public HTTPS URL")
        return value


class CareerPageLead(BaseModel):
    company_name: str
    career_page_url: HttpUrl
    discovery_source: str = "talivy"

    @field_validator("career_page_url")
    @classmethod
    def validate_external_url(cls, value: HttpUrl) -> HttpUrl:
        if not is_public_https_url(str(value)):
            raise ValueError("Career page URL must be a public HTTPS URL")
        return value


class SearchQueryGenerator:
    def __init__(self, search_config: dict[str, Any], max_queries: int = 10):
        self.search_config = search_config
        self.max_queries = max(1, max_queries)

    def generate(self) -> list[str]:
        roles = self.search_config.get("roles", [])
        locations = self.search_config.get("locations", ["India", "Remote"])
        talivy_config = self.search_config.get("talivy", {})
        terms = talivy_config.get(
            "search_terms",
            ["fresher", "graduate", "entry-level", "0-1 years"],
        )
        if not roles or not locations or not terms:
            return []
        queries: list[str] = []
        seen: set[str] = set()
        for index in range(self.max_queries):
            role = roles[index % len(roles)]
            role_name = role["name"] if isinstance(role, dict) else str(role)
            location = locations[(index // len(terms)) % len(locations)]
            experience = terms[index % len(terms)]
            query = f"{role_name} {experience} {location}".strip()
            normalized = " ".join(query.casefold().split())
            if normalized not in seen:
                seen.add(normalized)
                queries.append(query)
        return queries


class TalivyAdapter:
    name = "talivy"

    def __init__(
        self,
        client: TalivySearchClient,
        repository: JobRepository,
        search_config: dict[str, Any],
        *,
        max_queries: int = 10,
        max_results_per_query: int = 20,
        cache_ttl_hours: int = 24,
        request_delay_seconds: float = 1.0,
        original_posting_resolver: OriginalPostingResolver | None = None,
    ):
        self.client = client
        self.repository = repository
        self.query_generator = SearchQueryGenerator(search_config, max_queries)
        self.queries = self.query_generator.generate()
        self.max_results_per_query = max(1, max_results_per_query)
        self.cache_ttl_hours = max(1, cache_ttl_hours)
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.original_posting_resolver = original_posting_resolver
        self.failed_queries = 0

    def search(self) -> list[TalivySearchResult]:
        self.failed_queries = 0
        results: list[TalivySearchResult] = []
        for query in self.queries:
            cached = self.repository.get_search_query_cache("talivy", query)
            if cached is not None:
                payload = cached
            else:
                try:
                    payload = self.client.search(query, limit=self.max_results_per_query)
                    if not isinstance(payload, list):
                        raise ValueError("Talivy adapter contract requires a list of search-result objects")
                    payload = payload[:self.max_results_per_query]
                except Exception:
                    self.failed_queries += 1
                    logger.exception("Talivy query failed: %s", query)
                    continue
                self.repository.set_search_query_cache(
                    "talivy", query, payload, self.cache_ttl_hours
                )
                if self.request_delay_seconds:
                    time.sleep(self.request_delay_seconds)
            for item in payload:
                try:
                    results.append(TalivySearchResult.model_validate(item))
                except ValidationError:
                    logger.warning("Skipping malformed Talivy result for query: %s", query)
        return results

    def fetch_jobs(self) -> list[Job]:
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        for result in self.search():
            if result.result_type != "job":
                continue
            description = result.description if result.description_complete else None
            if not description and self.original_posting_resolver is not None:
                description = self.original_posting_resolver.get_job_description(str(result.url))
            if (
                not description
                or not result.title
                or not result.company
            ):
                continue
            canonical_url = normalize_url(str(result.url))
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            jobs.append(Job(
                source="talivy",
                source_job_id=result.source_job_id,
                title=result.title,
                company=result.company,
                description=clean_html(description),
                apply_url=result.url,
                source_url=result.url,
                raw_data=result.model_dump(mode="json"),
            ))
        return jobs

    def career_page_leads(self) -> list[CareerPageLead]:
        leads: list[CareerPageLead] = []
        seen: set[str] = set()
        for result in self.search():
            if result.result_type != "career_page" or not result.company:
                continue
            url = normalize_url(str(result.url))
            if url not in seen:
                seen.add(url)
                leads.append(CareerPageLead(
                    company_name=result.company,
                    career_page_url=result.url,
                ))
        return leads
