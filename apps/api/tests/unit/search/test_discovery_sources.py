"""Discovery sources on the search chain: web_search uses SearXNG first (DuckDuckGo fallback); Gemini grounded
discovery runs only when free search is insufficient."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from scout.ai.factory import FakeProvider
from scout.ai.provider import AIUsage, GroundedResult, GroundingSource
from scout.discovery.base import DiscoveryQuery
from scout.discovery.common import Throttle
from scout.discovery.gemini_search import GeminiSearchSource, GroundedCompanies
from scout.discovery.web_search import WebSearchSource
from scout.errors import BlockedError

from ..discovery.conftest import defn, fixture_text
from .conftest import COMPANY_RESULTS, DDG, SEARXNG_SEARCH, sx_payload, sx_result


def ws_query(**extra: Any) -> DiscoveryQuery:
    return DiscoveryQuery(
        key="ddg:fr-fr:agence marketing lyon",
        params={
            "q": "agence marketing Lyon",
            "kl": "fr-fr",
            "max_pages": 2,
            "expand_listicles": False,
            **extra,
        },
    )


@pytest.fixture
def src() -> WebSearchSource:
    return WebSearchSource(throttle=Throttle(0))


# ---------------------------------------------------------------------------------------- web_search


@respx.mock
async def test_web_search_uses_searxng_first(src: WebSearchSource) -> None:
    sx = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    ddg = respx.post(DDG).mock(return_value=httpx.Response(200, text=fixture_text("ddg_results.html")))
    page = await src.discover(ws_query(), None)
    assert not ddg.called and sx.calls.last.request.url.params["language"] == "fr-FR"
    assert [c.domain for c in page.candidates] == [
        "pixel-studio-lyon.fr",
        "lumiere-digitale.fr",
        "kreacom.fr",
        "atelier-nord.studio",
    ]
    first = page.candidates[0]
    assert first.source == "web_search" and first.name == "Pixel Studio"
    assert first.raw_data["engine"] == "searxng" and first.raw_data["engines"] == ["bing"]
    assert page.next_cursor is not None and page.next_cursor["engine"] == "searxng"
    assert page.next_cursor["form"] == {"pageno": 2} and page.next_cursor["page"] == 2

    sx.mock(
        return_value=httpx.Response(200, json=sx_payload([sx_result("https://agence-boreal.fr/", "Boréal")]))
    )
    page2 = await src.discover(ws_query(), page.next_cursor)
    assert sx.calls.last.request.url.params["pageno"] == "2"
    assert [c.domain for c in page2.candidates] == ["agence-boreal.fr"] and page2.next_cursor is None


@respx.mock
async def test_web_search_falls_back_to_duckduckgo_when_searxng_fails(src: WebSearchSource) -> None:
    respx.get(SEARXNG_SEARCH).mock(side_effect=httpx.ConnectError("down"))
    ddg = respx.post(DDG).mock(return_value=httpx.Response(200, text=fixture_text("ddg_results.html")))
    page = await src.discover(ws_query(), None)
    assert ddg.called and page.candidates[0].raw_data["engine"] == "duckduckgo"
    assert page.next_cursor is not None and page.next_cursor["engine"] == "duckduckgo"
    assert page.next_cursor["form"]["s"] == "10"  # DDG next-page form carried as provider state


@respx.mock
async def test_web_search_all_blocked_raises_blocked(src: WebSearchSource) -> None:
    respx.get(SEARXNG_SEARCH).mock(
        return_value=httpx.Response(200, json=sx_payload([], unresponsive=[["bing", "CAPTCHA"]]))
    )
    respx.post(DDG).mock(return_value=httpx.Response(200, text=fixture_text("ddg_blocked.html")))
    with pytest.raises(BlockedError):
        await src.discover(ws_query(), None)


def test_web_search_plan_carries_locale(src: WebSearchSource) -> None:
    plan = src.plan(defn(industries=["agence marketing"], countries=["FR"], cities=["Lyon"]))
    assert plan[0].params["region"] == "FR" and plan[0].params["lang"] == "fr"
    assert plan[0].key == "ddg:fr-fr:agence marketing lyon"  # stable keys


# ---------------------------------------------------------------------------------------- gemini_search


def _grounded(calls: list[str]) -> Any:
    def handler(query: str, schema: type | None) -> GroundedResult:
        calls.append(query)
        assert schema is GroundedCompanies
        return GroundedResult(
            text="{}",
            value=GroundedCompanies.model_validate(
                {"companies": [{"name": "Acme Analytics", "website": "https://acme-analytics.io"}]}
            ),
            sources=[
                GroundingSource(uri="https://acme-analytics.io/", title="Acme", domain="acme-analytics.io")
            ],
            search_queries=["saas austin"],
            supports=[],
            usage=AIUsage(model="fake", cost_usd=0.02, grounded_queries=1),
        )

    return handler


def gs_query() -> DiscoveryQuery:
    plan = GeminiSearchSource().plan(defn(industries=["agences marketing"], cities=["Lyon"]))
    assert plan[0].params["serp_q"] == "agence marketing Lyon" and plan[0].params["serp_region"] == "FR"
    return plan[0]


@respx.mock
async def test_gemini_not_invoked_when_free_search_is_sufficient(
    fake_ai: FakeProvider, stats: list[dict[str, Any]]
) -> None:
    calls: list[str] = []
    fake_ai.grounded_handler = _grounded(calls)
    sx = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    page = await GeminiSearchSource().discover(gs_query(), None)
    assert calls == [] and page.candidates == [] and page.next_cursor is None
    assert sx.calls.last.request.url.params["q"] == "agence marketing Lyon"
    assert [(e["key"], e["produced"]) for e in stats] == [("searxng", True)]


@respx.mock
async def test_gemini_invoked_when_free_search_is_insufficient(
    fake_ai: FakeProvider, stats: list[dict[str, Any]]
) -> None:
    calls: list[str] = []
    fake_ai.grounded_handler = _grounded(calls)
    junk = [sx_result("https://www.pagesjaunes.fr/x", "Annuaire"), sx_result("https://a-agency.fr/", "A")]
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(junk)))
    page = await GeminiSearchSource().discover(gs_query(), None)
    assert len(calls) == 1 and [c.domain for c in page.candidates] == ["acme-analytics.io"]
    assert [(e["key"], e["produced"]) for e in stats] == [("searxng", False), ("gemini_grounded", True)]
    assert stats[-1]["cost_usd"] == 0.02


@respx.mock
async def test_gemini_invoked_when_searxng_is_down(fake_ai: FakeProvider) -> None:
    calls: list[str] = []
    fake_ai.grounded_handler = _grounded(calls)
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(503))
    page = await GeminiSearchSource().discover(gs_query(), None)
    assert len(calls) == 1 and page.candidates


@respx.mock
@pytest.mark.parametrize(
    "env",
    [
        {"GEMINI_SEARCH_FALLBACK_ONLY": "false"},  # legacy behaviour selected explicitly
        {"SEARXNG_URL": None},  # no lookup provider configured
    ],
)
async def test_gemini_legacy_paths_skip_the_free_pass(
    fake_ai: FakeProvider, settings_env: Any, env: dict[str, str | None]
) -> None:
    settings_env(**env)
    calls: list[str] = []
    fake_ai.grounded_handler = _grounded(calls)
    sx = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    page = await GeminiSearchSource().discover(gs_query(), None)
    assert len(calls) == 1 and page.candidates and not sx.called


@respx.mock
async def test_gemini_runs_when_web_search_is_excluded(fake_ai: FakeProvider) -> None:
    calls: list[str] = []
    fake_ai.grounded_handler = _grounded(calls)
    sx = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    d = defn(industries=["agences marketing"], cities=["Lyon"], _extra={"sources": {"excluded": ["ddg"]}})
    q = GeminiSearchSource().plan(d)[0]
    assert q.params["serp_first"] is False
    await GeminiSearchSource().discover(q, None)
    assert len(calls) == 1 and not sx.called
