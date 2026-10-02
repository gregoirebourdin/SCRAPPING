"""Polite, cached, concurrent HTTP fetcher.

* global + per-host concurrency limits and a minimum delay between hits on the same host
* retries with exponential backoff on transient errors
* persistent cache (``page_cache`` table) so re-runs never refetch
* optional robots.txt compliance
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import PageCache
from ..util.ua import browser_headers, random_ua
from ..util.urls import canonicalize, ensure_scheme

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    html: str = ""
    content_type: str = ""
    error: str | None = None
    from_cache: bool = False
    rendered: bool = False
    elapsed: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300 and bool(self.html)

    @property
    def is_html(self) -> bool:
        ct = (self.content_type or "").lower()
        return "html" in ct or "xml" in ct or ct == "" or ct.startswith("text/")


class HostThrottle:
    """Per-host semaphore + spacing between requests."""

    def __init__(self, concurrency: int, min_delay: float) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.min_delay = min_delay
        self.last = 0.0
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        await self.sem.acquire()
        async with self.lock:
            wait = self.last + self.min_delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self.sem.release()


class Fetcher:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._global = asyncio.Semaphore(settings.fetch_concurrency)
        self._hosts: dict[str, HostThrottle] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._cache_lock = asyncio.Lock()
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "bytes": 0}

    # --- lifecycle --------------------------------------------------------------------------------------
    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(settings.fetch_timeout, connect=10.0),
                limits=httpx.Limits(max_connections=settings.fetch_concurrency + 8, max_keepalive_connections=20),
                proxy=settings.proxy_url,
                headers=browser_headers(),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "Fetcher":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Fetcher not started")
        return self._client

    def throttle(self, host: str, *, concurrency: int | None = None, min_delay: float | None = None) -> HostThrottle:
        t = self._hosts.get(host)
        if t is None:
            t = HostThrottle(concurrency or settings.per_host_concurrency, min_delay if min_delay is not None else settings.per_host_min_delay)
            self._hosts[host] = t
        return t

    # --- cache ------------------------------------------------------------------------------------------
    async def _cache_get(self, key: str, ttl_hours: int) -> FetchResult | None:
        async with SessionLocal() as s:
            row = await s.get(PageCache, key)
        if row is None:
            return None
        if row.fetched_at < datetime.utcnow() - timedelta(hours=ttl_hours):
            return None
        html = zlib.decompress(row.body).decode("utf-8", "ignore") if row.body else ""
        return FetchResult(
            url=key, final_url=row.final_url or key, status=row.status_code, html=html,
            content_type=row.content_type or "", error=row.error, from_cache=True, rendered=row.rendered,
        )

    async def cache_put(self, key: str, res: FetchResult) -> None:
        body = zlib.compress(res.html.encode("utf-8", "ignore"), 6) if res.html else None
        async with self._cache_lock:
            async with SessionLocal() as s:
                row = await s.get(PageCache, key)
                if row is None:
                    row = PageCache(url=key)
                    s.add(row)
                row.final_url = res.final_url
                row.status_code = res.status
                row.content_type = res.content_type[:128]
                row.body = body
                row.rendered = res.rendered
                row.error = res.error
                row.fetched_at = datetime.utcnow()
                await s.commit()

    # --- robots -----------------------------------------------------------------------------------------
    async def _allowed(self, url: str) -> bool:
        if not settings.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = await self.client.get(base + "/robots.txt", headers={"User-Agent": random_ua()})
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None
            except Exception:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch("*", url)

    # --- fetch ------------------------------------------------------------------------------------------
    async def fetch(
        self,
        url: str,
        *,
        use_cache: bool = True,
        ttl_hours: int | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        retries: int | None = None,
    ) -> FetchResult:
        url = ensure_scheme(url)
        key = canonicalize(url)
        ttl = settings.page_cache_ttl_hours if ttl_hours is None else ttl_hours
        if use_cache:
            cached = await self._cache_get(key, ttl)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached

        if not await self._allowed(url):
            res = FetchResult(url=url, error="robots_disallow")
            return res

        host = (urlparse(url).hostname or "").lower()
        retries = settings.fetch_retries if retries is None else retries
        max_bytes = max_bytes or settings.max_html_bytes
        res = FetchResult(url=url)
        attempt = 0
        t0 = time.monotonic()
        while True:
            attempt += 1
            try:
                async with self._global, self.throttle(host):
                    self.stats["requests"] += 1
                    hdrs = browser_headers()
                    if headers:
                        hdrs.update(headers)
                    async with self.client.stream("GET", url, headers=hdrs) as r:
                        res.status = r.status_code
                        res.final_url = str(r.url)
                        res.content_type = r.headers.get("content-type", "")
                        res.headers = {k.lower(): v for k, v in r.headers.items() if k.lower() in ("last-modified", "server", "x-powered-by", "content-type")}
                        chunks: list[bytes] = []
                        size = 0
                        if res.is_html:
                            async for chunk in r.aiter_bytes():
                                chunks.append(chunk)
                                size += len(chunk)
                                if size >= max_bytes:
                                    break
                        raw = b"".join(chunks)
                        self.stats["bytes"] += len(raw)
                        enc = r.encoding or "utf-8"
                        try:
                            res.html = raw.decode(enc, "ignore")
                        except LookupError:
                            res.html = raw.decode("utf-8", "ignore")
                res.error = None
                if res.status in TRANSIENT_STATUS and attempt <= retries:
                    await asyncio.sleep(min(8.0, 1.5 * 2 ** (attempt - 1)))
                    continue
                break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as e:
                res.error = f"{type(e).__name__}: {str(e)[:160]}"
                if attempt <= retries:
                    await asyncio.sleep(min(8.0, 1.5 * 2 ** (attempt - 1)))
                    continue
                break
            except httpx.HTTPError as e:
                res.error = f"{type(e).__name__}: {str(e)[:160]}"
                break
            except Exception as e:  # pragma: no cover - defensive
                res.error = f"{type(e).__name__}: {str(e)[:160]}"
                break
        res.elapsed = time.monotonic() - t0
        if res.error:
            self.stats["errors"] += 1
        if use_cache:
            await self.cache_put(key, res)
        return res

    async def head_alive(self, url: str) -> tuple[bool, int | None, str]:
        """Cheap reachability probe (used before crawling)."""
        try:
            async with self._global, self.throttle((urlparse(url).hostname or "").lower()):
                r = await self.client.get(url, headers=browser_headers(), timeout=12.0)
                return r.status_code < 500, r.status_code, str(r.url)
        except Exception as e:
            return False, None, f"{type(e).__name__}"


_fetcher: Fetcher | None = None


def get_fetcher() -> Fetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = Fetcher()
    return _fetcher
