"""Web search layer contracts: result records, the provider protocol and typed errors.

Providers are small adapters (SearXNG JSON API, DuckDuckGo HTML) behind ``WebSearchProvider``. Every failure is
raised as one of the ``Search*Error`` classes; they also subclass the job error taxonomy of ``scout.errors``
(``BlockedError`` / ``RateLimitedError`` / ``FetchError``), so a discovery job that lets one escape gets the
usual backoff and source-health treatment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from scout.errors import BlockedError, FetchError, RateLimitedError

TimeRange = Literal["day", "week", "month", "year"]


@dataclass
class SearchResult:
    """One organic search result. ``position`` is the 1-based rank in the provider's result list."""

    url: str
    title: str
    snippet: str
    position: int = 0
    engines: tuple[str, ...] = ()  # upstream engines that returned it (SearXNG merges several)
    published_at: datetime | None = None

    @property
    def rank(self) -> int:
        """Legacy alias of ``position`` (DuckDuckGo discovery code and tests)."""
        return self.position

    @property
    def engine(self) -> str | None:
        return self.engines[0] if self.engines else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "position": self.position,
            "engines": list(self.engines),
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass
class SearchPage:
    """One page of results from one provider. ``next_state`` is an opaque provider cursor (None = last page)."""

    results: list[SearchResult]
    provider: str
    next_state: dict[str, Any] | None = None
    unresponsive: list[str] = field(default_factory=list)  # upstream engines that failed (metasearch only)


class WebSearchProvider(Protocol):
    """A replaceable web search backend. Implementations must raise ``Search*Error`` on failure."""

    name: str
    cost_usd: float  # per request; 0 for self-hosted SearXNG and the free DuckDuckGo endpoint

    def is_configured(self) -> bool: ...

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        lang: str | None = None,
        region: str | None = None,
        time_range: TimeRange | None = None,
        site: str | None = None,
    ) -> list[SearchResult]:
        """First page of results, at most ``num``. ``lang`` is ISO 639-1, ``region`` ISO 3166-1 alpha-2."""
        ...

    async def search_page(
        self,
        query: str,
        *,
        lang: str | None = None,
        region: str | None = None,
        time_range: TimeRange | None = None,
        site: str | None = None,
        page: int = 1,
        state: dict[str, Any] | None = None,
    ) -> SearchPage:
        """One page; ``state`` is the ``next_state`` of the previous page of the same provider."""
        ...


class SearchFailure(Exception):
    """Mixin shared by every web search error (catch this to handle any provider failure)."""

    provider: str = ""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider


class SearchProviderError(SearchFailure, FetchError):
    """Network error, timeout, 5xx or unusable payload (e.g. JSON format disabled on SearXNG)."""


class SearchBlockedError(SearchFailure, BlockedError):
    """Anti-bot page, 403, or every upstream engine of a metasearch provider suspended / failing."""


class SearchRateLimitedError(SearchFailure, RateLimitedError):
    """HTTP 429 from the provider."""
