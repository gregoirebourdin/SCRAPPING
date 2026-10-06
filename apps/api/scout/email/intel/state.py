"""In-process state shared by the domain-intelligence modules.

* a TTL cache of assembled :class:`DomainIntel` (the database row is the durable cache);
* one ``asyncio.Lock`` per domain so concurrent contacts of the same domain trigger ONE build;
* pending in-memory cache-hit counters (flushed into ``domain_profiles.stats`` by the next build);
* change flags (``MX_CHANGED``, ``PATTERN_CHANGED``, ``CATCH_ALL_FLIPPED``) recorded in the profile.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import defaultdict
from collections.abc import Hashable, Mapping
from datetime import UTC, datetime
from typing import Any

from scout.email.contracts import DomainIntel

PROFILE_TTL_S = 600.0  # in-process reuse window (10 minutes)
MAX_ENTRIES = 5000
MAX_FLAGS = 20

MX_CHANGED = "MX_CHANGED"
PATTERN_CHANGED = "PATTERN_CHANGED"
CATCH_ALL_FLIPPED = "CATCH_ALL_FLIPPED"

COMPONENTS = ("mx", "website", "github", "rdap", "patterns")

_entries: dict[Hashable, tuple[float, DomainIntel]] = {}
_locks: dict[str, asyncio.Lock] = {}
_locks_loop: asyncio.AbstractEventLoop | None = None
_pending_hits: dict[str, int] = defaultdict(int)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---- network evidence switch ------------------------------------------------------------------

_network_override: bool | None = None


def network_evidence_enabled() -> bool:
    """GitHub / RDAP calls allowed? Off by default under ``APP_ENV=test`` (no internet in tests)."""
    if _network_override is not None:
        return _network_override
    from scout.config import get_settings

    return get_settings().app_env != "test"


def set_network_evidence(enabled: bool | None) -> None:
    """Force GitHub / RDAP calls on or off (None restores the environment default). Tests."""
    global _network_override
    _network_override = enabled


# ---- locks ------------------------------------------------------------------------------------


def lock_for(domain: str) -> asyncio.Lock:
    """Per-domain build lock (recreated when the event loop changes, e.g. between test sessions)."""
    global _locks_loop
    loop = asyncio.get_running_loop()
    if _locks_loop is not loop:
        _locks.clear()
        _locks_loop = loop
    lock = _locks.get(domain)
    if lock is None:
        if len(_locks) > MAX_ENTRIES:
            for key in [k for k, v in _locks.items() if not v.locked()]:
                _locks.pop(key, None)
        lock = _locks[domain] = asyncio.Lock()
    return lock


# ---- TTL cache --------------------------------------------------------------------------------


def snapshot(intel: DomainIntel, **changes: Any) -> DomainIntel:
    """Shallow copy with fresh containers, so callers never mutate the cached object."""
    return dataclasses.replace(
        intel,
        mx_hosts=list(intel.mx_hosts),
        patterns=list(intel.patterns),
        observed=list(intel.observed),
        evidence=list(intel.evidence),
        cache_hits=dict(intel.cache_hits),
        **changes,
    )


def cache_get(key: Hashable) -> DomainIntel | None:
    entry = _entries.get(key)
    if entry is None:
        return None
    expires, intel = entry
    if expires < time.monotonic():
        _entries.pop(key, None)
        return None
    return snapshot(intel)


def cache_put(key: Hashable, intel: DomainIntel, ttl: float = PROFILE_TTL_S) -> None:
    if len(_entries) >= MAX_ENTRIES:
        now = time.monotonic()
        for k in [k for k, (exp, _) in _entries.items() if exp < now]:
            _entries.pop(k, None)
        if len(_entries) >= MAX_ENTRIES:
            _entries.pop(next(iter(_entries)))
    _entries[key] = (time.monotonic() + ttl, snapshot(intel))


def invalidate(domain: str) -> None:
    """Drop every cached view of a domain (after relearning, new samples or new SMTP facts)."""
    for key in [k for k in _entries if isinstance(k, tuple) and k and k[0] == domain]:
        _entries.pop(key, None)


def count_hit(domain: str) -> None:
    _pending_hits[domain] += 1


def take_hits(domain: str) -> int:
    return _pending_hits.pop(domain, 0)


def take_all_hits() -> dict[str, int]:
    hits = dict(_pending_hits)
    _pending_hits.clear()
    return hits


def reset() -> None:
    """Tests: forget cached profiles, locks and pending counters."""
    global _locks_loop
    _entries.clear()
    _locks.clear()
    _locks_loop = None
    _pending_hits.clear()


# ---- change flags -----------------------------------------------------------------------------


def add_flag(stats: dict[str, Any], flag: str, detail: dict[str, Any], now: datetime) -> dict[str, Any]:
    """New stats dict with `flag` appended (last MAX_FLAGS kept) and ``invalidated_at`` bumped.

    ``invalidated_at`` tells consumers that verdicts computed before it (SMTP results, cached
    statuses, pattern-derived guesses) should be re-verified.
    """
    out = dict(stats)
    entry = {"flag": flag, "at": iso(now), **detail}
    out["flags"] = [*list(out.get("flags") or []), entry][-MAX_FLAGS:]
    out["invalidated_at"] = iso(now)
    return out


def flag_evidence(stats: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Profile evidence entries for the recorded change flags."""
    out = []
    for f in stats.get("flags") or []:
        if not isinstance(f, dict):
            continue
        detail = {k: v for k, v in f.items() if k not in ("flag", "at")}
        out.append(
            {
                "signal": f.get("flag"),
                "value": detail,
                "source": "domain_intel",
                "source_url": None,
                "observed_at": f.get("at"),
            }
        )
    return out
