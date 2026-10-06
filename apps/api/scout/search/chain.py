"""SearchChain: ordered providers with in-process health/cooldown, a TTL cache and per-provider telemetry.

``search()`` / ``search_page()`` try the configured providers in order (default SearXNG → DuckDuckGo HTML),
skipping unconfigured or cooling-down ones; an error moves on to the next provider. The first provider that
answers wins; its results are judged by the caller's ``Assessor`` and the verdict (``sufficient`` + reason)
travels with the result, so callers escalate to Gemini grounding only on an explicit "insufficient".
With ``escalate_on_insufficient`` an insufficient answer also tries the next provider (results are merged).

The chain never raises on provider failures: ``ChainResult.provider is None`` means nobody answered, and
``ChainResult.raise_if_failed()`` turns that into the most relevant typed error (blocked > rate limited >
provider error) for callers that need job-level backoff (the discovery source).

Health and cache are per process (no DB writes, no new tables): good enough for a small worker fleet and
self-healing on restart. Cached entries are first pages only (later pages carry provider state).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from scout.config import get_settings
from scout.search.assess import Assessment, Assessor, assess_any
from scout.search.telemetry import ENGINE_DIMENSION, record_search_usage, record_stats, stat
from scout.search.types import (
    SearchBlockedError,
    SearchFailure,
    SearchPage,
    SearchProviderError,
    SearchRateLimitedError,
    SearchResult,
    TimeRange,
    WebSearchProvider,
)
from scout.util.text import collapse_ws

log = structlog.get_logger(__name__)


# ---- health ---------------------------------------------------------------------------------


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    blocks: int = 0
    cooldown_until: float = 0.0
    last_error: str | None = None


class ProviderHealth:
    """Consecutive-failure cooldown per provider (process-wide).

    Blocked (CAPTCHA / anomaly): cooldown at once, doubling per repeated block. Rate limited: base cooldown at
    once. Other errors: cooldown after ``failure_threshold`` consecutive failures, doubling. Success resets.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        base_cooldown_s: float = 60.0,
        block_cooldown_s: float = 300.0,
        max_cooldown_s: float = 1800.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.base_cooldown_s = base_cooldown_s
        self.block_cooldown_s = block_cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self._state: dict[str, _ProviderState] = {}

    def _get(self, name: str) -> _ProviderState:
        return self._state.setdefault(name, _ProviderState())

    def cooldown_remaining(self, name: str) -> float:
        st = self._state.get(name)
        return max(0.0, st.cooldown_until - time.monotonic()) if st else 0.0

    def available(self, name: str) -> bool:
        return self.cooldown_remaining(name) <= 0.0

    def success(self, name: str) -> None:
        st = self._get(name)
        st.consecutive_failures, st.blocks, st.cooldown_until, st.last_error = 0, 0, 0.0, None

    def failure(self, name: str, error: str, *, blocked: bool = False, rate_limited: bool = False) -> None:
        st = self._get(name)
        st.consecutive_failures += 1
        st.last_error = error[:300]
        cooldown = 0.0
        if blocked:
            st.blocks += 1
            cooldown = self.block_cooldown_s * 2 ** (st.blocks - 1)
        elif rate_limited:
            cooldown = self.base_cooldown_s
        elif st.consecutive_failures >= self.failure_threshold:
            cooldown = self.base_cooldown_s * 2 ** (st.consecutive_failures - self.failure_threshold)
        if cooldown > 0:
            st.cooldown_until = time.monotonic() + min(self.max_cooldown_s, cooldown)
            log.warning(
                "search.provider_cooling_down", provider=name, seconds=round(cooldown), error=error[:200]
            )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            k: {
                "consecutive_failures": v.consecutive_failures,
                "cooldown_s": round(self.cooldown_remaining(k)),
                "last_error": v.last_error,
            }
            for k, v in self._state.items()
        }

    def reset(self) -> None:
        self._state.clear()


# ---- cache ----------------------------------------------------------------------------------


class SearchCache:
    """Small in-process LRU with TTL: normalized request → (provider, results, next_state)."""

    def __init__(self, *, max_entries: int = 2048) -> None:
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, SearchPage]] = OrderedDict()

    @staticmethod
    def key(
        query: str, *, lang: str | None, region: str | None, time_range: str | None, site: str | None
    ) -> str:
        return "|".join(
            [
                collapse_ws(query).casefold(),
                (lang or "").lower(),
                (region or "").upper(),
                time_range or "",
                site or "",
            ]
        )

    def get(self, key: str) -> SearchPage | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, page = hit
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return SearchPage(
            results=list(page.results),
            provider=page.provider,
            next_state=dict(page.next_state) if page.next_state else None,
        )

    def put(self, key: str, page: SearchPage, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        self._data[key] = (time.monotonic() + ttl_s, page)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


HEALTH = ProviderHealth()
CACHE = SearchCache()


def reset_state() -> None:
    """Forget provider health and cached results (tests, admin)."""
    HEALTH.reset()
    CACHE.clear()


# ---- results --------------------------------------------------------------------------------


@dataclass
class Attempt:
    provider: str
    ok: bool
    results: int = 0
    latency_ms: int = 0
    error: str | None = None
    blocked: bool = False
    rate_limited: bool = False
    cached: bool = False
    skipped: str | None = None  # "unconfigured" | "cooling_down"
    sufficient: bool | None = None
    exc: SearchFailure | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in {
                "provider": self.provider,
                "ok": self.ok,
                "results": self.results,
                "latency_ms": self.latency_ms,
                "error": self.error,
                "cached": self.cached or None,
                "skipped": self.skipped,
                "sufficient": self.sufficient,
            }.items()
            if v is not None
        }


