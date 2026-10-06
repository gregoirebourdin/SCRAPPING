"""Empirical source scoring — generic counters (docs/ARCHITECTURE.md §Empirical Source Scoring).

One row per ``(dimension, key)`` in ``resolver_stats``:

* **attempts** — the resolver/source was tried; **successes** — the attempt produced a usable result
  (coverage = successes / attempts);
* **outcomes** — ground truth arrived later: confirmed correct (SMTP accepted on a proven domain, a user
  kept/approved the value, a benchmark matched), confirmed wrong (a user replaced the value, SMTP rejected,
  a discovered company was off-ICP), or inconclusive;
* latency and cost totals (averages per attempt).

``precision()`` / ``coverage()`` turn counts into Beta-smoothed estimates around the code's default prior
(``PRIOR_STRENGTH`` pseudo-observations), so priors (``scout.learning.priors``) are only a starting point.

Dimensions in use: email ``resolver`` / ``source`` / ``pattern`` / ``technique`` / ``provider`` (the email
engine, docs/EMAIL_ENGINE.md §Empirical scoring), ``discovery.source``, ``people.source``,
``enrich.resolver``, ``search.engine``, ``crawl.tier``.

Bookkeeping must never break the product: ``record()`` and ``snapshot()`` never raise (a stats outage logs
once and the callers fall back to priors).
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.models import ResolverStat

log = structlog.get_logger("learning.stats")

PRIOR_STRENGTH = 20.0  # pseudo-observations behind the default prior
SNAPSHOT_TTL_S = 300.0
SNAPSHOT_RETRY_S = 30.0  # after a failed load, serve the last good snapshot this long before retrying
WILSON_Z_90 = 1.6448536269514722
KEY_MAX_LEN = 200


@dataclass(frozen=True)
class StatEvent:
    """One attempt (``outcome=False``) or one ground-truth outcome (``outcome=True``) for a resolver/source.

    ``produced`` (attempts only) says whether the attempt produced a usable result. ``None`` means the
    caller does not track coverage and only records attempts that produced something (the email engine), so
    it counts as produced; pass ``produced=False`` for empty attempts.
    """

    dimension: str
    key: str
    correct: bool | None = None  # outcome: True correct / False wrong / None inconclusive
    outcome: bool = False  # True when this event reports an outcome rather than an attempt
    produced: bool | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class StatRow:
    attempts: int
    correct: int
    wrong: int
    inconclusive: int
    latency_ms_total: int
    successes: int = 0
    cost_usd_total: float = 0.0
    last_outcome_at: datetime | None = None

    @property
    def judged(self) -> int:
        return self.correct + self.wrong

    @property
    def avg_latency_ms(self) -> float | None:
        return self.latency_ms_total / self.attempts if self.attempts else None

    @property
    def avg_cost_usd(self) -> float | None:
        return self.cost_usd_total / self.attempts if self.attempts else None


Snapshot = Mapping[tuple[str, str], StatRow]

_snapshot: dict[tuple[str, str], StatRow] = {}
_snapshot_at = 0.0
_failed_at = 0.0  # monotonic time of the last failed load (0: healthy)


# ---------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------
@dataclass
class _Agg:
    attempts: int = 0
    successes: int = 0
    correct: int = 0
    wrong: int = 0
    inconclusive: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    outcome: bool = False


def _aggregate(events: Iterable[StatEvent]) -> list[tuple[str, str, _Agg]]:
    """Sum events per (dimension, key); sorted so concurrent writers always lock rows in the same order."""
    out: dict[tuple[str, str], _Agg] = {}
    for e in events:
        if not e.key or not e.dimension:
            continue
        a = out.setdefault((e.dimension, e.key[:KEY_MAX_LEN]), _Agg())
        if e.outcome:
            a.outcome = True
            if e.correct is True:
                a.correct += 1
            elif e.correct is False:
                a.wrong += 1
            else:
                a.inconclusive += 1
        else:
            a.attempts += 1
            a.successes += 0 if e.produced is False else 1
        a.latency_ms += max(0, int(e.latency_ms or 0))
        a.cost_usd += max(0.0, float(e.cost_usd or 0.0))
    return [(d, k, a) for (d, k), a in sorted(out.items())]


async def _upsert(s: AsyncSession, rows: list[tuple[str, str, _Agg]]) -> None:
    T = ResolverStat
    for dimension, key, a in rows:
        ins = pg_insert(T).values(
            dimension=dimension,
            key=key,
            attempts=a.attempts,
            successes=a.successes,
            confirmed_correct=a.correct,
            confirmed_wrong=a.wrong,
            inconclusive=a.inconclusive,
            latency_ms_total=a.latency_ms,
            cost_usd_total=Decimal(str(round(a.cost_usd, 6))),
            last_outcome_at=sa.func.now() if a.outcome else None,
        )
        await s.execute(
            ins.on_conflict_do_update(
                index_elements=[T.dimension, T.key],
                set_={
                    "attempts": T.attempts + ins.excluded.attempts,
                    "successes": T.successes + ins.excluded.successes,
                    "confirmed_correct": T.confirmed_correct + ins.excluded.confirmed_correct,
                    "confirmed_wrong": T.confirmed_wrong + ins.excluded.confirmed_wrong,
                    "inconclusive": T.inconclusive + ins.excluded.inconclusive,
                    "latency_ms_total": T.latency_ms_total + ins.excluded.latency_ms_total,
                    "cost_usd_total": T.cost_usd_total + ins.excluded.cost_usd_total,
                    "last_outcome_at": sa.func.coalesce(ins.excluded.last_outcome_at, T.last_outcome_at),
                    "updated_at": sa.func.now(),
                },
            )
        )


async def record(events: Iterable[StatEvent]) -> None:
    """Upsert counters for a batch of events in one transaction. Never raises (logs and drops the batch)."""
    try:
        rows = _aggregate(events)
        if not rows:
            return
        async with session_scope() as s:
            await _upsert(s, rows)
    except Exception as exc:
        log.warning("learning.record_failed", error=str(exc)[:300])


async def record_in(session: AsyncSession, events: Iterable[StatEvent]) -> None:
    """Record inside the caller's transaction (atomic with a user edit) under a SAVEPOINT. Never raises."""
    try:
        rows = _aggregate(events)
        if not rows:
            return
        async with session.begin_nested():
            await _upsert(session, rows)
    except Exception as exc:
        log.warning("learning.record_failed", error=str(exc)[:300])


