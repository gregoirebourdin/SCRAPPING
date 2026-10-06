"""Enrichment web research: free search (+ targeted crawl) with a cheap verified extraction first; Gemini
grounded search only when unresolved. Database access (cache / persistence) is stubbed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from scout.ai.factory import FakeProvider
from scout.ai.provider import AIUsage, GroundedResult, GroundingSource
from scout.db.enums import CellStatus
from scout.enrich.ai_schemas import ResearchAnswer
from scout.enrich.planner import plan_column
from scout.enrich.resolvers import ResolveContext, resolve
from scout.enrich.resolvers import web_research as wr
from tests.unit.enrich.helpers import load_fixture, make_company

from .conftest import SEARXNG_SEARCH, sx_payload, sx_result

JOB_URL = "https://www.welcometothejungle.com/fr/companies/la-ruche-sociale/jobs/community-manager"
JOB_SENTENCE = "La Ruche Sociale recrute un community manager en CDI à Lyon."
SERP = [
    sx_result(JOB_URL, "Community manager - La Ruche Sociale - CDI à Lyon", JOB_SENTENCE),
    sx_result("https://larushesociale.fr/", "La Ruche Sociale | Agence social media", "Agence lyonnaise."),
    sx_result("https://www.unrelated.fr/", "Une autre agence", "Rien à voir."),
]


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def no_cache(rc: Any, key: str) -> None:
        return None

    async def store(rc: Any, **values: Any) -> None:
        rows.append(values)

    monkeypatch.setattr(wr, "_cached", no_cache)
    monkeypatch.setattr(wr, "_store", store)
    return rows


@pytest.fixture
def grounded_calls(fake_ai: FakeProvider) -> list[str]:
    calls: list[str] = []

    def grounded(query: str, schema: type | None) -> GroundedResult:
        calls.append(query)
        return GroundedResult(
            text="",
            value=ResearchAnswer(
                value="Growth marketer (CDI)",
                confidence=0.85,
                evidence_quote="La Ruche Sociale recrute un growth marketer",
                source_url="https://jobs.example/ruche",
            ),
            sources=[GroundingSource(uri="https://jobs.example/ruche", title="jobs.example", domain="jobs.example")],
            search_queries=["La Ruche Sociale jobs"],
            supports=[],
            usage=AIUsage(model="fake-search", cost_usd=0.014, grounded_queries=1),
        )

    fake_ai.grounded_handler = grounded
    return calls


async def _ctx() -> ResolveContext:
    company = make_company(load_fixture("agency_instagram"))
    plan = await plan_column("Latest job", "Find their latest job posting", use_ai=False)
    assert plan.strategy == "web_research"
    return ResolveContext(plan=plan, workspace_id=company.workspace_id, company=company)


@respx.mock
async def test_resolved_by_free_search_gemini_not_invoked(
    fake_ai: FakeProvider, grounded_calls: list[str], stored: list[dict[str, Any]], stats: list[dict[str, Any]]
) -> None:
    sx = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(SERP)))
    fake_ai.on(
        "ResearchAnswer",
        lambda prompt: {
            "value": "Community manager (CDI, Lyon)",
            "confidence": 0.9,
            "evidence_quote": JOB_SENTENCE,
            "source_url": JOB_URL,
        },
    )
    res = await resolve(await _ctx())
    assert grounded_calls == []
    assert res.status == CellStatus.success and res.value == "Community manager (CDI, Lyon)"
    assert res.source_id == "web_search" and res.source_url == JOB_URL and res.resolver == "ai_web_research"
    assert res.confidence == 0.7  # snippet-only evidence is capped
    q = sx.calls.last.request.url.params["q"]
    assert q.startswith('"La Ruche Sociale"') and sx.calls.last.request.url.params["language"] == "fr-FR"
    schema, prompt = fake_ai.calls[-1]
    assert schema == "ResearchAnswer" and f'<untrusted_search_result source="{JOB_URL}">' in prompt
    assert "unrelated.fr" not in prompt  # results not about the company are not shown to the model
    assert len(stored) == 1 and stored[0]["result"]["via"] == "searxng" and stored[0]["search_queries"] == [q]
    assert [(e["key"], e["produced"]) for e in stats] == [("searxng", True)]


@respx.mock
async def test_unverified_quote_falls_back_to_gemini(
    fake_ai: FakeProvider, grounded_calls: list[str], stored: list[dict[str, Any]], stats: list[dict[str, Any]]
) -> None:
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(SERP)))
    fake_ai.on(
        "ResearchAnswer",
        lambda prompt: {
            "value": "Head of growth",
            "confidence": 0.95,
            "evidence_quote": "La Ruche Sociale recrute un head of growth à Paris.",  # not in any source
            "source_url": JOB_URL,
        },
    )
    res = await resolve(await _ctx())
    assert len(grounded_calls) == 1
    assert res.status == CellStatus.success and res.value == "Growth marketer (CDI)"
    assert res.source_id == "gemini_search"
    assert [r["model"] for r in stored] == ["fake-search"] and "via" not in stored[0]["result"]
    assert [(e["key"], e["produced"]) for e in stats] == [("searxng", True), ("gemini_grounded", True)]


@respx.mock
async def test_insufficient_results_fall_back_to_gemini(
    fake_ai: FakeProvider, grounded_calls: list[str], stored: list[dict[str, Any]]
) -> None:
    respx.get(SEARXNG_SEARCH).mock(
        return_value=httpx.Response(200, json=sx_payload([sx_result("https://www.unrelated.fr/", "Autre")]))
    )
    res = await resolve(await _ctx())
    assert len(grounded_calls) == 1 and res.source_id == "gemini_search"
    assert not any(schema == "ResearchAnswer" for schema, _ in fake_ai.calls)  # no extraction call


@respx.mock
@pytest.mark.parametrize("env", [{"SEARXNG_URL": None}, {"GEMINI_SEARCH_FALLBACK_ONLY": "false"}])
async def test_legacy_paths_go_straight_to_gemini(
    fake_ai: FakeProvider,
    grounded_calls: list[str],
    stored: list[dict[str, Any]],
    settings_env: Any,
    env: dict[str, str | None],
) -> None:
    settings_env(**env)
    sx = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(SERP)))
    res = await resolve(await _ctx())
    assert len(grounded_calls) == 1 and not sx.called and res.source_id == "gemini_search"


@respx.mock
async def test_targeted_crawl_supports_full_confidence(
    fake_ai: FakeProvider,
    grounded_calls: list[str],
    stored: list[dict[str, Any]],
    settings_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(WEB_RESEARCH_CRAWL_TOP="2")
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(SERP)))
    fetched: list[str] = []
    page_sentence = "Poste : Community manager confirmé, CDI basé à Lyon, démarrage en novembre."

    async def fake_fetch(url: str) -> Any:
        fetched.append(url)
        if url != JOB_URL:
            return None
        return SimpleNamespace(
            id=None,
            url=JOB_URL,
            title="Community manager",
            meta_description=None,
            content_text=f"La Ruche Sociale\n{page_sentence}\nPostuler",
            page_type="search_page",
        )

    monkeypatch.setattr(wr, "fetch_excerpt_page", fake_fetch)
    fake_ai.on(
        "ResearchAnswer",
        lambda prompt: {
            "value": "Community manager confirmé (CDI, Lyon)",
            "confidence": 0.9,
            "evidence_quote": page_sentence,
            "source_url": JOB_URL,
        },
    )
    res = await resolve(await _ctx())
    assert fetched == [JOB_URL, "https://larushesociale.fr/"]
    assert grounded_calls == [] and res.status == CellStatus.success and res.confidence == 0.9


async def test_cached_free_search_answer_keeps_its_provenance(
    fake_ai: FakeProvider, grounded_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = SimpleNamespace(
        result={
            "value": "Community manager",
            "confidence": 0.7,
            "evidence_quote": JOB_SENTENCE,
            "source_url": JOB_URL,
            "via": "searxng",
        },
        sources=[{"uri": JOB_URL, "title": "WTTJ", "domain": "welcometothejungle.com"}],
        model="fake",
    )

    async def cached(rc: Any, key: str) -> Any:
        return row

    monkeypatch.setattr(wr, "_cached", cached)
    res = await resolve(await _ctx())
    assert res.status == CellStatus.success and res.source_id == "web_search" and res.cost_usd == 0.0
    assert grounded_calls == []