@dataclass
class ChainResult:
    query: str
    results: list[SearchResult]
    provider: str | None  # who answered; None = no provider could answer
    assessment: Assessment
    attempts: list[Attempt] = field(default_factory=list)
    next_state: dict[str, Any] | None = None
    from_cache: bool = False

    @property
    def sufficient(self) -> bool:
        return self.assessment.sufficient

    @property
    def usable(self) -> list[SearchResult]:
        return self.assessment.usable

    @property
    def failed(self) -> bool:
        return self.provider is None

    def summary(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "results": len(self.results),
            "sufficient": self.sufficient,
            "reason": self.assessment.reason,
            "attempts": [a.as_dict() for a in self.attempts],
        }

    def raise_if_failed(self) -> None:
        """Raise the most relevant typed error when no provider answered (blocked > rate limited > error)."""
        if self.provider is not None:
            return
        errors = [a.exc for a in self.attempts if a.exc is not None]
        for kind in (SearchBlockedError, SearchRateLimitedError, SearchProviderError):
            for exc in errors:
                if isinstance(exc, kind):
                    raise exc
        if any(a.skipped == "cooling_down" for a in self.attempts):
            names = ", ".join(a.provider for a in self.attempts if a.skipped == "cooling_down")
            raise SearchBlockedError(f"web search providers cooling down: {names}", provider=names)
        raise SearchProviderError("no web search provider configured", provider="")


# ---- chain ----------------------------------------------------------------------------------


