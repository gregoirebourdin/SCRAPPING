"""Deterministic resolvers on cached pages (no AI) + the offline semantic heuristic."""

from __future__ import annotations

import sys
import types
import uuid

import pytest

from scout.db.enums import CellStatus, ColumnDataType, PageType
from scout.db.models import Technology
from scout.enrich.planner import plan_column
from scout.enrich.resolvers import ResolveContext, resolve
from scout.enrich.resolvers.base import NOT_CRAWLED
from scout.enrich.types import EnrichmentPlan
from tests.unit.enrich.helpers import load_fixture, make_company, make_pages


def _ctx(plan: EnrichmentPlan, fixture: str | None, *, with_pages: bool = True) -> ResolveContext:
    data = load_fixture(fixture or "agency_social_follow")
    company = make_company(data)
    pages = make_pages(data, company) if with_pages else []
    return ResolveContext(plan=plan, workspace_id=company.workspace_id, company=company, pages=pages)


async def _run(name: str, instruction: str | None, fixture: str = "agency_social_follow", **kw):
    plan = await plan_column(name, instruction, use_ai=False)
    return await resolve(_ctx(plan, fixture, **kw))


# ----------------------------------------------------------------------------- keyword
async def test_keyword_true_with_context_and_source(local_ai):
    res = await _run("Instagram mention", "Whether their site mentions Instagram")
    assert res.status == CellStatus.success and res.value is True and res.display_value == "true"
    assert res.confidence == 1.0
    assert "Instagram" in res.evidence and len(res.evidence) <= 200
    assert res.source_url == "https://studiolumiere.fr/"
    assert res.resolver == "keyword"


async def test_keyword_false_when_crawled_but_absent(local_ai):
    res = await _run("ManyChat", None)
    assert res.status == CellStatus.success and res.value is False and res.display_value == "false"
    assert res.confidence == 0.9
    assert "ManyChat" in res.evidence


async def test_keyword_unknown_without_pages(local_ai):
    res = await _run("ManyChat", None, with_pages=False)
    assert res.status == CellStatus.unknown and res.value is None
    assert res.error == NOT_CRAWLED and res.display_value == "unknown"


async def test_keyword_accent_insensitive_and_all_mode(local_ai):
    res = await _run("x", "Whether their site mentions referencement")
    assert res.value is True
    both = await _run("x", "Whether their site mentions SEO and TikTok")
    assert both.value is False and "TikTok" in both.evidence


# ----------------------------------------------------------------------------- semantic heuristic
async def test_follow_us_mention_is_keyword_true_but_not_instagram_management(local_ai):
    kw = await _run("IG", "Whether their site mentions Instagram")
    assert kw.value is True  # "Suivez-nous sur Instagram" is a mention…
    sem = await _run("IG mgmt", "Add whether the agency actually offers Instagram management")
    assert sem.resolver == "heuristic_semantic"
    assert sem.value is not True  # …but not evidence of an Instagram-management service
    assert sem.status == CellStatus.success and sem.value is False and sem.confidence == 0.6
    assert "social-follow" in sem.evidence


async def test_heuristic_detects_real_service(local_ai):
    res = await _run("IG mgmt", "Add whether the agency actually offers Instagram management", "agency_instagram")
    assert res.status == CellStatus.success and res.value is True
    assert res.resolver == "heuristic_semantic" and res.confidence <= 0.7
    assert res.source_url == "https://larushesociale.fr/services"
    assert "Instagram" in res.evidence


async def test_heuristic_unknown_with_too_few_pages(local_ai):
    plan = await plan_column("IG mgmt", "Add whether the agency actually offers TikTok ads", use_ai=False)
    ctx = _ctx(plan, "agency_social_follow")
    ctx.pages = ctx.pages[:1]
    res = await resolve(ctx)
    assert res.status == CellStatus.unknown


# ----------------------------------------------------------------------------- social
async def test_social_profile_found_and_absent(local_ai):
    ig = await _run("IG", "Find their Instagram account")
    assert ig.status == CellStatus.success and ig.value == "https://www.instagram.com/studiolumiere/"
    assert ig.confidence == 0.95 and ig.source_url == "https://studiolumiere.fr/"
    li = await _run("LI", "Find their LinkedIn page")
    assert li.value == "https://www.linkedin.com/company/studio-lumiere/"
    tt = await _run("TT", "Find their TikTok account")
    assert tt.status == CellStatus.unknown and tt.evidence == "No TikTok link found on crawled pages"
    none = await _run("IG", "Find their Instagram account", with_pages=False)
    assert none.status == CellStatus.unknown and none.error == NOT_CRAWLED


