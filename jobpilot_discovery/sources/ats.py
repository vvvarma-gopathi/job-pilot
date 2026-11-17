from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError, field_validator

from ..http import FetchError
from ..models import Job
from ..utils import clean_html, is_public_https_url, normalize_url, parse_datetime
from .base import BaseSource

logger = logging.getLogger("jobpilot.discovery.ats")
URL_ADAPTER = TypeAdapter(HttpUrl)


class ATSBoard(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    provider: Literal["greenhouse", "lever"]
    board_identifier: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    company_name: str = Field(min_length=1)
    career_page_url: HttpUrl
    discovery_source: str = "talivy"

    @field_validator("career_page_url")
    @classmethod
    def validate_career_url(cls, value: HttpUrl) -> HttpUrl:
        if not is_public_https_url(str(value)):
            raise ValueError("Career page URL must be a public HTTPS URL")
        return value


class ATSBoardAdapter:
    provider: str

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.last_validation_error = False

    @classmethod
    def identify(cls, url: str) -> tuple[Literal["greenhouse", "lever"], str] | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        segments = [part for part in parts.path.split("/") if part]
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and segments:
            return "greenhouse", segments[0]
        if host == "jobs.lever.co" and segments:
            return "lever", segments[0]
        return None

    def validate(self, board: ATSBoard) -> bool:
        self.last_validation_error = False
        try:
            payload = self.fetcher.get_json(self.endpoint(board.board_identifier))
        except FetchError as exc:
            self.last_validation_error = True
            logger.info("Board validation failed for %s/%s: %s", self.provider, board.board_identifier, exc)
            return False
        return self.has_postings_shape(payload)

    def fetch_jobs(self, board: ATSBoard) -> list[Job]:
        payload = self.fetcher.get_json(self.endpoint(board.board_identifier))
        return self.normalize_jobs(board, payload)

    def endpoint(self, identifier: str) -> str:
        raise NotImplementedError

    def has_postings_shape(self, payload: Any) -> bool:
        raise NotImplementedError

    def normalize_jobs(self, board: ATSBoard, payload: Any) -> list[Job]:
        raise NotImplementedError

    @staticmethod
    def _to_job(board: ATSBoard, item: dict[str, Any], *, title: Any, url: Any, description: Any,
                location: Any, posted_at: Any, employment_type: Any = None,
                workplace_type: Any = None, tags: list[str] | None = None,
                source_url: Any = None) -> Job | None:
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.strip():
            return None
        try:
            apply_url = URL_ADAPTER.validate_python(url)
            original_url = URL_ADAPTER.validate_python(source_url if isinstance(source_url, str) else url)
            return Job(
                source=board.provider,
                source_job_id=str(item.get("id")) if item.get("id") is not None else normalize_url(url),
                title=title,
                company=board.company_name,
                location=location if isinstance(location, str) else None,
                description=clean_html(description if isinstance(description, str) else ""),
                apply_url=apply_url,
                source_url=original_url,
                employment_type=employment_type if isinstance(employment_type, str) else None,
                workplace_type=workplace_type if isinstance(workplace_type, str) else None,
                posted_at=parse_datetime(posted_at),
                tags=tags or [],
                raw_data=item,
            )
        except (TypeError, ValueError, ValidationError):
            return None


class GreenhouseBoardAdapter(ATSBoardAdapter):
    provider = "greenhouse"

    def endpoint(self, identifier: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs?content=true"

    def has_postings_shape(self, payload: Any) -> bool:
        return isinstance(payload, dict) and isinstance(payload.get("jobs"), list)

    def normalize_jobs(self, board: ATSBoard, payload: Any) -> list[Job]:
        if not self.has_postings_shape(payload):
            return []
        jobs: list[Job] = []
        for item in payload["jobs"]:
            if not isinstance(item, dict):
                continue
            location = item.get("location")
            department = item.get("departments") or []
            tags: list[str] = []
            if isinstance(department, list):
                for value in department:
                    if isinstance(value, dict) and isinstance(value.get("name"), str):
                        tags.append(value["name"])
            job = self._to_job(
                board, item,
                title=item.get("title"),
                url=item.get("absolute_url"),
                description=item.get("content"),
                location=location.get("name") if isinstance(location, dict) else location,
                posted_at=item.get("updated_at"),
                tags=tags,
            )
            if job:
                jobs.append(job)
        return jobs


class LeverBoardAdapter(ATSBoardAdapter):
    provider = "lever"

    def endpoint(self, identifier: str) -> str:
        return f"https://api.lever.co/v0/postings/{identifier}?mode=json"

    def has_postings_shape(self, payload: Any) -> bool:
        return isinstance(payload, list) and all(isinstance(item, dict) for item in payload)

    def normalize_jobs(self, board: ATSBoard, payload: Any) -> list[Job]:
        if not self.has_postings_shape(payload):
            return []
        jobs: list[Job] = []
        for item in payload:
            categories = item.get("categories") or {}
            if not isinstance(categories, dict):
                categories = {}
            job = self._to_job(
                board, item,
                title=item.get("text"),
                url=item.get("applyUrl") or item.get("hostedUrl"),
                source_url=item.get("hostedUrl") or item.get("applyUrl"),
                description=item.get("descriptionPlain") or item.get("description"),
                location=categories.get("location"),
                posted_at=item.get("createdAt"),
                employment_type=categories.get("commitment"),
                workplace_type=item.get("workplaceType"),
                tags=[categories["team"]] if isinstance(categories.get("team"), str) else [],
            )
            if job:
                jobs.append(job)
        return jobs


class ATSBoardSource(BaseSource):
    def __init__(self, fetcher, board: ATSBoard):
        super().__init__(fetcher)
        self.board = board
        self.name = board.provider
        self.adapter = (
            GreenhouseBoardAdapter(fetcher) if board.provider == "greenhouse"
            else LeverBoardAdapter(fetcher)
        )

    def fetch_jobs(self) -> list[Job]:
        return self.adapter.fetch_jobs(self.board)


def discover_board_candidates(
    urls: list[str], *, company_name: str, career_page_url: str, discovery_source: str
) -> list[ATSBoard]:
    candidates: list[ATSBoard] = []
    seen: set[tuple[str, str]] = set()
    for url in urls:
        identified = ATSBoardAdapter.identify(url)
        if identified is None or identified in seen:
            continue
        provider, identifier = identified
        try:
            candidates.append(ATSBoard(
                provider=provider,
                board_identifier=identifier,
                company_name=company_name,
                career_page_url=URL_ADAPTER.validate_python(career_page_url),
                discovery_source=discovery_source,
            ))
        except ValidationError:
            continue
        seen.add(identified)
    return candidates


class ATSBoardDiscovery:
    def __init__(self, repository, fetcher, user_agent: str, request_delay_seconds: float = 1.0):
        self.repository = repository
        self.fetcher = fetcher
        self.user_agent = user_agent
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.adapters = {
            "greenhouse": GreenhouseBoardAdapter(fetcher),
            "lever": LeverBoardAdapter(fetcher),
        }
        self.robots_cache: dict[str, RobotFileParser | None] = {}
        self.failed_requests = 0

    def revalidate_expired(self, interval_hours: int = 24, provider: str | None = None) -> int:
        now = datetime.now(timezone.utc)
        failed = 0
        for record in self.repository.company_boards(provider):
            last_validated = record["last_validated_at"]
            if last_validated:
                try:
                    validated_at = datetime.fromisoformat(last_validated)
                except ValueError:
                    validated_at = datetime.min.replace(tzinfo=timezone.utc)
                if validated_at.tzinfo is None:
                    validated_at = validated_at.replace(tzinfo=timezone.utc)
                if now - validated_at < timedelta(hours=interval_hours):
                    continue
            adapter = self.adapters[record["provider"]]
            board = ATSBoard(
                provider=record["provider"],
                board_identifier=record["board_identifier"],
                company_name=record["company_name"],
                career_page_url=record["career_page_url"],
                discovery_source=record["discovery_source"],
            )
            valid = adapter.validate(board)
            self.repository.save_company_board(
                company_name=board.company_name,
                provider=board.provider,
                board_identifier=board.board_identifier,
                career_page_url=str(board.career_page_url),
                discovery_source=board.discovery_source,
                validation_status=(
                    "valid" if valid else ("unverified" if adapter.last_validation_error else "invalid")
                ),
                last_validated_at=now.isoformat(),
            )
            if not valid:
                failed += 1
        return failed

    def discover(
        self, leads: list[tuple[str, str, str]]
    ) -> tuple[list[ATSBoard], int]:
        discovered: list[ATSBoard] = []
        seen: set[tuple[str, str]] = set()
        failures = 0
        self.failed_requests = 0
        for company_name, career_page_url, discovery_source in leads:
            try:
                urls = self._candidate_urls(career_page_url)
            except FetchError as exc:
                failures += 1
                logger.warning("Could not inspect career page %s: %s", career_page_url, exc)
                continue
            for board in discover_board_candidates(
                urls,
                company_name=company_name,
                career_page_url=career_page_url,
                discovery_source=discovery_source,
            ):
                key = (board.provider, board.board_identifier)
                if key in seen:
                    continue
                seen.add(key)
                adapter = self.adapters[board.provider]
                valid = adapter.validate(board)
                if adapter.last_validation_error:
                    failures += 1
                now = datetime.now(timezone.utc).isoformat()
                self.repository.save_company_board(
                    company_name=board.company_name,
                    provider=board.provider,
                    board_identifier=board.board_identifier,
                    career_page_url=str(board.career_page_url),
                    discovery_source=board.discovery_source,
                    validation_status=(
                        "valid" if valid else ("unverified" if adapter.last_validation_error else "invalid")
                    ),
                    last_validated_at=now,
                )
                if valid:
                    discovered.append(board)
            if self.request_delay_seconds:
                time.sleep(self.request_delay_seconds)
        return discovered, failures + self.failed_requests

    def _candidate_urls(self, career_page_url: str) -> list[str]:
        if not is_public_https_url(career_page_url):
            logger.warning("Skipping non-public or non-HTTPS career page: %s", career_page_url)
            return []
        if ATSBoardAdapter.identify(career_page_url):
            return [career_page_url]
        parts = urlsplit(career_page_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        robots = self.robots_cache.get(origin)
        if origin not in self.robots_cache:
            parser = RobotFileParser(f"{origin}/robots.txt")
            try:
                parser.parse(self.fetcher.get_text(f"{origin}/robots.txt").splitlines())
            except FetchError:
                self.failed_requests += 1
                self.robots_cache[origin] = None
                return []
            self.robots_cache[origin] = parser
            robots = parser
        if robots is None or not robots.can_fetch(self.user_agent, career_page_url):
            logger.info("Skipping career page disallowed by robots.txt: %s", career_page_url)
            return []
        html = self.fetcher.get_text(career_page_url)
        soup = BeautifulSoup(html, "html.parser")
        urls = [career_page_url]
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href")
            if isinstance(href, str):
                urls.append(urljoin(career_page_url, href))
        return urls
