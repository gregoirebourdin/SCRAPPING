"""Website conditions: deterministic + semantic evaluation with the (company, condition, content) cache."""

from __future__ import annotations

import sys
import types

import pytest
import sqlalchemy as sa

from scout.ai.factory import FakeProvider, LocalProvider, set_ai
from scout.db.engine import session_scope
from scout.db.models import Company, Technology, WebsiteConditionResult, WebsitePage
from scout.enrich.conditions import condition_hash, evaluate_condition, evaluate_conditions
from scout.schemas.campaign import KeywordCondition, RegexCondition, SemanticCondition, TechnologyCondition
from tests.unit.enrich.helpers import load_fixture, seed_agencies, sentence_containing

pytestmark = pytest.mark.integration

IG = SemanticCondition(concept="offers Instagram management as a client service", keywords=["Instagram"])


async def _company_and_pages(company_id):
    async with session_scope() as s:
        company = await s.get(Company, company_id)
        pages = (await s.scalars(sa.select(WebsitePage).where(WebsitePage.company_id == company_id))).all()
    return company, list(pages)


@pytest.fixture
def fake_ai():
    fake = FakeProvider()
    quote, url = sentence_containing(load_fixture("agency_instagram"), "Nous gérons vos comptes Instagram")
    fake.on("SemanticVerdict", lambda prompt: {
        "verdict": "true", "confidence": 0.9, "evidence_quote": quote, "source_url": url})
    set_ai(fake)
    yield fake
    set_ai(None)


async def test_semantic_condition_is_cached_by_content_hash(workspace, fake_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    company, pages = await _company_and_pages(ids["ruche"])

    first = await evaluate_condition(ws, company, IG, pages)
    assert first.passed is True and not first.cached and first.resolver == "ai_on_cached_content"
    assert "Instagram" in first.evidence and first.source_url == "https://larushesociale.fr/services"
    assert len(fake_ai.calls) == 1

    again = await evaluate_condition(ws, company, IG, pages)
    assert again.cached and again.passed is True
    assert len(fake_ai.calls) == 1  # unchanged pages ⇒ no recomputation, no AI call

    relabeled = IG.model_copy(update={"label": "Instagram service", "required": False})
    assert condition_hash(relabeled) == condition_hash(IG)

    pages[1].content_hash = "changed"
    changed = await evaluate_condition(ws, company, IG, pages)
    assert not changed.cached and len(fake_ai.calls) == 2
    async with session_scope() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(WebsiteConditionResult)) == 2


async def test_threshold_maps_to_unknown(workspace, fake_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    company, pages = await _company_and_pages(ids["ruche"])
    strict = SemanticCondition(concept="offers Instagram management", keywords=["Instagram"], min_confidence=0.95)
    res = await evaluate_condition(ws, company, strict, pages)
    assert res.passed is None and res.confidence == 0.9


async def test_keyword_regex_and_absence(workspace):
    set_ai(LocalProvider())
    try:
        ws, _ = workspace
        ids = await seed_agencies(ws)
        company, pages = await _company_and_pages(ids["lumiere"])
        kw = await evaluate_condition(ws, company, KeywordCondition(terms=["référencement", "SEO"]), pages)
        assert kw.passed is True and kw.confidence == 1.0 and kw.resolver == "keyword"
        assert (await evaluate_condition(ws, company, KeywordCondition(terms=["référencement"]), pages)).passed
        both = await evaluate_condition(ws, company, KeywordCondition(type="keyword_all", terms=["SEO", "TikTok"]), pages)
        assert both.passed is False and "TikTok" in both.evidence
        rx = await evaluate_condition(ws, company, RegexCondition(pattern=r"RCS\s+\w+\s+\d{3}"), pages)
        assert rx.passed is True and rx.source_url.endswith("/mentions-legales")
        cached = await evaluate_condition(ws, company, KeywordCondition(terms=["référencement", "SEO"]), pages)
        assert cached.cached
        none = await evaluate_condition(ws, company, KeywordCondition(terms=["SEO"]), [])
        assert none.passed is None
    finally:
        set_ai(None)


async def test_required_failure_stops_before_ai(workspace, fake_ai):
    ws, _ = workspace
    ids = await seed_agencies(ws)
    company, pages = await _company_and_pages(ids["lumiere"])
    results = await evaluate_conditions(ws, company, [IG, KeywordCondition(terms=["ManyChat"])], pages)
    assert [c.type for c, _ in results] == ["keyword_any"]
    assert results[0][1].passed is False
    assert fake_ai.calls == []


async def test_offline_heuristic_condition(workspace):
    set_ai(LocalProvider())
    try:
        ws, _ = workspace
        ids = await seed_agencies(ws)
        ruche, ruche_pages = await _company_and_pages(ids["ruche"])
        lum, lum_pages = await _company_and_pages(ids["lumiere"])
        cond = SemanticCondition(concept="offers Instagram management", keywords=["Instagram"], min_confidence=0.6)
        assert (await evaluate_condition(ws, ruche, cond, ruche_pages)).passed is True
        lum_res = await evaluate_condition(ws, lum, cond, lum_pages)
        assert lum_res.passed is False and lum_res.resolver == "heuristic_semantic"
    finally:
        set_ai(None)


async def test_technology_condition(workspace, monkeypatch):
    ws, _ = workspace
    ids = await seed_agencies(ws)

    async def detect_technologies(workspace_id, company_id, *, force=False, max_age_days=30):
        return [Technology(workspace_id=workspace_id, company_id=company_id, name="Shopify", category="Ecommerce",
                           confidence=1.0, detector="scripts", source_url="https://larushesociale.fr/")]

    mod = types.ModuleType("scout.tech.detector")
    mod.detect_technologies = detect_technologies  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scout.tech.detector", mod)
    set_ai(LocalProvider())
    try:
        company, pages = await _company_and_pages(ids["ruche"])
        assert (await evaluate_condition(ws, company, TechnologyCondition(technologies=["shopify"]), pages)).passed
        both = TechnologyCondition(technologies=["Shopify", "WordPress"], match="all")
        assert (await evaluate_condition(ws, company, both, pages)).passed is False
    finally:
        set_ai(None)
