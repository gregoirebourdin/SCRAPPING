"""Web search discovery source (search chain; DuckDuckGo HTML here): parsing, non-company filtering, pagination,
blocking, listicle expansion. SearXNG-first behaviour is covered in tests/unit/search."""

from __future__ import annotations

import httpx
import pytest
import respx

from scout.discovery.base import DiscoveryQuery, RawCandidate
from scout.discovery.common import Throttle, candidate_domain, is_candidate_domain
from scout.discovery.web_search import (
    SearchResult,
    WebSearchSource,
    company_name_from_title,
    is_listicle,
    parse_results,
)
from scout.errors import BlockedError
from scout.search import reset_state

from .conftest import defn, fixture_text

DDG = "https://html.duckduckgo.com/html/"


@pytest.fixture(autouse=True)
def _fresh_search_state() -> None:
    """The search chain caches first pages and cools blocked providers down (process-wide)."""
    reset_state()


@pytest.fixture
def src() -> WebSearchSource:
    return WebSearchSource(throttle=Throttle(0))


def q(expand: bool = False, max_pages: int = 2) -> DiscoveryQuery:
    return DiscoveryQuery(
        key="ddg:fr-fr:agence marketing lyon",
        params={
            "q": "agence marketing Lyon",
            "kl": "fr-fr",
            "max_pages": max_pages,
            "expand_listicles": expand,
        },
    )


def test_parse_results_skips_ads_and_unwraps_redirects() -> None:
    results, next_form = parse_results(fixture_text("ddg_results.html"))
    assert len(results) == 11  # the ad is skipped
    assert results[0].url == "https://www.pixel-studio-lyon.fr/"
    assert results[0].title == "Agence Web Lyon - Pixel Studio | Création de sites"
    assert "agence marketing" in results[0].snippet
    assert next_form is not None and next_form["s"] == "10" and next_form["vqd"].startswith("4-")
    assert next_form["kl"] == "fr-fr" and next_form["q"] == "agence marketing Lyon"


def test_next_form_is_the_forward_one() -> None:
    _, form = parse_results(fixture_text("ddg_results_page2.html"))
    assert form is not None and form["s"] == "40"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://www.pixel-studio-lyon.fr/", True),
        ("https://fr.linkedin.com/company/x", False),
        ("https://www.pagesjaunes.fr/annuaire", False),
        ("https://www.lemonde.fr/economie/a.html", False),
        ("https://www.service-public.fr/x", False),
        ("https://www.impots.gouv.fr/", False),
        ("https://www.stanford.edu/", False),
        ("https://www.gov.uk/x", False),
        ("https://clutch.co/agencies", False),
        ("https://jobs.lever.co/acme", False),
        ("https://acme.io/", True),
    ],
)
def test_candidate_domain_filters(url: str, ok: bool) -> None:
    assert (candidate_domain(url) is not None) is ok
    assert is_candidate_domain(None) is False


def test_name_from_title() -> None:
    assert (
        company_name_from_title("Agence Web Lyon - Pixel Studio | Création de sites", "pixel-studio-lyon.fr")
        == "Pixel Studio"
    )
    assert (
        company_name_from_title("Lumière Digitale – Agence marketing digital à Lyon", "lumiere-digitale.fr")
        == "Lumière Digitale"
    )
    assert company_name_from_title("Accueil - Atelier Nord", "atelier-nord.studio") == "Atelier Nord"
    assert company_name_from_title("", "acme.fr") == "Acme"


def test_listicle_detection() -> None:
    assert is_listicle(
        SearchResult("https://x.fr/blog/a", "Top 10 des meilleures agences marketing à Lyon", "", 1)
    )
    assert is_listicle(SearchResult("https://x.com/best-agencies-boston", "Agencies in Boston", "", 1))
    assert not is_listicle(SearchResult("https://pixel.fr/", "Pixel Studio - agence web", "", 1))


