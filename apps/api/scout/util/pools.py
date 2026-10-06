"""Bounded concurrency pools per integration + per-domain politeness (spec §98)."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from scout.config import get_settings

PoolName = Literal[
    "http", "browser", "maps", "gemini", "search", "smtp", "public_api", "scrapling", "scrapling_dynamic"
]

_pools: dict[str, asyncio.Semaphore] = {}
_domain_sems: dict[str, asyncio.Semaphore] = {}
_domain_last: dict[str, float] = defaultdict(float)
_domain_locks: dict[str, asyncio.Lock] = {}


def _size(name: str) -> int:
    s = get_settings()
    return {
        "http": s.pool_http,
        "browser": s.pool_browser,
        "maps": s.pool_maps,
        "gemini": s.pool_gemini,
        "search": s.pool_search,
        "smtp": s.pool_smtp,
        "public_api": s.pool_public_api,
        "scrapling": s.pool_scrapling,
        "scrapling_dynamic": s.pool_scrapling_dynamic,
    }.get(name, 4)


def pool(name: PoolName) -> asyncio.Semaphore:
    sem = _pools.get(name)
    if sem is None:
        sem = asyncio.Semaphore(_size(name))
        _pools[name] = sem
    return sem


@asynccontextmanager
async def domain_slot(domain: str) -> AsyncIterator[None]:
    """At most N concurrent requests per domain, with a minimum spacing between requests."""
    s = get_settings()
    sem = _domain_sems.get(domain)
    if sem is None:
        sem = asyncio.Semaphore(s.per_domain_concurrency)
        _domain_sems[domain] = sem
    lock = _domain_locks.setdefault(domain, asyncio.Lock())
    async with sem:
        async with lock:
            wait = (_domain_last[domain] + s.per_domain_delay_ms / 1000.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _domain_last[domain] = time.monotonic()
        yield


def reset_pools() -> None:
    """Tests: event loops change between test sessions."""
    _pools.clear()
    _domain_sems.clear()
    _domain_locks.clear()
    _domain_last.clear()
