"""Source health: atomic counters on ``sources`` + automatic cooldown of failing sources.

After ``FAILURE_THRESHOLD`` consecutive failures/blocks a source is unhealthy for 15 min; every further failure
doubles the cooldown (capped at 6 h). Any success resets the streak and clears the cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.models import Source
from scout.discovery.catalog import SOURCE_CATALOG

log = structlog.get_logger(__name__)

FAILURE_THRESHOLD = 5
BASE_COOLDOWN_MIN = 15
MAX_COOLDOWN_MIN = 360


@dataclass
class SourceHealth:
    key: str
    success_rate: float
    block_rate: float
    avg_latency_ms: float
    results_per_query: float
    duplicate_rate: float
    qualification_rate: float
    unhealthy_until: datetime | None
    healthy: bool
    requests: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None


def _insert_defaults(key: str) -> dict[str, object]:
    entry = SOURCE_CATALOG.get(key)
    return {
        "key": key,
        "name": entry.name if entry else key,
        "kind": entry.kind if entry else "discovery",
        "description": entry.description if entry else None,
        "quality_score": entry.quality if entry else 0.5,
        "priority": entry.priority if entry else 0,
    }


async def record_request(
    key: str,
    *,
    ok: bool,
    blocked: bool = False,
    latency_ms: int = 0,
    results: int = 0,
    error: str | None = None,
) -> None:
    """Count one adapter request (atomic upsert). A blocked request always counts as a failure."""
    ok = ok and not blocked
    t = Source.__table__
    streak = t.c.consecutive_failures + 1
    cooldown_min = sa.cast(
        sa.func.least(MAX_COOLDOWN_MIN, BASE_COOLDOWN_MIN * sa.func.power(2, streak - FAILURE_THRESHOLD)),
        sa.Integer,
    )
    err = (error or ("blocked" if blocked else "error"))[:500] if not ok else None
    stmt = pg_insert(Source).values(
        **_insert_defaults(key),
        requests=1,
        successes=int(ok),
        failures=int(not ok),
        blocks=int(blocked),
        results=max(0, results),
        total_latency_ms=max(0, latency_ms),
        consecutive_failures=0 if ok else 1,
        last_error=err,
        last_success_at=sa.func.now() if ok else None,
    )
    common: dict[str, Any] = {
        "requests": t.c.requests + 1,
        "results": t.c.results + max(0, results),
        "total_latency_ms": t.c.total_latency_ms + max(0, latency_ms),
        "updated_at": sa.func.now(),
    }
    set_: dict[str, Any]
    if ok:
        set_ = {
            **common,
            "successes": t.c.successes + 1,
            "consecutive_failures": 0,
            "last_success_at": sa.func.now(),
            "unhealthy_until": None,
        }
    else:
        set_ = {
            **common,
            "failures": t.c.failures + 1,
            "blocks": t.c.blocks + int(blocked),
            "consecutive_failures": streak,
            "last_error": err,
            "unhealthy_until": sa.case(
                (
                    streak >= FAILURE_THRESHOLD,
                    sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, cooldown_min),
                ),
                else_=t.c.unhealthy_until,
            ),
        }
    upsert = stmt.on_conflict_do_update(index_elements=[Source.key], set_=set_).returning(
        Source.consecutive_failures, Source.unhealthy_until
    )
    async with session_scope() as s:
        row = (await s.execute(upsert)).one()
    if not ok and row.consecutive_failures >= FAILURE_THRESHOLD:
        log.warning(
            "source.unhealthy",
            source=key,
            streak=row.consecutive_failures,
            until=str(row.unhealthy_until),
            error=err,
        )


async def record_outcomes(key: str, *, duplicates: int = 0, qualified: int = 0) -> None:
    """Add downstream outcomes (duplicates found, leads qualified) to a source's counters."""
    if not duplicates and not qualified:
        return
    t = Source.__table__
    stmt = pg_insert(Source).values(
        **_insert_defaults(key), duplicates=max(0, duplicates), qualified=max(0, qualified)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Source.key],
        set_={
            "duplicates": t.c.duplicates + max(0, duplicates),
            "qualified": t.c.qualified + max(0, qualified),
            "updated_at": sa.func.now(),
        },
    )
    async with session_scope() as s:
        await s.execute(stmt)


def _ratio(a: int, b: int, default: float) -> float:
    return a / b if b else default


async def health_snapshot() -> dict[str, SourceHealth]:
    """Health of every known source (rates default to optimistic values until data exists)."""
    async with session_scope() as s:
        now = await s.scalar(sa.select(sa.func.now()))
        rows = (await s.scalars(sa.select(Source))).all()
    out: dict[str, SourceHealth] = {}
    for r in rows:
        cooling = r.unhealthy_until is not None and now is not None and r.unhealthy_until > now
        out[r.key] = SourceHealth(
            key=r.key,
            success_rate=_ratio(r.successes, r.requests, 1.0),
            block_rate=_ratio(r.blocks, r.requests, 0.0),
            avg_latency_ms=_ratio(r.total_latency_ms, r.requests, 0.0),
            results_per_query=_ratio(r.results, r.requests, 0.0),
            duplicate_rate=_ratio(r.duplicates, r.results, 0.0),
            qualification_rate=_ratio(r.qualified, r.results, 0.0),
            unhealthy_until=r.unhealthy_until if cooling else None,
            healthy=bool(r.enabled) and not cooling,
            requests=r.requests,
            consecutive_failures=r.consecutive_failures,
            last_error=r.last_error,
        )
    return out