@respx.mock
async def test_discover_one_candidate_per_company_domain(src: WebSearchSource) -> None:
    route = respx.post(DDG).mock(return_value=httpx.Response(200, text=fixture_text("ddg_results.html")))
    page = await src.discover(q(), None)
    assert [c.domain for c in page.candidates] == [
        "pixel-studio-lyon.fr",
        "lumiere-digitale.fr",
        "kreacom.fr",
        "atelier-nord.studio",
    ]
    names = {c.domain: c.name for c in page.candidates}
    assert names["pixel-studio-lyon.fr"] == "Pixel Studio" and names["atelier-nord.studio"] == "Atelier Nord"
    first = page.candidates[0]
    assert first.source == "web_search" and first.website == "https://pixel-studio-lyon.fr/"
    assert (
        first.raw_data["snippet"]
        and first.raw_data["rank"] == 1
        and first.raw_data["query"] == "agence marketing Lyon"
    )
    form = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert form == {"q": "agence marketing Lyon", "kl": "fr-fr"}
    assert page.next_cursor is not None and page.next_cursor["page"] == 2
    assert page.next_cursor["form"]["s"] == "10" and "kreacom.fr" in page.next_cursor["seen"]

    route.mock(return_value=httpx.Response(200, text=fixture_text("ddg_results_page2.html")))
    page2 = await src.discover(q(), page.next_cursor)
    sent = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert sent["s"] == "10" and sent["vqd"].startswith("4-")
    assert [c.domain for c in page2.candidates] == ["agence-boreal.fr"]  # lumiere-digitale.fr already seen
    assert page2.next_cursor is None  # max_pages reached


@respx.mock
@pytest.mark.parametrize(("status", "fixture"), [(200, "ddg_blocked.html"), (202, "ddg_results.html")])
async def test_anomaly_page_is_blocked(src: WebSearchSource, status: int, fixture: str) -> None:
    respx.post(DDG).mock(return_value=httpx.Response(status, text=fixture_text(fixture)))
    with pytest.raises(BlockedError):
        await src.discover(q(), None)


@respx.mock
async def test_listicles_expanded_at_expansion(src: WebSearchSource, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(DDG).mock(return_value=httpx.Response(200, text=fixture_text("ddg_results.html")))
    expanded: list[str] = []

    async def fake_expand(url: str, *, max_links: int = 60) -> list[RawCandidate]:
        expanded.append(url)
        return [
            RawCandidate(
                source="web_search",
                source_entity_id="okidoki-agence.fr",
                name="Okidoki",
                website="https://okidoki-agence.fr/",
                domain="okidoki-agence.fr",
                source_url=url,
            ),
            RawCandidate(
                source="web_search", source_entity_id="kreacom.fr", name="Kréa", domain="kreacom.fr"
            ),
        ]

    monkeypatch.setattr("scout.discovery.listicle.expand_listicle", fake_expand)
    plain = await src.discover(q(expand=False), None)
    assert expanded == [] and "okidoki-agence.fr" not in {c.domain for c in plain.candidates}
    page = await src.discover(q(expand=True), None)
    assert expanded == ["https://www.lafabrique-du-web.fr/blog/top-10-agences-marketing-lyon"]
    domains = [c.domain for c in page.candidates]
    assert domains.count("kreacom.fr") == 1 and domains[-1] == "okidoki-agence.fr"


def test_plan_and_suitability() -> None:
    d = defn(
        industries=["agence marketing"],
        countries=["FR"],
        cities=["Lyon"],
        _extra={"website_conditions": [{"type": "keyword_any", "terms": ["instagram", "manychat"]}]},
    )
    src = WebSearchSource()
    assert src.suitability(d) == 0.8
    plan = src.plan(d)
    qs = [x.params["q"] for x in plan]
    assert qs[:2] == ["agence marketing Lyon", "agence marketing Lyon instagram"]
    assert all(x.params["kl"] == "fr-fr" and x.params["expand_listicles"] is False for x in plan)
    assert src.plan(d, expansion=1)[0].params["expand_listicles"] is True
    us = src.plan(defn(industries=["SaaS"], countries=["US"]))
    assert us[0].params["kl"] == "us-en" and us[0].params["q"].endswith("New York")
    assert src.suitability(defn(industries=["quantum blorp"])) == 0
