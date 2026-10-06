"""Empirical Source Scoring on Postgres: resolver_stats counters, outage safety, feedback paths, admin API."""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa

import scout.enrich.jobs  # noqa: F401 — registers the enrichment.batch handler
from scout.ai.factory import LocalProvider, set_ai
from scout.auth.context import issue_service_token
from scout.db.engine import session_scope
from scout.db.enums import EntityType, SourceType
from scout.db.models import ResolverStat, User, WorkspaceMember
from scout.discovery.health import health_snapshot, record_outcomes, record_request
from scout.enrich.engine import create_column, enqueue_column, set_user_value
from scout.jobs.worker import Worker
from scout.learning import stats as S
from scout.services import leads, registry
from tests.unit.enrich.helpers import fake_crawl_module, seed_agencies

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_cache():
    S.reset_cache()
    yield
    S.reset_cache()


async def _stat(dimension: str, key: str) -> ResolverStat | None:
    async with session_scope() as s:
        return await s.get(ResolverStat, (dimension, key))


# ---- counters ---------------------------------------------------------------------------------------
async def test_record_snapshot_precision_coverage(db) -> None:
    await S.record(
        [
            S.StatEvent("people.source", "official_team_page", produced=True, latency_ms=40),
            S.StatEvent("people.source", "official_team_page", produced=False, latency_ms=20),
            S.StatEvent("people.source", "official_team_page", produced=True, cost_usd=0.0015),
            S.StatEvent("resolver", "github", latency_ms=7),  # legacy email attempt (produced=None)
        ]
    )
    row = await _stat("people.source", "official_team_page")
    assert row is not None and (row.attempts, row.successes, row.latency_ms_total) == (3, 2, 60)
    assert float(row.cost_usd_total) == pytest.approx(0.0015) and row.last_outcome_at is None

    await S.record_outcome("people.source", "official_team_page", True)
    await S.record_outcome("people.source", "official_team_page", False)
    await S.record_outcome("people.source", "official_team_page", None)
    row = await _stat("people.source", "official_team_page")
    assert (row.confirmed_correct, row.confirmed_wrong, row.inconclusive) == (1, 1, 1)
    assert row.last_outcome_at is not None and row.attempts == 3

    snap = await S.snapshot(force=True)
    r = snap[("people.source", "official_team_page")]
    assert (r.attempts, r.successes, r.correct, r.wrong, r.inconclusive) == (3, 2, 1, 1, 1)
    assert snap[("resolver", "github")].successes == 1  # email attempts count as produced
    assert S.precision("people.source", "official_team_page", 0.88, snap) == pytest.approx((1 + 17.6) / 22)
    assert S.coverage("people.source", "official_team_page", 0.7, snap) == pytest.approx((2 + 14) / 23)
    assert await S.snapshot() is snap  # cached


async def test_stats_outage_never_blocks_callers(db) -> None:
    await S.record([S.StatEvent("enrich.resolver", "keyword", produced=True)])
    good = await S.snapshot(force=True)
    assert ("enrich.resolver", "keyword") in good
    async with session_scope() as s:
        await s.execute(sa.text("ALTER TABLE resolver_stats RENAME TO resolver_stats_offline"))
    try:
        await S.record([S.StatEvent("enrich.resolver", "keyword", produced=True)])  # swallowed
        await S.record_outcome("enrich.resolver", "keyword", False)  # swallowed
        assert await S.snapshot(force=True) is good  # last good snapshot
        S.reset_cache()
        assert await S.snapshot(force=True) == {}  # nothing cached: {} → priors
        # the email engine path keeps resolving on priors
        from scout.email.stats import precision

        assert precision("resolver", "github", 0.82, await S.snapshot()) == 0.82
        # a savepoint failure inside a user transaction leaves that transaction usable
        async with session_scope() as s:
            await S.record_in(s, [S.StatEvent("enrich.resolver", "keyword", correct=False, outcome=True)])
            assert await s.scalar(sa.select(sa.literal(1))) == 1
    finally:
        async with session_scope() as s:
            await s.execute(sa.text("ALTER TABLE resolver_stats_offline RENAME TO resolver_stats"))
    row = await _stat("enrich.resolver", "keyword")
    assert row is not None and row.attempts == 1  # outage writes were dropped, not half-applied


