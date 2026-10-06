"""robots.txt support: per-origin in-memory cache (TTL 24 h) on top of ``urllib.robotparser``.

Semantics (RFC 9309 / common crawler practice):
* 2xx → parse rules; 4xx (except 401/403) → allow all; 401/403 → disallow all;
* 5xx, network errors, SSRF refusals → fail open (allow), cached for a shorter time.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog

from scout.config import get_settings
from scout.crawl import http
from scout.errors import BlockedError, JobError, RateLimitedError

log = structlog.get_logger(__name__)

ROBOTS_TTL_S = 24 * 3600
ROBOTS_ERROR_TTL_S = 3600
ROBOTS_MAX_BYTES = 512_000


@dataclass
class RobotsInfo:
    origin: str
    parser: RobotFileParser | None = None  # None → no rules (allow all) unless disallow_all
    disallow_all: bool = False
    sitemaps: list[str] = field(default_factory=list)
    status: int | None = None
    fetched_at: float = 0.0
    expires_at: float = 0.0

    def can_fetch(self, url: str, user_agent: str) -> bool:
        if self.disallow_all:
            return False
        if self.parser is None:
            return True
        try:
            return self.parser.can_fetch(user_agent, url)
        except Exception:  # malformed rules: fail open
            return True


_cache: dict[str, RobotsInfo] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_cache() -> None:
    """Tests / long-running workers: drop every cached robots.txt."""
    _cache.clear()
    _locks.clear()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{(parts.scheme or 'https').lower()}://{(parts.netloc or '').lower()}"


def _user_agent_token() -> str:
    ua = get_settings().crawler_user_agent
    return ua.split("/", 1)[0].strip() or ua


def parse_robots(text: str, origin: str) -> RobotsInfo:
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    sitemaps = [s.strip() for s in (parser.site_maps() or []) if s.strip()]
    return RobotsInfo(origin=origin, parser=parser, sitemaps=sitemaps)


async def _load(origin: str) -> RobotsInfo:
    now = time.time()
    try:
        resp = await http.fetch(
            f"{origin}/robots.txt", accept="text/plain,*/*;q=0.5", max_bytes=ROBOTS_MAX_BYTES
        )
    except BlockedError as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            info = RobotsInfo(origin=origin, disallow_all=True, status=status)
            info.fetched_at, info.expires_at = now, now + ROBOTS_TTL_S
            return info
        if isinstance(exc, RateLimitedError):
            info = RobotsInfo(origin=origin, status=429)
            info.fetched_at, info.expires_at = now, now + ROBOTS_ERROR_TTL_S
            return info
        info = RobotsInfo(origin=origin, status=status)
        info.fetched_at, info.expires_at = now, now + ROBOTS_ERROR_TTL_S
        return info
    except JobError as exc:
        log.debug("robots_fetch_failed", origin=origin, error=str(exc))
        info = RobotsInfo(origin=origin)
        info.fetched_at, info.expires_at = now, now + ROBOTS_ERROR_TTL_S
        return info

    if resp.status_code in (401, 403):
        info = RobotsInfo(origin=origin, disallow_all=True, status=resp.status_code)
    elif 200 <= resp.status_code < 300:
        info = parse_robots(resp.text, origin)
        info.status = resp.status_code
    else:  # 404/410/other 4xx → no restrictions; 5xx → fail open
        info = RobotsInfo(origin=origin, status=resp.status_code)
    ttl = ROBOTS_ERROR_TTL_S if resp.status_code >= 500 else ROBOTS_TTL_S
    info.fetched_at, info.expires_at = now, now + ttl
    return info


async def get_robots(url: str) -> RobotsInfo:
    """Return the (cached) robots.txt info for the URL's origin."""
    origin = _origin(url)
    cached = _cache.get(origin)
    if cached and cached.expires_at > time.time():
        return cached
    lock = _locks.setdefault(origin, asyncio.Lock())
    async with lock:
        cached = _cache.get(origin)
        if cached and cached.expires_at > time.time():
            return cached
        info = await _load(origin)
        _cache[origin] = info
        return info


async def allowed(url: str) -> bool:
    """May the crawler fetch ``url``? Always True when ``crawler_respect_robots`` is off."""
    if not get_settings().crawler_respect_robots:
        return True
    info = await get_robots(url)
    return info.can_fetch(url, _user_agent_token())


async def sitemaps(url: str) -> list[str]:
    """Sitemap URLs declared in robots.txt for the URL's origin (may be empty)."""
    info = await get_robots(url)
    return list(info.sitemaps)