class SearchChain:
    def __init__(
        self,
        providers: Sequence[WebSearchProvider],
        *,
        purpose: str = "search",
        health: ProviderHealth | None = None,
        cache: SearchCache | None = None,
        cache_ttl_s: float | None = None,
        escalate_on_insufficient: bool = False,
    ) -> None:
        self.providers = list(providers)
        self.purpose = purpose
        self.health = health if health is not None else HEALTH
        self.cache = cache if cache is not None else CACHE
        self.cache_ttl_s = float(get_settings().search_cache_ttl_s if cache_ttl_s is None else cache_ttl_s)
        self.escalate_on_insufficient = escalate_on_insufficient

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    @property
    def configured(self) -> bool:
        return any(p.is_configured() for p in self.providers)

    def available(self) -> bool:
        """At least one configured provider that is not cooling down."""
        return any(p.is_configured() and self.health.available(p.name) for p in self.providers)

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        lang: str | None = None,
        region: str | None = None,
        time_range: TimeRange | None = None,
        site: str | None = None,
        assess: Assessor | None = None,
    ) -> ChainResult:
        res = await self.search_page(
            query, lang=lang, region=region, time_range=time_range, site=site, assess=assess
        )
        if len(res.results) > num:
            res.results = res.results[:num]
            res.assessment = (assess or assess_any)(res.results)
        return res

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
        provider: str | None = None,
        assess: Assessor | None = None,
    ) -> ChainResult:
        """One page. With ``provider`` + ``state`` (pagination), that provider is tried first."""
        assessor = assess or assess_any
        order = list(self.providers)
        if provider:
            order.sort(key=lambda p: p.name != provider)
        cacheable = page == 1 and not state
        ckey = SearchCache.key(query, lang=lang, region=region, time_range=time_range, site=site)
        if cacheable:
            hit = self.cache.get(ckey)
            if hit is not None:
                hit_verdict = assessor(hit.results)
                return ChainResult(
                    query=query,
                    results=hit.results,
                    provider=hit.provider,
                    assessment=hit_verdict,
                    attempts=[
                        Attempt(
                            hit.provider,
                            ok=True,
                            results=len(hit.results),
                            cached=True,
                            sufficient=hit_verdict.sufficient,
                        )
                    ],
                    next_state=hit.next_state,
                    from_cache=True,
                )

        attempts: list[Attempt] = []
        answered: SearchPage | None = None
        merged: list[SearchResult] = []
        verdict: Assessment | None = None
        for prov in order:
            if not prov.is_configured():
                attempts.append(Attempt(prov.name, ok=False, skipped="unconfigured"))
                continue
            if not self.health.available(prov.name):
                attempts.append(Attempt(prov.name, ok=False, skipped="cooling_down"))
                continue
            own_state = state if (state and (provider is None or prov.name == provider)) else None
            started = time.monotonic()
            try:
                pg = await prov.search_page(
                    query,
                    lang=lang,
                    region=region,
                    time_range=time_range,
                    site=site,
                    page=page,
                    state=own_state,
                )
            except SearchFailure as exc:
                latency = int((time.monotonic() - started) * 1000)
                blocked = isinstance(exc, SearchBlockedError)
                limited = isinstance(exc, SearchRateLimitedError)
                self.health.failure(prov.name, str(exc), blocked=blocked, rate_limited=limited)
                attempts.append(
                    Attempt(
                        prov.name,
                        ok=False,
                        latency_ms=latency,
                        error=str(exc)[:300],
                        blocked=blocked,
                        rate_limited=limited,
                        sufficient=False,
                        exc=exc,
                    )
                )
                await record_search_usage(prov.name, purpose=self.purpose, cost_usd=prov.cost_usd)
                log.info(
                    "search.provider_failed", provider=prov.name, purpose=self.purpose, error=str(exc)[:200]
                )
                continue
            latency = int((time.monotonic() - started) * 1000)
            self.health.success(prov.name)
            await record_search_usage(prov.name, purpose=self.purpose, cost_usd=prov.cost_usd)
            if answered is None:
                answered = pg
                merged = list(pg.results)
            else:
                known = {r.url for r in merged}
                merged.extend(r for r in pg.results if r.url not in known)
            verdict = assessor(merged)
            attempts.append(
                Attempt(
                    prov.name,
                    ok=True,
                    results=len(pg.results),
                    latency_ms=latency,
                    sufficient=verdict.sufficient,
                )
            )
            if cacheable and answered is pg:
                self.cache.put(ckey, pg, self.cache_ttl_s)
            if verdict.sufficient or not self.escalate_on_insufficient:
                break

        await record_stats(
            [
                stat(
                    ENGINE_DIMENSION,
                    a.provider,
                    produced=bool(a.sufficient),
                    latency_ms=a.latency_ms,
                    cost_usd=0.0,
                )
                for a in attempts
                if a.skipped is None and not a.cached
            ]
        )
        if answered is None:
            reasons = (
                "; ".join(f"{a.provider}: {a.error or a.skipped}" for a in attempts)
                or "no provider configured"
            )
            verdict = Assessment(False, f"no search provider answered ({reasons})", [])
            result = ChainResult(
                query=query, results=[], provider=None, assessment=verdict, attempts=attempts
            )
        else:
            assert verdict is not None
            result = ChainResult(
                query=query,
                results=merged,
                provider=answered.provider,
                assessment=verdict,
                attempts=attempts,
                next_state=answered.next_state,
            )
        log.info(
            "search.chain",
            purpose=self.purpose,
            provider=result.provider,
            results=len(result.results),
            sufficient=result.sufficient,
            reason=result.assessment.reason[:200],
        )
        return result


# ---- factories ------------------------------------------------------------------------------

_ALIASES = {"ddg": "duckduckgo", "duckduckgo_html": "duckduckgo", "searx": "searxng"}


def build_providers(names: Sequence[str], **overrides: Any) -> list[WebSearchProvider]:
    """Provider instances for ``names`` (unknown names are ignored with a warning).

    ``overrides``: ``ddg_throttle`` (a ``Throttle``) for the DuckDuckGo provider.
    """
    from scout.search.duckduckgo import DuckDuckGoProvider
    from scout.search.searxng import SearXNGProvider

    out: list[WebSearchProvider] = []
    for raw in names:
        name = _ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if not name or any(p.name == name for p in out):
            continue
        if name == "searxng":
            out.append(SearXNGProvider())
        elif name == "duckduckgo":
            out.append(DuckDuckGoProvider(throttle=overrides.get("ddg_throttle")))
        else:
            log.warning("search.unknown_provider", provider=raw)
    return out


def discovery_chain(**overrides: Any) -> SearchChain:
    """Paginated company-list searches (``web_search`` discovery source): ``SEARCH_PROVIDERS``."""
    return SearchChain(build_providers(get_settings().search_providers, **overrides), purpose="discovery")


def lookup_chain(purpose: str = "lookup", **overrides: Any) -> SearchChain:
    """Per-lead lookups and the free pass before any Gemini grounding: ``SEARCH_LOOKUP_PROVIDERS``.

    Default SearXNG only — the DuckDuckGo HTML endpoint blocks quickly at per-lead volume (opt in explicitly).
    """
    return SearchChain(build_providers(get_settings().search_lookup_providers, **overrides), purpose=purpose)


def gemini_fallback_only() -> bool:
    """True (default): Gemini grounding runs only when the free search chain could not resolve the question."""
    return bool(get_settings().gemini_search_fallback_only)