# ----------------------------------------------------------------------------- website fields
@pytest.mark.parametrize(
    ("instruction", "fixture", "expected"),
    [
        ("Add whether their homepage has testimonials", "agency_social_follow", False),
        ("Add whether their homepage has testimonials", "agency_instagram", True),
        ("Find their main CTA", "agency_social_follow", "Demander un devis"),
        ("Find their main CTA", "agency_instagram", "Réserver un appel"),
        ("Find their phone number", "agency_social_follow", "01 42 33 44 55"),
        ("Find their email address", "agency_social_follow", "contact@studiolumiere.fr"),
        ("Find their address", "agency_social_follow", "12 rue des Lilas, 75011 Paris, FR"),
        ("Add whether they have a pricing page", "agency_instagram", True),
        ("Add whether they have a pricing page", "agency_social_follow", False),
        ("Add whether they have a careers page", "agency_instagram", True),
        ("Find their booking link", "agency_instagram", "https://calendly.com/larushesociale/decouverte"),
        ("Add whether they have a chat widget", "agency_instagram", True),
        ("Add whether they have a chat widget", "agency_social_follow", False),
        ("Add whether they have a newsletter", "agency_social_follow", True),
        ("Add whether they have a blog", "agency_social_follow", True),
    ],
)
async def test_website_fields(instruction, fixture, expected, local_ai):
    res = await _run("field", instruction, fixture)
    assert res.status == CellStatus.success, (instruction, res)
    assert res.value == expected, (instruction, res)
    assert res.evidence


async def test_booking_link_absent_is_unknown(local_ai):
    res = await _run("Booking", "Find their booking link", "agency_social_follow")
    assert res.status == CellStatus.unknown and "No booking" in res.evidence


async def test_testimonials_scope_home_missing(local_ai):
    plan = await plan_column("T", "Add whether their homepage has testimonials", use_ai=False)
    ctx = _ctx(plan, "agency_instagram")
    ctx.pages = [p for p in ctx.pages if p.page_type != PageType.home]
    res = await resolve(ctx)
    assert res.status == CellStatus.unknown


# ----------------------------------------------------------------------------- regex & record
async def test_regex_siren_on_legal_page(local_ai):
    res = await _run("SIREN", "Find their SIREN number")
    assert res.status == CellStatus.success and res.value == "812345678"
    assert res.source_url == "https://studiolumiere.fr/mentions-legales"


async def test_regex_rejects_catastrophic_pattern(local_ai):
    plan = await plan_column("x", "regex: (a+)+$", use_ai=False)
    res = await resolve(_ctx(plan, None))
    assert res.status == CellStatus.failed and "nested" in res.error


async def test_deterministic_fields(local_ai):
    city = await _run("City", None)
    assert city.value == "Paris" and city.source_id == "company_record"
    size = await _run("Company size", None, "agency_instagram")
    assert size.value == "11–50"
    founded = await _run("Founded year", None)
    assert founded.status == CellStatus.unknown and "No founded year" in founded.evidence


# ----------------------------------------------------------------------------- technology
@pytest.fixture
def fake_detector(monkeypatch):
    calls: list[uuid.UUID] = []

    async def detect_technologies(workspace_id, company_id, *, force=False, max_age_days=30):
        calls.append(company_id)
        return [Technology(company_id=company_id, workspace_id=workspace_id, name="WordPress", category="CMS",
                           confidence=1.0, detector="html", source_url="https://studiolumiere.fr/"),
                Technology(company_id=company_id, workspace_id=workspace_id, name="Google Analytics",
                           category="Analytics", confidence=0.9, detector="scripts", source_url=None)]

    mod = types.ModuleType("scout.tech.detector")
    mod.detect_technologies = detect_technologies  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scout.tech.detector", mod)
    return calls


async def test_tech_detection_boolean_and_stack(fake_detector, local_ai):
    wp = await _run("WP", "Add whether they use WordPress")
    assert wp.status == CellStatus.success and wp.value is True and wp.source_id == "tech_scan"
    assert "detected by html" in wp.evidence
    shopify = await _run("Shopify", "Add whether they use Shopify")
    assert shopify.value is False and shopify.confidence == 0.85
    stack = await _run("Stack", "What is their tech stack")
    assert stack.value == "WordPress, Google Analytics"
    assert fake_detector  # detector was consulted


async def test_tech_detection_script_fallback(fake_detector, local_ai):
    res = await _run("Shopify", "Add whether they use Shopify", "agency_instagram")
    assert res.value is True and res.resolver == "page_scripts"


async def test_unsupported_field_fails_cleanly(local_ai):
    plan = EnrichmentPlan(name="x", data_type=ColumnDataType.text, resolver="CACHED_WEBSITE",
                          strategy="website_field", field="nope")
    res = await resolve(_ctx(plan, None))
    assert res.status == CellStatus.failed and "Unsupported" in res.error
