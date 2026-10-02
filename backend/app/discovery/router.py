"""Search router: fan a query out to healthy engines, cache SERPs, fail over on blocks."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..config import settings
from ..db import SessionLocal
from ..fetch.http import Fetcher
from ..models import SerpCache
from .base import EngineBlocked, EngineError, SearchProvider, SerpResult
from .bing import BingProvider
from .duckduckgo import DuckDuckGoProvider
from .google import GoogleProvider

log = logging.getLogger(__name__)


class SearchRouter:
    def __init__(self, fetcher: Fetcher, engines: list[str] | None = None) -> None:
        wanted = engines or settings.engines
        self.providers: dict[str, SearchProvider] = {}
        for name in wanted:
            if name == "google":
                self.providers[name] = GoogleProvider()
            elif name == "bing":
                self.providers[name] = BingProvider(fetcher)
            elif name in ("duckduckgo", "ddg"):
                self.providers["duckduckgo"] = DuckDuckGoProvider(fetcher)
        self._sem = asyncio.Semaphore(settings.search_concurrency)
        self.stats = {"queries": 0, "cache_hits": 0, "results": 0}

    # --- cache --------------------------------------------------------------------------------------------
    @staticmethod
    def _key(engine: str, query: str, country: str, pages: int) -> str:
        return f"{engine}|{country}|{pages}|{query.strip().lower()}"[:512]

    async def _cached(self, key: str) -> list[SerpResult] | None:
        async with SessionLocal() as s:
            row = await s.get(SerpCache, key)
        if row is None or row.fetched_at < datetime.utcnow() - timedelta(hours=settings.serp_cache_ttl_hours):
            return None
        return [SerpResult.from_dict(d) for d in row.results]

    async def _store(self, key: str, engine: str, query: str, results: list[SerpResult]) -> None:
        async with SessionLocal() as s:
            row = await s.get(SerpCache, key)
            if row is None:
                row = SerpCache(key=key, engine=engine, query=query)
                s.add(row)
            row.results = [r.to_dict() for r in results]
            row.fetched_at = datetime.utcnow()
            await s.commit()

    # --- search -------------------------------------------------------------------------------------------
    def healthy(self) -> list[SearchProvider]:
        return [p for p in self.providers.values() if p.health.available]

    def any_alive(self) -> bool:
        return any(not p.health.disabled for p in self.providers.values())

    async def search_one(self, provider: SearchProvider, query: str, country: str, pages: int) -> list[SerpResult]:
        key = self._key(provider.name, query, country, pages)
        cached = await self._cached(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached
        async with self._sem:
            try:
                results = await provider.search(query, country=country, pages=pages)
            except EngineBlocked as e:
                provider.health.blocked(str(e))
                return []
            except EngineError as e:
                provider.health.failed(str(e))
                log.debug("engine %s error on %r: %s", provider.name, query, e)
                return []
        provider.health.ok(len(results))
        self.stats["queries"] += 1
        self.stats["results"] += len(results)
        await self._store(key, provider.name, query, results)
        return results

    async def search(self, query: str, *, country: str = "us", pages: int | None = None, max_engines: int = 2) -> list[SerpResult]:
        """Query up to ``max_engines`` healthy engines and merge (de-duplicated by URL)."""
        pages = pages or settings.search_pages_per_query
        providers = self.healthy()[:max_engines]
        if not providers:
            return []
        batches = await asyncio.gather(*(self.search_one(p, query, country, pages if p.supports_pagination else 1) for p in providers))
        merged: dict[str, SerpResult] = {}
        for batch in batches:
            for r in batch:
                merged.setdefault(r.url, r)
        return list(merged.values())

    def health_snapshot(self) -> dict:
        return {name: p.health.snapshot() for name, p in self.providers.items()}

    async def close(self) -> None:
        for p in self.providers.values():
            await p.close()
