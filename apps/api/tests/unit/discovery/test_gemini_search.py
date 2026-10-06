"""Grounded-search adapter with the FakeProvider: only companies with a real company domain survive."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scout.ai.factory import FakeProvider, LocalProvider, set_ai
from scout.ai.provider import AIUsage, GroundedResult, GroundingSource
from scout.discovery.base import DiscoveryQuery
from scout.discovery.gemini_search import GeminiSearchSource, GroundedCompanies

from .conftest import defn


@pytest.fixture
def fake() -> Iterator[FakeProvider]:
    provider = FakeProvider()
    set_ai(provider)
    yield provider
    set_ai(None)


def grounded(_query: str, schema: type | None) -> GroundedResult:
    assert schema is GroundedCompanies
    value = GroundedCompanies.model_validate({"companies": [
        {"name": "Acme Analytics", "website": "https://www.acme-analytics.io/pricing", "city": "Austin",
         "evidence": "Acme Analytics, an Austin SaaS startup"},
        {"name": "No Site Inc", "website": None, "city": "Austin"},
        {"name": "Social Only", "website": "https://www.linkedin.com/company/social-only"},
        {"name": "Directory Listing", "website": "https://clutch.co/profile/foo"},
        {"name": "Acme Analytics (dup)", "website": "https://acme-analytics.io"},
        {"name": "Beta Ops", "website": "betaops.com"},
    ]})
    return GroundedResult(
        text="{}",
        value=value,
        sources=[GroundingSource(uri="https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                                 title="acme-analytics.io", domain="acme-analytics.io")],
        search_queries=["saas startups austin"],
        supports=[],
        usage=AIUsage(model="fake", grounded_queries=1),
    )


def query() -> DiscoveryQuery:
    return DiscoveryQuery(key="gs:x", params={"question": "Which SaaS companies in Austin?", "subject": "s", "area": "a"})


async def test_drops_companies_without_real_domains(fake: FakeProvider) -> None:
    fake.grounded_handler = grounded
    page = await GeminiSearchSource().discover(query(), None)
    assert [c.domain for c in page.candidates] == ["acme-analytics.io", "betaops.com"]
    acme = page.candidates[0]
    assert acme.source == "gemini_search" and acme.website == "https://acme-analytics.io/"
    assert acme.location == {"city": "Austin"}
    assert acme.raw_data["search_queries"] == ["saas startups austin"]
    assert acme.raw_data["grounding_sources"][0]["domain"] == "acme-analytics.io"
    assert acme.raw_data["evidence"].startswith("Acme Analytics")
    assert acme.source_url and "grounding-api-redirect" in acme.source_url
    assert page.next_cursor is None  # one page per question


async def test_budget_guard_skips_the_call(fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(q: str, schema: type | None) -> GroundedResult:
        calls.append(q)
        return grounded(q, schema)

    async def deny() -> bool:
        return False

    fake.grounded_handler = handler
    monkeypatch.setattr("scout.discovery.gemini_search.allow_expensive", deny)
    page = await GeminiSearchSource().discover(query(), None)
    assert page.candidates == [] and page.next_cursor is None and calls == []


async def test_empty_value_yields_nothing(fake: FakeProvider) -> None:
    fake.grounded_handler = lambda q, s: GroundedResult(text="sorry", value=None, sources=[], search_queries=[],
                                                        supports=[], usage=AIUsage(model="fake"))
    page = await GeminiSearchSource().discover(query(), None)
    assert page.candidates == []


def test_configured_only_with_available_ai(fake: FakeProvider) -> None:
    src = GeminiSearchSource()
    assert src.is_configured()
    set_ai(LocalProvider())
    assert not src.is_configured()


def test_plan_segments_questions(fake: FakeProvider) -> None:
    plan = GeminiSearchSource().plan(defn(industries=["SaaS startups"], countries=["US"], employee_range=(2, 30)))
    questions = [q.params["question"] for q in plan]
    assert len(plan) == 4
    assert questions[0] == ("Which saas companies in New York, United States with 2 to 30 employees are there? "
                            "List their names and official websites.")
    assert questions[-1].startswith("Which saas companies in United States with")
    lyon = GeminiSearchSource().plan(defn(industries=["agences marketing"], cities=["Lyon"]))
    assert [q.params["area"] for q in lyon] == ["Lyon, France"]
    assert "marketing agencies" in lyon[0].params["question"]
