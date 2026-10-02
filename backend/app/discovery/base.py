"""Search-provider abstraction with health tracking (captcha → cooldown → disable)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class SerpResult:
    engine: str
    query: str
    country: str
    rank: int
    url: str
    title: str = ""
    snippet: str = ""

    def to_dict(self) -> dict:
        return {"engine": self.engine, "query": self.query, "country": self.country, "rank": self.rank, "url": self.url, "title": self.title, "snippet": self.snippet}

    @classmethod
    def from_dict(cls, d: dict) -> "SerpResult":
        return cls(**{k: d.get(k, "") for k in ("engine", "query", "country", "rank", "url", "title", "snippet")})


class EngineBlocked(Exception):
    """Raised when an engine answers with a captcha/challenge/rate-limit."""


class EngineError(Exception):
    """Transient error (network, parse)."""


@dataclass
class EngineHealth:
    name: str
    consecutive_failures: int = 0
    blocked_count: int = 0
    cooldown_until: float = 0.0
    disabled: bool = False
    queries: int = 0
    results: int = 0
    last_error: str | None = None
    history: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return not self.disabled and time.monotonic() >= self.cooldown_until

    def ok(self, n_results: int) -> None:
        self.consecutive_failures = 0
        self.queries += 1
        self.results += n_results

    def blocked(self, reason: str) -> None:
        self.blocked_count += 1
        self.consecutive_failures += 1
        self.last_error = reason
        self.history.append(f"blocked:{reason}")
        self.cooldown_until = time.monotonic() + settings.engine_cooldown_seconds * min(4, self.blocked_count)
        if self.consecutive_failures >= settings.engine_max_failures:
            self.disabled = True
        log.warning("engine %s blocked (%s); cooldown %ss, disabled=%s", self.name, reason, settings.engine_cooldown_seconds * min(4, self.blocked_count), self.disabled)

    def failed(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.last_error = reason
        self.history.append(f"error:{reason[:80]}")
        if self.consecutive_failures >= settings.engine_max_failures:
            self.disabled = True

    def snapshot(self) -> dict:
        return {
            "available": self.available, "disabled": self.disabled, "queries": self.queries, "results": self.results,
            "blocked": self.blocked_count, "failures": self.consecutive_failures, "last_error": self.last_error,
            "cooldown_remaining": max(0, int(self.cooldown_until - time.monotonic())),
        }


class SearchProvider:
    name: str = "base"
    supports_pagination: bool = False

    def __init__(self) -> None:
        self.health = EngineHealth(self.name)

    async def search(self, query: str, *, country: str = "us", pages: int = 1) -> list[SerpResult]:
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - optional
        return None