async def test_discovery_health_feeds_discovery_source(db) -> None:
    await record_request("fr_registry", ok=True, latency_ms=100, results=12)
    await record_request("fr_registry", ok=True, latency_ms=50, results=0)
    await record_request("fr_registry", ok=False, blocked=True, latency_ms=10)
    await record_outcomes("fr_registry", qualified=2, duplicates=1)
    row = await _stat("discovery.source", "fr_registry")
    assert row is not None and (row.attempts, row.successes, row.latency_ms_total) == (3, 1, 160)
    assert (row.confirmed_correct, row.confirmed_wrong) == (2, 0)
    from scout.learning.feedback import discovery_outcome

    await discovery_outcome("fr_registry", wrong=1)  # off-ICP rejection (processor._finish)
    assert (await _stat("discovery.source", "fr_registry")).confirmed_wrong == 1
    await health_snapshot()  # warms the snapshot the (sync) router reads
    assert S.cached_snapshot()[("discovery.source", "fr_registry")].attempts == 3


# ---- feedback: enrichment cell override → wrong ---------------------------------------------------------------
@pytest.fixture
def crawl(monkeypatch):
    mod = fake_crawl_module()
    monkeypatch.setitem(sys.modules, "scout.crawl.cache", mod)
    return mod.state


@pytest.fixture
def local_ai():
    set_ai(LocalProvider())
    yield
    set_ai(None)


async def test_cell_override_marks_resolver_wrong(workspace, crawl, local_ai) -> None:
    ws, user = workspace
    ids = await seed_agencies(ws)
    col = await create_column(
        ws,
        name="Mentions Instagram",
        instruction="Whether their site mentions Instagram",
        list_id=ids["list"],
    )
    assert col.configuration["strategy"] == "keyword"
    await enqueue_column(ws, col.id)
    await Worker(slots=2, types=["enrichment.batch"]).run_until_idle(timeout_s=60)
    row = await _stat("enrich.resolver", "keyword")
    assert row is not None and (row.attempts, row.successes) == (2, 2)  # one attempt per computed cell

    async with session_scope() as s:
        from scout.db.models import CustomFieldValue

        cell = await s.scalar(
            sa.select(CustomFieldValue).where(
                CustomFieldValue.column_id == col.id, CustomFieldValue.entity_id == ids["lumiere"]
            )
        )
    assert cell.value_json is True and not cell.is_user_override
    await set_user_value(ws, col.id, EntityType.company, ids["lumiere"], False, user_id=user)
    row = await _stat("enrich.resolver", "keyword")
    assert (row.confirmed_wrong, row.confirmed_correct) == (1, 0) and row.last_outcome_at is not None
    assert (await _stat("enrich.resolver", "user")).confirmed_correct == 1
    # editing a user-owned cell again never counts against the resolver twice
    await set_user_value(ws, col.id, EntityType.company, ids["lumiere"], True, user_id=user)
    assert (await _stat("enrich.resolver", "keyword")).confirmed_wrong == 1
    # confirming an automatic value (same value) → correct
    await set_user_value(ws, col.id, EntityType.company, ids["ruche"], True, user_id=user)
    assert (await _stat("enrich.resolver", "keyword")).confirmed_correct == 1


# ---- feedback: person field edit / approval ------------------------------------------------------------------
async def _person(ws: uuid.UUID, name: str, title: str, url: str):
    ev = registry.Evidence(
        source_type=SourceType.website, confidence=0.88, source_key="website", source_url=url, evidence=name
    )
    async with session_scope() as s:
        comp, _ = await registry.upsert_company(
            s,
            ws,
            registry.CompanyInput(name=f"Agence {name}", website=f"https://{uuid.uuid4().hex[:8]}.fr"),
            ev,
        )
        p, _ = await registry.upsert_person(
            s, ws, company_id=comp.id, full_name=name, job_title=title, evidence=ev
        )
        return p.id


