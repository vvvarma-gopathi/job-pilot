from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from ..models import Job


class JsonFetcher(Protocol):
    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any: ...


class BaseSource(ABC):
    name: str

    def __init__(self, fetcher: JsonFetcher):
        self.fetcher = fetcher

    @abstractmethod
    def fetch_jobs(self) -> list[Job]:
        raise NotImplementedError
