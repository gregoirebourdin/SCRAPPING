"""Fetch tier chain (docs/ARCHITECTURE.md §Crawl tiers): cheapest tier first, escalate only on evidence.

Static fetch — :meth:`TierChain.fetch`:

    L1 ``http``               httpx, SSRF-safe transport, conditional requests (always first)
    L2 ``scrapling_fetcher``  only when L1 was blocked (403 / challenge) or answered an empty 2xx body
    L3 ``scrapling_dynamic``  only when L2 did not recover the page either (and dynamic is enabled)

429 is never escalated (the site asked us to slow down), SSRF refusals and network errors neither.
Once an escalation tier recovers a page it becomes *sticky* for the rest of the crawl (L1 is not
retried on every page of a site that blocks it).

Rendering — :meth:`TierChain.render` (only when ``render.needs_js`` says the page is client-rendered):

    ``scrapling_dynamic`` → ``crawl4ai`` → ``playwright``

Playwright is skipped when the Scrapling dynamic tier ran: same Chromium engine, and the Scrapling
session is reused across the crawl's pages while ``render_playwright`` launches a browser per page.
Crawl4AI stays the semantic / LLM-oriented option (and a renderer when Scrapling is not installed).

Every tier is an adapter (:class:`FetchTierAdapter`) returning an ``http.HttpResponse`` or ``None``;
adapters for missing optional dependencies report ``available() == False`` and are skipped. Each
attempt is recorded for empirical learning (dimension ``crawl.tier``) once per crawl, best effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import structlog

from scout.config import get_settings
from scout.crawl import http, render
from scout.crawl.scrapling_tiers import ScraplingDynamicTier, ScraplingFetcherTier
from scout.db.enums import FetchTier
from scout.errors import BlockedError

log = structlog.get_logger(__name__)

T = TypeVar("T")

HTTP = "http"
SCRAPLING_FETCHER = "scrapling_fetcher"
SCRAPLING_DYNAMIC = "scrapling_dynamic"
CRAWL4AI = "crawl4ai"
PLAYWRIGHT = "playwright"

ESCALATION_ORDER: tuple[str, ...] = (SCRAPLING_FETCHER, SCRAPLING_DYNAMIC)
RENDER_ORDER: tuple[str, ...] = (SCRAPLING_DYNAMIC, CRAWL4AI, PLAYWRIGHT)
BROWSER_TIERS = frozenset({SCRAPLING_DYNAMIC, CRAWL4AI, PLAYWRIGHT})

# Persisted page/run tier (DB enum, unchanged): finer tiers are only visible in the telemetry.
STORED_TIER: dict[str, FetchTier] = {
    HTTP: FetchTier.http,
    SCRAPLING_FETCHER: FetchTier.http,
    SCRAPLING_DYNAMIC: FetchTier.browser,
    CRAWL4AI: FetchTier.crawl4ai,
    PLAYWRIGHT: FetchTier.browser,
}
STATS_DIMENSION = "crawl.tier"
STATS_TIMEOUT_S = 3.0
_CHALLENGE_TITLES = (
    "just a moment...",
    "just a moment…",
    "attention required! | cloudflare",
    "ddos-guard",
    "please wait while we verify",
    "un instant…",
    "un instant...",
)


class FetchTierAdapter(Protocol):
    """A replaceable fetch tier. ``fetch`` never raises: ``None`` means "no usable answer"."""

    name: str

    def available(self) -> bool: ...

    async def fetch(self, url: str) -> http.HttpResponse | None: ...

    async def aclose(self) -> None: ...


L1Fetch = Callable[..., Awaitable[http.HttpResponse]]


@dataclass(frozen=True)
class TierAttempt:
    tier: str
    produced: bool
    latency_ms: int
    cost_usd: float


class RenderFnTier:
    """Adapter over the legacy render functions (``render.render_crawl4ai`` / ``render_playwright``),
    looked up at call time so tests can monkeypatch them. They check their own settings flag."""

    def __init__(
        self,
        name: str,
        fn: Callable[[], Callable[[str], Awaitable[str | None]]],
        enabled: Callable[[], bool],
    ) -> None:
        self.name = name
        self._fn = fn
        self.enabled = enabled

    def available(self) -> bool:
        return True

    async def fetch(self, url: str) -> http.HttpResponse | None:
        started = time.monotonic()
        html = await self._fn()(url)
        if not html:
            return None
        return http.HttpResponse(
            url=url,
            final_url=url,
            status_code=200,
            headers={},
            text=html,
            content_type="text/html",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            size_bytes=len(html),
            tier=self.name,
        )

    async def aclose(self) -> None:
        return None


def looks_challenged(resp: http.HttpResponse) -> bool:
    """Anti-bot interstitial served with any status (escalated tiers can get a 200 challenge)."""
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if resp.status_code in (401, 403, 429, 503) and http._looks_like_challenge(resp.headers, resp.text):
        return True
    head = resp.text[:4000].lower()
    start = head.find("<title")
    if start < 0:
        return False
    end = head.find("</title", start)
    title = head[head.find(">", start) + 1 : end if end > start else start + 300].strip()
    return any(title.startswith(t) for t in _CHALLENGE_TITLES)


def usable(resp: http.HttpResponse | None) -> bool:
    return (
        resp is not None
        and 200 <= resp.status_code < 400
        and bool(resp.text.strip())
        and not looks_challenged(resp)
    )


def _cost(tier: str) -> float:
    s = get_settings()
    return float(s.cost_browser_request_usd if tier in BROWSER_TIERS else s.cost_crawl_request_usd)


async def record_tier_stats(attempts: list[TierAttempt]) -> None:
    """Best-effort telemetry through ``scout.learning.stats`` (no-op when that module is absent)."""
    if not attempts:
        return
    try:
        stats: Any = importlib.import_module("scout.learning.stats")
    except ImportError:
        return
    try:
        events = [
            stats.StatEvent(
                dimension=STATS_DIMENSION,
                key=a.tier,
                produced=a.produced,
                latency_ms=a.latency_ms,
                cost_usd=a.cost_usd,
            )
            for a in attempts
        ]
        await asyncio.wait_for(stats.record(events), timeout=STATS_TIMEOUT_S)
    except Exception as exc:  # telemetry must never break a crawl
        log.debug("crawl_tier_stats_failed", error=str(exc)[:200])


class TierChain:
    """Per-crawl tier state: lazily built adapters, sticky escalation tier, telemetry buffer.

    ``tiers`` (tests / custom wiring) replaces the default adapters by name; ``l1`` replaces
    ``http.fetch``. Always ``await chain.aclose()`` (closes browser / curl sessions, flushes stats).
    """

    def __init__(
        self,
        *,
        tiers: Mapping[str, FetchTierAdapter] | None = None,
        l1: L1Fetch | None = None,
    ) -> None:
        self._custom = dict(tiers) if tiers is not None else None
        self._built: dict[str, FetchTierAdapter | None] = {}
        self._l1: L1Fetch = l1 or http.fetch
        self.sticky: str | None = None
        self.attempts: list[TierAttempt] = []

    # ---- adapters ------------------------------------------------------------------------------
    def tier(self, name: str) -> FetchTierAdapter | None:
        if self._custom is not None:
            return self._custom.get(name)
        if name not in self._built:
            self._built[name] = self._build(name)
        return self._built[name]

    def _build(self, name: str) -> FetchTierAdapter | None:
        if name == SCRAPLING_FETCHER:
            return ScraplingFetcherTier()
        if name == SCRAPLING_DYNAMIC:
            fetcher = self.tier(SCRAPLING_FETCHER)
            return ScraplingDynamicTier(fetcher) if isinstance(fetcher, ScraplingFetcherTier) else None
        if name == CRAWL4AI:
            return RenderFnTier(
                CRAWL4AI, lambda: render.render_crawl4ai, lambda: get_settings().crawler_enable_crawl4ai
            )
        if name == PLAYWRIGHT:
            return RenderFnTier(
                PLAYWRIGHT, lambda: render.render_playwright, lambda: get_settings().crawler_enable_browser
            )
        return None

    # ---- telemetry -----------------------------------------------------------------------------
    def _note(self, tier: str, started: float, *, produced: bool) -> None:
        latency = int((time.monotonic() - started) * 1000)
        self.attempts.append(TierAttempt(tier, produced, latency, _cost(tier)))
        log.debug("crawl_tier_attempt", tier=tier, produced=produced, ms=latency)

    async def _attempt(
        self, name: str, url: str, accept: Callable[[http.HttpResponse], bool] | None = None
    ) -> http.HttpResponse | None:
        adapter = self.tier(name)
        if adapter is None or not adapter.available():
            return None
        started = time.monotonic()
        try:
            resp = await adapter.fetch(url)
        except Exception as exc:  # adapters should not raise; be defensive anyway
            log.info("crawl_tier_error", tier=name, url=url[:300], error=str(exc)[:300])
            resp = None
        ok = usable(resp) and (accept is None or (resp is not None and accept(resp)))
        enabled = getattr(adapter, "enabled", None)
        if ok or enabled is None or enabled():
            self._note(name, started, produced=ok)
        return resp if ok else None

    # ---- static fetch --------------------------------------------------------------------------
    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> http.HttpResponse:
        """L1 first (or the sticky tier), escalating on block / challenge / empty body.

        Returns the response (``.tier`` names the tier) or raises L1's error, exactly like
        ``http.fetch``, when no tier recovered the page.
        """
        skip: str | None = None
        if self.sticky is not None:
            resp = await self._attempt(self.sticky, url)
            if resp is not None:
                return resp
            skip = self.sticky
        started = time.monotonic()
        try:
            resp = await self._l1(url, etag=etag, last_modified=last_modified)
        except BlockedError:  # 403 / anti-bot challenge (429 is RateLimitedError: not escalated)
            self._note(HTTP, started, produced=False)
            recovered = await self._escalate(url, skip=skip)
            if recovered is not None:
                return recovered
            raise
        except Exception:
            self._note(HTTP, started, produced=False)
            raise
        empty = 200 <= resp.status_code < 300 and not resp.not_modified and not resp.text.strip()
        self._note(HTTP, started, produced=resp.not_modified or (resp.status_code < 400 and not empty))
        if empty:
            recovered = await self._escalate(url, skip=skip)
            if recovered is not None:
                return recovered
        return resp

    async def _escalate(self, url: str, *, skip: str | None = None) -> http.HttpResponse | None:
        delay = max(0.0, get_settings().per_domain_delay_ms / 1000.0)
        for name in ESCALATION_ORDER:
            if name == skip:
                continue
            adapter = self.tier(name)
            if adapter is None or not adapter.available():
                continue
            if delay:
                await asyncio.sleep(delay)  # same politeness spacing as consecutive page fetches
            resp = await self._attempt(name, url)
            if resp is not None:
                if self.sticky != name:
                    log.info("crawl_tier_escalated", tier=name, url=url[:300])
                self.sticky = name
                return resp
        return None

    # ---- rendering -----------------------------------------------------------------------------
    async def render(self, url: str, accept: Callable[[str], T | None]) -> tuple[str, T, str] | None:
        """JS tiers in order; ``(html, accept(html), tier)`` for the first rendering ``accept``
        approves (e.g. more visible text than the HTTP version), else None."""
        dynamic_ran = False
        for name in RENDER_ORDER:
            if name == PLAYWRIGHT and dynamic_ran:
                continue
            adapter = self.tier(name)
            if adapter is None or not adapter.available():
                continue
            dynamic_ran = dynamic_ran or name == SCRAPLING_DYNAMIC
            accepted: list[T] = []

            def judge(resp: http.HttpResponse, _acc: list[T] = accepted) -> bool:
                value = accept(resp.text)
                if value is not None:
                    _acc.append(value)
                return value is not None

            resp = await self._attempt(name, url, accept=judge)
            if resp is not None and accepted:
                return resp.text, accepted[0], name
        return None

    # ---- lifecycle -----------------------------------------------------------------------------
    async def aclose(self) -> None:
        adapters = list(self._custom.values()) if self._custom is not None else list(self._built.values())
        for adapter in reversed(adapters):  # dynamic (built after the fetcher) closes first
            if adapter is not None:
                with contextlib.suppress(Exception):
                    await adapter.aclose()
        attempts, self.attempts = self.attempts, []
        await record_tier_stats(attempts)