async def test_person_edit_and_approval_feedback(workspace) -> None:
    ws, user = workspace
    pid = await _person(ws, "Marie Dupont", "CTO", "https://agence.fr/equipe")
    async with session_scope() as s:
        await leads.edit_field(s, ws, EntityType.person, pid, "job_title", "CEO", user_id=user)
    row = await _stat("people.source", "official_team_page")
    assert row is not None and (row.confirmed_wrong, row.confirmed_correct) == (1, 0)
    assert (await _stat("people.source", "user")).confirmed_correct == 1
    async with session_scope() as s:  # value is user-owned now: no second strike
        await leads.edit_field(s, ws, EntityType.person, pid, "job_title", "Founder", user_id=user)
    assert (await _stat("people.source", "official_team_page")).confirmed_wrong == 1

    other = await _person(ws, "Paul Martin", "Gérant", "https://autre.fr/mentions-legales")
    async with session_scope() as s:
        assert await leads.approve_review(s, ws, EntityType.person, [other], user_id=user) == 1
    legal = await _stat("people.source", "legal_notice")
    assert legal is not None and legal.confirmed_correct == 2  # name + title held by the legal notice
    async with session_scope() as s:  # approving again: already user-confirmed → no double count
        await leads.approve_review(s, ws, EntityType.person, [other], user_id=user)
    assert (await _stat("people.source", "legal_notice")).confirmed_correct == 2


# ---- API --------------------------------------------------------------------------------------------------
@pytest.fixture
async def client(db) -> AsyncIterator[httpx.AsyncClient]:
    from scout.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://scout.test"
    ) as c:
        yield c


def _auth(user: str, ws: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_service_token(user)}", "X-Workspace-Id": str(ws)}


async def test_learning_sources_endpoint(client, workspace) -> None:
    ws, owner = workspace
    for correct in [True] * 18 + [False] * 2:
        await S.record_outcome("people.source", "official_team_page", correct)
    await S.record(
        [S.StatEvent("people.source", "official_team_page", produced=p) for p in [True] * 30 + [False] * 10]
    )

    r = await client.get(
        "/v1/learning/sources", params={"dimension": "people.source"}, headers=_auth(owner, ws)
    )
    assert r.status_code == 200, r.text
    rows = {x["key"]: x for x in r.json()}
    assert {x["dimension"] for x in r.json()} == {"people.source"}
    team = rows["official_team_page"]
    assert (team["attempts"], team["successes"], team["confirmed_correct"], team["confirmed_wrong"]) == (
        40,
        30,
        18,
        2,
    )
    assert team["coverage"] == 0.75 and team["raw_precision"] == 0.9 and team["prior"] == 0.88
    lo, hi = team["precision_interval"]
    assert lo < 0.9 < hi
    assert team["precision"] == pytest.approx((18 + 20 * 0.88) / 40, abs=1e-4)
    assert team["evidence_level"] == "learned" and team["label"] == "Official team page"
    assert rows["gemini_grounded_result"]["evidence_level"] == "prior only"  # priors listed too
    assert (
        rows["gemini_grounded_result"]["attempts"] == 0 and rows["gemini_grounded_result"]["coverage"] is None
    )

    all_rows = (await client.get("/v1/learning/sources", headers=_auth(owner, ws))).json()
    dims = [x["dimension"] for x in all_rows]
    assert dims.index("discovery.source") < dims.index("people.source") < dims.index("enrich.resolver")

    member = "user_" + uuid.uuid4().hex[:10]
    async with session_scope() as s:
        s.add(User(id=member, name="Member", email=f"{member}@example.com"))
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws, user_id=member, role="member"))
    r = await client.get("/v1/learning/sources", headers=_auth(member, ws))
    assert r.status_code == 403