async def record_outcome(dimension: str, key: str, correct: bool | None) -> None:
    """One ground-truth outcome (benchmark harness, feedback): True correct, False wrong, None inconclusive."""
    await record([StatEvent(dimension, key, correct=correct, outcome=True)])


# ---------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------
def row_from_model(r: Any) -> StatRow:
    return StatRow(
        attempts=int(r.attempts or 0),
        correct=int(r.confirmed_correct or 0),
        wrong=int(r.confirmed_wrong or 0),
        inconclusive=int(r.inconclusive or 0),
        latency_ms_total=int(r.latency_ms_total or 0),
        successes=int(r.successes or 0),
        cost_usd_total=float(r.cost_usd_total or 0),
        last_outcome_at=r.last_outcome_at,
    )


async def snapshot(force: bool = False) -> dict[tuple[str, str], StatRow]:
    """All counters, cached in-process for a few minutes (they move slowly).

    Never raises: on a database error it logs once and returns the last good snapshot (or ``{}``, so the
    priors apply), retrying at most every ``SNAPSHOT_RETRY_S`` seconds.
    """
    global _snapshot, _snapshot_at, _failed_at
    now = time.monotonic()
    if not force and _snapshot and now - _snapshot_at < SNAPSHOT_TTL_S:
        return _snapshot
    if not force and _failed_at and now - _failed_at < SNAPSHOT_RETRY_S:
        return _snapshot
    try:
        async with session_scope() as s:
            rows = (await s.execute(sa.select(ResolverStat))).scalars().all()
    except Exception as exc:
        if not _failed_at:
            log.warning("learning.snapshot_failed", error=str(exc)[:300], fallback_rows=len(_snapshot))
        _failed_at = time.monotonic()
        return _snapshot
    if _failed_at:
        log.info("learning.snapshot_recovered")
    _failed_at = 0.0
    _snapshot = {(r.dimension, r.key): row_from_model(r) for r in rows}
    _snapshot_at = time.monotonic()
    return _snapshot


def cached_snapshot() -> dict[tuple[str, str], StatRow]:
    """The last loaded snapshot without any I/O (``{}`` before the first load). For sync call sites."""
    return _snapshot


def reset_cache() -> None:
    global _snapshot, _snapshot_at, _failed_at
    _snapshot, _snapshot_at, _failed_at = {}, 0.0, 0.0


# ---------------------------------------------------------------------------------------------
# Estimates (pure)
# ---------------------------------------------------------------------------------------------
def _row(dimension: str, key: str, snap: Snapshot | None) -> StatRow | None:
    try:
        return (snap or {}).get((dimension, key))
    except Exception:  # never let a malformed snapshot break a hot path
        return None


def beta_mean(hits: int, n: int, prior: float, strength: float = PRIOR_STRENGTH) -> float:
    """(hits + k·prior) / (n + k)."""
    return (hits + strength * prior) / (n + strength)


def precision(dimension: str, key: str, prior: float, snap: Snapshot | None = None) -> float:
    """Beta-smoothed precision: (correct + k·prior) / (correct + wrong + k). ``prior`` without data."""
    row = _row(dimension, key, snap)
    if row is None:
        return prior
    return beta_mean(row.correct, row.correct + row.wrong, prior)


def coverage(dimension: str, key: str, prior: float, snap: Snapshot | None = None) -> float:
    """Beta-smoothed coverage: (successes + k·prior) / (attempts + k). ``prior`` without data."""
    row = _row(dimension, key, snap)
    if row is None:
        return prior
    return beta_mean(row.successes, row.attempts, prior)


def wilson_interval(hits: int, n: int, z: float = WILSON_Z_90) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion (90 % by default); None without observations."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def describe(dimension: str, key: str, snap: Snapshot | None = None) -> str | None:
    row = _row(dimension, key, snap)
    if row is None or row.correct + row.wrong == 0:
        return None
    return f"{row.correct}/{row.correct + row.wrong} confirmed correct historically"
