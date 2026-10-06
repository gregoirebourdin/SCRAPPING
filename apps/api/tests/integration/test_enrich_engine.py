"""Enrichment engine end-to-end on Postgres: columns, fan-out, worker batches, hashing, overrides, freshness."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

import scout.enrich.jobs  # noqa: F401 — registers the enrichment.batch handler
from scout.ai.factory import FakeProvider, LocalProvider, set_ai
from scout.ai.provider import AIUsage, GroundedResult, GroundingSource
from scout.db.engine import session_scope
from scout.db.enums import CellStatus, EntityType, ExposureType, JobStatus, ResolverType, WebsiteStatus
from scout.db.models import (
    Company,
    CustomFieldValue,
    GroundedResearch,
    Job,
    JobEvent,
    LeadExposure,
    ListMembership,
    WebsitePage,
)
from scout.enrich.ai_schemas import ResearchAnswer
from scout.enrich.engine import (
    column_entity_ids,
    column_progress,
    create_column,
    enqueue_column,
    estimate_coverage,
    mark_stale_cells,
    refresh_column,
    set_user_value,
    update_column_definition,
)
from scout.jobs.worker import Worker
from scout.util.text import content_hash
from tests.unit.enrich.helpers import fake_crawl_module, load_fixture, seed_agencies, sentence_containing

pytestmark = pytest.mark.integration

IG_MGMT = "Add whether the agency actually offers Instagram management"


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


@pytest.fixture
def fake_ai():
    fake = FakeProvider()
    ruche_quote, ruche_url = sentence_containing(load_fixture("agency_instagram"), "Nous gérons vos comptes Instagram")
    lum_quote, lum_url = sentence_containing(load_fixture("agency_social_follow"), "Nous gérons vos campagnes Google Ads")

    def verdict(prompt: str) -> dict:
        if "larushesociale" in prompt:
            return {"verdict": "true", "confidence": 0.93, "evidence_quote": ruche_quote, "source_url": ruche_url}
        return {"verdict": "false", "confidence": 0.86, "evidence_quote": lum_quote, "source_url": lum_url}

    fake.on("SemanticVerdict", verdict)
    set_ai(fake)
    yield fake
    set_ai(None)


async def _run() -> None:
    await Worker(slots=4, types=["enrichment.batch"]).run_until_idle(timeout_s=60)


async def _cells(column_id) -> dict:
    async with session_scope() as s:
        rows = (await s.scalars(sa.select(CustomFieldValue).where(CustomFieldValue.column_id == column_id))).all()
    return {r.entity_id: r for r in rows}


async def test_keyword_column_end_to_end(workspace, crawl, local_ai):
    ws, user = workspace
    ids = await seed_agencies(ws)
    col = await create_column(ws, name="Mentions Instagram", instruction="Whether their site mentions Instagram",
                              list_id=ids["list"], created_by=user)
    assert col.resolver_type == ResolverType.CACHED_WEBSITE and col.slug == "mentions_instagram"
    assert col.configuration["strategy"] == "keyword" and col.refresh_policy == {"refresh_days": 30}

    targets = await column_entity_ids(ws, col)
    assert set(targets) == {ids["lumiere"], ids["ruche"]}  # company-level: distinct companies of the list's people
    assert await estimate_coverage(ws, col, targets) == {"total": 2, "cached": 2, "done": 0}

    assert await enqueue_column(ws, col.id) == 2
    assert {c.status for c in (await _cells(col.id)).values()} == {CellStatus.queued}
    await _run()

    cells = await _cells(col.id)
    assert {c.status for c in cells.values()} == {CellStatus.success}
    lum = cells[ids["lumiere"]]
    assert lum.value_json is True and lum.display_value == "true" and lum.confidence == 1.0
    assert lum.source_url == "https://studiolumiere.fr/" and "Instagram" in lum.evidence
    assert lum.resolver == "keyword" and lum.source_id == "website" and lum.input_hash and lum.observed_at

    async with session_scope() as s:
        exposures = (await s.scalars(sa.select(LeadExposure).where(LeadExposure.workspace_id == ws))).all()
        events = (await s.scalars(sa.select(JobEvent).where(JobEvent.workspace_id == ws))).all()
        jobs = (await s.scalars(sa.select(Job).where(Job.type == "enrichment.batch"))).all()
    assert {(e.entity_type, e.entity_id, e.exposure_type) for e in exposures} == {
        (EntityType.company, ids["lumiere"], ExposureType.ENRICHED),
        (EntityType.company, ids["ruche"], ExposureType.ENRICHED),
    }
    updates = [e for e in events if e.type == "cell.updated"]
    assert updates and all(e.payload["column_id"] == str(col.id) for e in updates)
    final = [c for e in updates for c in e.payload["cells"] if c["status"] == "success"]
    assert {c["entity_id"] for c in final} == {str(ids["lumiere"]), str(ids["ruche"])}
    assert any(e.type == "column.progress" and e.payload["done"] == 2 for e in events)
    assert [j.status for j in jobs] == [JobStatus.completed]
    assert (await column_progress(ws, col.id))["done"] == 2


async def test_semantic_column_unchanged_inputs_make_zero_ai_calls(workspace, crawl, fake_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    col = await create_column(ws, name="Instagram management", instruction=IG_MGMT, list_id=ids["list"])
    assert col.configuration["strategy"] == "semantic_classifier"
    assert fake_ai.calls == []  # planning a semantic column with explicit wording needs no AI

    await enqueue_column(ws, col.id)
    await _run()
    assert len(fake_ai.calls) == 2
    cells = await _cells(col.id)
    assert cells[ids["ruche"]].value_json is True and cells[ids["ruche"]].status == CellStatus.success
    assert cells[ids["lumiere"]].value_json is False and cells[ids["lumiere"]].status == CellStatus.success
    assert cells[ids["ruche"]].model == "fake" and cells[ids["ruche"]].resolver == "ai_on_cached_content"
    before = {k: (v.status, v.value_json, v.input_hash) for k, v in cells.items()}

    # Missing-only enqueue: nothing to do.
    assert await enqueue_column(ws, col.id) == 0
    # Re-check everything without force: inputs unchanged → no recomputation, ZERO AI calls.
    assert await enqueue_column(ws, col.id, only_missing=False) == 2
    await _run()
    assert len(fake_ai.calls) == 2
    after = await _cells(col.id)
    assert {k: (v.status, v.value_json, v.input_hash) for k, v in after.items()} == before

    # A changed page re-runs only that company.
    async with session_scope() as s:
        page = await s.scalar(sa.select(WebsitePage).where(WebsitePage.url == "https://larushesociale.fr/services"))
        page.content_text += "\nNouveau : formations Instagram pour les équipes marketing."
        page.content_hash = content_hash(page.content_text)
    await enqueue_column(ws, col.id, only_missing=False)
    await _run()
    assert len(fake_ai.calls) == 3

    # force recomputes everything.
    assert await enqueue_column(ws, col.id, force=True) == 2
    await _run()
    assert len(fake_ai.calls) == 5


async def test_user_override_is_preserved(workspace, crawl, local_ai):
    ws, user = workspace
    ids = await seed_agencies(ws)
    col = await create_column(ws, name="Mentions Instagram", instruction="Whether their site mentions Instagram",
                              list_id=ids["list"])
    await enqueue_column(ws, col.id)
    await _run()
    cell = await set_user_value(ws, col.id, "company", ids["lumiere"], "no", user_id=user)
    assert cell.is_user_override and cell.value_json is False and cell.source_id == "user" and cell.confidence == 1.0

    assert await enqueue_column(ws, col.id, force=True) == 1  # the override is never queued
    await _run()
    cells = await _cells(col.id)
    assert cells[ids["lumiere"]].is_user_override and cells[ids["lumiere"]].value_json is False
    assert cells[ids["lumiere"]].source_id == "user"
    assert cells[ids["ruche"]].value_json is True

    cleared = await set_user_value(ws, col.id, "company", ids["lumiere"], None, user_id=user)
    assert not cleared.is_user_override and cleared.status == CellStatus.not_started


async def test_person_level_column_and_exposures(workspace, crawl, local_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    col = await create_column(ws, name="Job title", instruction=None, list_id=ids["list"])
    assert col.entity_type == EntityType.person
    targets = await column_entity_ids(ws, col)
    assert set(targets) == set(ids["people"].values())
    await enqueue_column(ws, col.id)
    await _run()
    cells = await _cells(col.id)
    assert cells[ids["people"]["claire"]].value_json == "Fondatrice & CEO"
    assert cells[ids["people"]["paul"]].status == CellStatus.unknown
    async with session_scope() as s:
        exposures = (await s.scalars(sa.select(LeadExposure).where(LeadExposure.workspace_id == ws))).all()
    person_rows = [e for e in exposures if e.entity_type == EntityType.person]
    company_rows = [e for e in exposures if e.entity_type == EntityType.company]
    assert {e.entity_id for e in person_rows} == set(ids["people"].values())
    assert all(e.company_id for e in person_rows)
    assert {e.entity_id for e in company_rows} == {ids["lumiere"], ids["ruche"]}


async def test_one_failing_entity_does_not_fail_the_batch(workspace, crawl, local_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    async with session_scope() as s:
        dead = Company(workspace_id=ws, name="Ghost Agency", normalized_name="ghost", domain="ghost.example",
                       normalized_domain="ghost.example", website_status=WebsiteStatus.unreachable)
        nosite = Company(workspace_id=ws, name="Offline Co", normalized_name="offline co")
        s.add_all([dead, nosite])
        await s.flush()
        for c in (dead, nosite):
            s.add(ListMembership(workspace_id=ws, list_id=ids["list"], company_id=c.id))
    col = await create_column(ws, name="Mentions Instagram", instruction="Whether their site mentions Instagram",
                              list_id=ids["list"])
    assert await enqueue_column(ws, col.id) == 4
    await _run()
    cells = await _cells(col.id)
    assert cells[dead.id].status == CellStatus.failed and cells[dead.id].error == "Website unreachable"
    assert cells[nosite.id].status == CellStatus.unknown and cells[nosite.id].error == "No website"
    assert cells[ids["ruche"]].status == CellStatus.success
    assert crawl["ensure_calls"] == [dead.id]  # crawl attempted only where a website exists and nothing is cached
    async with session_scope() as s:
        statuses = (await s.scalars(sa.select(Job.status).where(Job.type == "enrichment.batch"))).all()
    assert statuses == [JobStatus.completed]
    progress = await column_progress(ws, col.id)
    assert progress["failed"] == 1 and progress["unknown"] == 1 and progress["done"] == 4


async def test_stale_cells_and_refresh(workspace, crawl, local_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    col = await create_column(ws, name="Mentions Instagram", instruction="Whether their site mentions Instagram",
                              list_id=ids["list"])
    await enqueue_column(ws, col.id)
    await _run()
    old = datetime.now(UTC) - timedelta(days=40)
    async with session_scope() as s:
        await s.execute(sa.update(CustomFieldValue).where(CustomFieldValue.entity_id == ids["lumiere"])
                        .values(observed_at=old))
    assert await mark_stale_cells(ws) == 1
    assert (await _cells(col.id))[ids["lumiere"]].status == CellStatus.stale
    assert await enqueue_column(ws, col.id) == 1
    await _run()
    cell = (await _cells(col.id))[ids["lumiere"]]
    assert cell.status == CellStatus.success and cell.value_json is True
    assert cell.observed_at > old + timedelta(days=39)

    assert await refresh_column(ws, col.id) == 2
    await _run()
    assert {c.status for c in (await _cells(col.id)).values()} == {CellStatus.success}


async def test_update_definition_replans_and_marks_stale(workspace, crawl, local_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    col = await create_column(ws, name="Mention", instruction="Whether their site mentions Instagram",
                              list_id=ids["list"])
    await enqueue_column(ws, col.id)
    await _run()
    col = await update_column_definition(ws, col.id, instruction="Whether their site mentions TikTok",
                                         refresh_days=7)
    assert col.configuration["keywords"][0] == "TikTok" and col.refresh_policy["refresh_days"] == 7
    assert {c.status for c in (await _cells(col.id)).values()} == {CellStatus.stale}
    await enqueue_column(ws, col.id)
    await _run()
    cells = await _cells(col.id)
    assert cells[ids["lumiere"]].value_json is False and cells[ids["ruche"]].value_json is True


async def test_unique_slug_and_position(workspace, local_ai):
    ws, _ = workspace
    a = await create_column(ws, name="ManyChat", instruction=None)
    b = await create_column(ws, name="ManyChat", instruction=None)
    assert (a.slug, b.slug) == ("manychat", "manychat_2")
    assert b.position == a.position + 1


async def test_web_research_persisted_cached_and_source_rules(workspace, crawl):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    fake = FakeProvider()
    calls: list[str] = []
    job_url = "https://www.welcometothejungle.com/fr/companies/la-ruche-sociale/jobs/community-manager"

    def grounded(query: str, schema) -> GroundedResult:
        calls.append(query)
        if "La Ruche Sociale" in query:
            return GroundedResult(
                text="", value=ResearchAnswer(value="Community manager (CDI, Lyon)", confidence=0.8,
                                              evidence_quote="La Ruche Sociale recrute un community manager",
                                              source_url=job_url),
                sources=[GroundingSource(uri="https://vertexaisearch.example/redirect/1", title="welcometothejungle.com",
                                         domain="welcometothejungle.com")],
                search_queries=["La Ruche Sociale offre d'emploi"], supports=[],
                usage=AIUsage(model="fake-search", cost_usd=0.014, grounded_queries=1))
        return GroundedResult(text="", value=ResearchAnswer(value="Growth marketer", confidence=0.9), sources=[],
                              search_queries=["Studio Lumière jobs"], supports=[],
                              usage=AIUsage(model="fake-search", cost_usd=0.014, grounded_queries=1))

    fake.grounded_handler = grounded
    set_ai(fake)
    try:
        col = await create_column(ws, name="Latest job", instruction="Find their latest job posting", list_id=ids["list"])
        assert col.resolver_type == ResolverType.AI_WEB_RESEARCH
        await enqueue_column(ws, col.id)
        await _run()
        cells = await _cells(col.id)
        ruche, lum = cells[ids["ruche"]], cells[ids["lumiere"]]
        assert ruche.status == CellStatus.success and ruche.value_json == "Community manager (CDI, Lyon)"
        assert ruche.source_url == job_url and ruche.source_id == "gemini_search"
        assert lum.status == CellStatus.unknown and lum.confidence <= 0.4  # no grounding sources → never a value
        async with session_scope() as s:
            rows = (await s.scalars(sa.select(GroundedResearch).where(GroundedResearch.workspace_id == ws))).all()
        assert len(rows) == 2 and all(r.cache_key and r.query for r in rows)
        assert any(r.sources and r.search_queries == ["La Ruche Sociale offre d'emploi"] for r in rows)

        # Same research in another column → served from the 14-day cache, no new grounded search.
        twin = await create_column(ws, name="Latest job (copy)", instruction="Find their latest job posting",
                                   list_id=ids["list"])
        await enqueue_column(ws, twin.id)
        await _run()
        assert len(calls) == 2
        assert (await _cells(twin.id))[ids["ruche"]].value_json == "Community manager (CDI, Lyon)"
    finally:
        set_ai(None)


async def test_composite_email_of_role(workspace, crawl, local_ai):
    from scout.db.enums import EmailDiscoveryMethod, EmailStatus
    from scout.db.models import Email, Person

    ws, _ = workspace
    ids = await seed_agencies(ws)
    async with session_scope() as s:
        email = Email(workspace_id=ws, person_id=ids["people"]["claire"], company_id=ids["lumiere"],
                      address="claire@studiolumiere.fr", local_part="claire", domain="studiolumiere.fr",
                      discovery_method=EmailDiscoveryMethod.known_pattern, status=EmailStatus.SAFE,
                      overall_confidence=0.92, is_primary=True)
        s.add(email)
        await s.flush()
        await s.execute(sa.update(Person).where(Person.id == ids["people"]["claire"])
                        .values(primary_email_id=email.id))
    col = await create_column(ws, name="CEO email", instruction="Find the CEO's email", list_id=ids["list"])
    assert col.configuration["strategy"] == "composite"
    await enqueue_column(ws, col.id)
    await _run()
    cells = await _cells(col.id)
    lum, ruche = cells[ids["lumiere"]], cells[ids["ruche"]]
    assert lum.status == CellStatus.success and lum.value_json == "claire@studiolumiere.fr"
    assert "SAFE" in lum.evidence and "Claire Martin" in lum.evidence and lum.confidence == 0.92
    assert ruche.status == CellStatus.unknown and ruche.error == "Missing dependency: email for Thomas Bernard"


async def test_resolve_plan_preview(workspace, crawl, local_ai):
    from scout.enrich.engine import resolve_plan
    from scout.enrich.planner import plan_column

    ws, _ = workspace
    ids = await seed_agencies(ws)
    async with session_scope() as s:
        company = await s.get(Company, ids["ruche"])
    res = await resolve_plan(ws, await plan_column("Booking", "Find their booking link"), company=company)
    assert res.status == CellStatus.success and res.value.startswith("https://calendly.com/")
    async with session_scope() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(CustomFieldValue)) == 0
