"""Self-hosted SearXNG (``services/searxng``) as a ``WebSearchProvider``, over its JSON search API.

``GET {SEARXNG_URL}/search?q=…&format=json&pageno=n&language=fr-FR&safesearch=0&categories=general``.
The instance is private (Railway private network, limiter off), so there is no auth and no throttle beyond a
bounded per-process concurrency (``SEARXNG_MAX_CONCURRENCY``). Failures are typed:

* timeout / connection error / 5xx / non-JSON (``search.formats`` lacks ``json`` → 403) → ``SearchProviderError``
* 429 → ``SearchRateLimitedError``
* no results while upstream engines report errors (CAPTCHA, suspended, timeouts) → ``SearchBlockedError``,
  so the chain falls back to the next provider instead of trusting an empty page.
"""

from __future__ import annotations

import asyncio
import weakref
from datetime import datetime
from typing import Any

import httpx

from scout.config import get_settings
from scout.discovery import geo
from scout.search.types import (
    SearchBlockedError,
    SearchPage,
    SearchProviderError,
    SearchRateLimitedError,
    SearchResult,
    TimeRange,
)
from scout.util.text import collapse_ws

NAME = "searxng"
_sems: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


def _semaphore(size: int) -> asyncio.Semaphore:
    """Bounded concurrency per event loop (asyncio primitives must not cross loops)."""
    per_loop = _sems.setdefault(asyncio.get_running_loop(), {})
    sem = per_loop.get(size)
    if sem is None:
        sem = per_loop[size] = asyncio.Semaphore(max(1, size))
    return sem


def language_param(lang: str | None, region: str | None) -> str | None:
    """SearXNG ``language``: "fr-FR" with both, "fr" with a language only, the region's language otherwise."""
    reg = (region or "").upper() or None
    lg = (lang or "").lower() or (geo.country_language(reg) if reg else None)
    if lg and reg:
        return f"{lg}-{reg}"
    return lg


def _published(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_payload(payload: dict[str, Any], *, offset: int = 0) -> tuple[list[SearchResult], list[str]]:
    """JSON payload → (results in SearXNG order, unresponsive engine descriptions)."""
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
        results.append(
            SearchResult(
                url=url,
                title=collapse_ws(str(item.get("title") or "")),
                snippet=collapse_ws(str(item.get("content") or "")),
                position=offset + len(results) + 1,
                engines=tuple(str(e) for e in engines),
                published_at=_published(item.get("publishedDate")),
            )
        )
    unresponsive: list[str] = []
    for e in payload.get("unresponsive_engines") or []:
        if isinstance(e, list | tuple) and e:
            unresponsive.append(": ".join(str(x) for x in e[:2]))
        elif isinstance(e, str):
            unresponsive.append(e)
    return results, unresponsive


class SearXNGProvider:
    name = NAME
    cost_usd = 0.0

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_concurrency: int | None = None,
        categories: str | None = None,
        engines: str | None = None,
        safesearch: int | None = None,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url if base_url is not None else s.searxng_url or "").rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else s.searxng_timeout
        self.max_concurrency = max_concurrency if max_concurrency is not None else s.searxng_max_concurrency
        self.categories = categories if categories is not None else s.searxng_categories
        self.engines = engines if engines is not None else s.searxng_engines
        self.safesearch = safesearch if safesearch is not None else s.searxng_safesearch

    def is_configured(self) -> bool:
        return bool(self.base_url)

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
        page = await self.search_page(query, lang=lang, region=region, time_range=time_range, site=site)
        return page.results[: max(0, num)]

    def params(
        self,
        query: str,
        *,
        lang: str | None,
        region: str | None,
        time_range: TimeRange | None,
        site: str | None,
        page: int,
    ) -> dict[str, str]:
        params = {
            "q": f"{query} site:{site}" if site else query,
            "format": "json",
            "pageno": str(max(1, page)),
            "safesearch": str(self.safesearch),
        }
        if self.categories:
            params["categories"] = self.categories
        if self.engines:
            params["engines"] = self.engines
        language = language_param(lang, region)
        if language:
            params["language"] = language
        if time_range:
            params["time_range"] = time_range
        return params

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
        if not self.base_url:
            raise SearchProviderError("searxng: SEARXNG_URL is not configured", provider=NAME)
        pageno = int((state or {}).get("pageno") or page or 1)
        params = self.params(query, lang=lang, region=region, time_range=time_range, site=site, page=pageno)
        timeout = httpx.Timeout(self.timeout_s, connect=min(3.0, self.timeout_s))
        try:
            async with _semaphore(self.max_concurrency):
                async with httpx.AsyncClient(
                    timeout=timeout, headers={"Accept": "application/json", "User-Agent": "scout-api/1.0"}
                ) as client:
                    resp = await client.get(f"{self.base_url}/search", params=params)
        except httpx.TimeoutException as exc:
            raise SearchProviderError(f"searxng: timeout after {self.timeout_s:.0f}s", provider=NAME) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(
                f"searxng: network error: {exc.__class__.__name__}", provider=NAME
            ) from exc
        if resp.status_code == 429:
            raise SearchRateLimitedError(
                "searxng: rate limited (429) — is the limiter enabled?", provider=NAME
            )
        if resp.status_code == 403:
            raise SearchProviderError(
                "searxng: 403 — JSON output disabled (add json to search.formats) or access denied",
                provider=NAME,
            )
        if resp.status_code >= 400:
            raise SearchProviderError(f"searxng: HTTP {resp.status_code}", provider=NAME)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SearchProviderError("searxng: response is not JSON", provider=NAME) from exc
        if not isinstance(payload, dict):
            raise SearchProviderError("searxng: unexpected JSON payload", provider=NAME)
        results, unresponsive = parse_payload(payload, offset=(pageno - 1) * 10)
        if not results and unresponsive:
            raise SearchBlockedError(
                f"searxng: no results, engines failing ({'; '.join(unresponsive)[:300]})", provider=NAME
            )
        return SearchPage(
            results=results,
            provider=NAME,
            next_state={"pageno": pageno + 1} if results else None,
            unresponsive=unresponsive,
        )
