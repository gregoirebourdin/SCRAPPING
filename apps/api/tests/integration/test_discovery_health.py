"""Source health counters, cooldown escalation, catalog seeding (Postgres)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.models import Source
from scout.discovery.catalog import SOURCE_CATALOG, seed_sources, source_quality
from scout.discovery.health import health_snapshot, record_outcomes, record_request

pytestmark = pytest.mark.integration


async def _row(key: str) -> Source:
    async with session_scope() as s:
        row = await s.get(Source, key)
        assert row is not None
        return row


async def _now():
    async with session_scope() as s:
        return await s.scalar(sa.select(sa.func.now()))


async def test_counters_are_accumulated(db) -> None:
    await record_request("fr_registry", ok=True, latency_ms=120, results=25)
    await record_request("fr_registry", ok=True, latency_ms=80, results=5)
    await record_request("fr_registry", ok=False, latency_ms=50, error="upstream error 503")
    await record_request("fr_registry", ok=False, blocked=True)
    await record_outcomes("fr_registry", duplicates=6, qualified=3)
    row = await _row("fr_registry")
    assert (row.requests, row.successes, row.failures, row.blocks) == (4, 2, 2, 1)
    assert (row.results, row.total_latency_ms, row.duplicates, row.qualified) == (30, 250, 6, 3)
    assert row.consecutive_failures == 2 and row.last_error == "blocked" and row.last_success_at is not None
    assert (
        row.name == SOURCE_CATALOG["fr_registry"].name and row.quality_score == 0.95
    )  # created from the catalog
    h = (await health_snapshot())["fr_registry"]
    assert h.success_rate == 0.5 and h.block_rate == 0.25 and h.avg_latency_ms == 62.5
    assert h.results_per_query == 7.5 and h.duplicate_rate == 0.2 and h.qualification_rate == 0.1
    assert h.healthy and h.unhealthy_until is None


async def test_cooldown_after_consecutive_failures_doubles_and_resets(db) -> None:
    for _ in range(4):
        await record_request("web_search", ok=False, blocked=True)
    assert (await health_snapshot())["web_search"].healthy  # 4 failures: still healthy

    await record_request("web_search", ok=False, error="anomaly page")
    now = await _now()
    first = (await _row("web_search")).unhealthy_until
    assert first is not None and timedelta(minutes=14) < first - now <= timedelta(minutes=15, seconds=5)
    snap = (await health_snapshot())["web_search"]
    assert not snap.healthy and snap.unhealthy_until == first and snap.consecutive_failures == 5

    await record_request("web_search", ok=False)  # probe after cooldown fails again → 30 min
    second = (await _row("web_search")).unhealthy_until
    assert timedelta(minutes=29) < second - await _now() <= timedelta(minutes=30, seconds=5)

    for _ in range(10):  # capped at 6 h
        await record_request("web_search", ok=False)
    capped = (await _row("web_search")).unhealthy_until
    assert timedelta(hours=5, minutes=59) < capped - await _now() <= timedelta(hours=6, seconds=5)

    await record_request("web_search", ok=True, results=10)
    row = await _row("web_search")
    assert row.consecutive_failures == 0 and row.unhealthy_until is None
    assert (await health_snapshot())["web_search"].healthy


async def test_disabled_source_is_unhealthy(db) -> None:
    await seed_sources()
    async with session_scope() as s:
        await s.execute(sa.update(Source).where(Source.key == "osm").values(enabled=False))
    snap = await health_snapshot()
    assert not snap["osm"].healthy and snap["yc"].healthy


async def test_seed_sources_is_idempotent_and_preserves_operator_settings(db) -> None:
    n = await seed_sources()
    assert n == len(SOURCE_CATALOG)
    await seed_sources()
    async with session_scope() as s:
        count = await s.scalar(sa.select(sa.func.count()).select_from(Source))
        assert count == len(SOURCE_CATALOG)
        await s.execute(
            sa.update(Source)
            .where(Source.key == "github")
            .values(enabled=False, priority=99, quality_score=0.1, requests=7)
        )
    await seed_sources()
    row = await _row("github")
    assert row.enabled is False and row.priority == 99 and row.requests == 7  # operator data kept
    assert row.quality_score == SOURCE_CATALOG["github"].quality  # catalog metadata refreshed
    website = await _row("website")
    assert website.kind == "evidence" and website.quality_score == 0.95
    assert source_quality("user") == 1.0 and source_quality("hn") == 0.6 and source_quality("unknown") == 0.4
