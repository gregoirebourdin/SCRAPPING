"""scout.email.stats stays a drop-in alias of scout.learning.stats; bookkeeping never raises (no DB needed)."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager

import pytest

import scout.email.stats as email_stats
import scout.learning.stats as S
from scout.db.models import EmailResolverStat, ResolverStat


def test_email_stats_reexports_the_same_objects() -> None:
    for name in ("StatEvent", "StatRow", "record", "snapshot", "precision", "describe", "reset_cache"):
        assert getattr(email_stats, name) is getattr(S, name)
    assert email_stats.PRIOR_STRENGTH == 20.0 and email_stats.SNAPSHOT_TTL_S == 300.0
    assert EmailResolverStat is ResolverStat and ResolverStat.__tablename__ == "resolver_stats"


def test_legacy_constructors_and_signatures() -> None:
    # positional StatRow (email tests) and keyword StatEvent (email engine) keep working
    r = email_stats.StatRow(200, 20, 180, 0, 0)
    assert (r.attempts, r.correct, r.wrong, r.successes, r.cost_usd_total) == (200, 20, 180, 0, 0.0)
    ev = email_stats.StatEvent("resolver", "github", correct=None, outcome=False, latency_ms=12)
    assert ev.produced is None and ev.cost_usd == 0.0
    assert list(inspect.signature(S.precision).parameters) == ["dimension", "key", "prior", "snap"]
    assert "force" in inspect.signature(S.snapshot).parameters


def test_precision_matches_legacy_formula() -> None:
    snap = {
        ("resolver", "github"): S.StatRow(
            attempts=200, correct=20, wrong=180, inconclusive=0, latency_ms_total=0
        )
    }
    assert S.precision("resolver", "github", 0.82, snap) == pytest.approx((20 + 20 * 0.82) / (200 + 20))
    assert S.precision("resolver", "github", 0.82, None) == 0.82
    assert S.precision("resolver", "gitlab", 0.7, snap) == 0.7
    assert S.describe("resolver", "github", snap) == "20/200 confirmed correct historically"
    assert S.describe("resolver", "gitlab", snap) is None


def test_coverage_is_beta_smoothed() -> None:
    snap = {("enrich.resolver", "keyword"): S.StatRow(100, 0, 0, 0, 0, successes=50)}
    assert S.coverage("enrich.resolver", "keyword", 0.9, snap) == pytest.approx((50 + 18) / 120)
    assert S.coverage("enrich.resolver", "regex", 0.9, snap) == 0.9


def test_aggregate_sums_and_orders_events() -> None:
    rows = S._aggregate(
        [
            S.StatEvent("b", "k", produced=True, latency_ms=10, cost_usd=0.01),
            S.StatEvent("b", "k", produced=False, latency_ms=5),
            S.StatEvent("b", "k"),  # produced=None: legacy attempt that yielded a candidate
            S.StatEvent("a", "k", correct=True, outcome=True),
            S.StatEvent("a", "k", correct=False, outcome=True),
            S.StatEvent("a", "k", correct=None, outcome=True),
            S.StatEvent("a", "", correct=True, outcome=True),  # dropped: no key
        ]
    )
    assert [(d, k) for d, k, _ in rows] == [("a", "k"), ("b", "k")]  # sorted: stable lock order
    a, b = rows[0][2], rows[1][2]
    assert (a.attempts, a.correct, a.wrong, a.inconclusive, a.outcome) == (0, 1, 1, 1, True)
    assert (b.attempts, b.successes, b.latency_ms, b.outcome) == (3, 2, 15, False)
    assert b.cost_usd == pytest.approx(0.01)


class _Boom(Exception):
    pass


@asynccontextmanager
async def _broken_session():
    raise _Boom('relation "resolver_stats" does not exist')
    yield  # pragma: no cover


async def test_record_and_snapshot_never_raise_on_db_errors(monkeypatch) -> None:
    monkeypatch.setattr(S, "session_scope", _broken_session)
    await S.record([S.StatEvent("resolver", "github", produced=True)])  # logged + swallowed
    await S.record_outcome("resolver", "github", True)
    snap = await S.snapshot()
    assert snap == {} and await S.snapshot(force=True) == {}
    assert email_stats.precision("resolver", "github", 0.82, snap) == 0.82  # priors apply

    # with a last good snapshot, an outage serves it (and does not hammer the DB on every call)
    good = {("resolver", "github"): S.StatRow(10, 9, 1, 0, 0)}
    monkeypatch.setattr(S, "_snapshot", good)
    monkeypatch.setattr(S, "_snapshot_at", 0.0)  # expired
    monkeypatch.setattr(S, "_failed_at", 0.0)  # healthy so far → reload → fails → last good
    assert await S.snapshot() is good
    calls = 0

    @asynccontextmanager
    async def counting():
        nonlocal calls
        calls += 1
        raise _Boom("still down")
        yield  # pragma: no cover

    monkeypatch.setattr(S, "session_scope", counting)
    for _ in range(5):
        assert await S.snapshot() is good
    assert calls == 0  # within SNAPSHOT_RETRY_S after the failure: no new attempt


async def test_record_in_never_raises() -> None:
    class Session:
        def begin_nested(self):
            raise _Boom("no savepoint")

    await S.record_in(Session(), [S.StatEvent("enrich.resolver", "keyword", correct=False, outcome=True)])  # type: ignore[arg-type]
