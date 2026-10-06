"""Decision makers from free search results (profile titles, press snippets) before any Gemini grounding."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from scout.search.people import people_from_results, serp_people
from scout.search.types import SearchResult

from .conftest import SEARXNG_SEARCH, sx_payload, sx_result


def r(url: str, title: str, snippet: str = "") -> SearchResult:
    return SearchResult(url=url, title=title, snippet=snippet, position=1)


def names(cands: list[Any]) -> list[tuple[str, str | None]]:
    return [(c.full_name, c.title) for c in cands]


def test_profile_titles_and_press_snippets() -> None:
    results = [
        r(
            "https://fr.linkedin.com/in/jean-dupont-12",
            "Jean Dupont - Fondateur - Pixel Studio | LinkedIn",
            "Lyon · Fondateur chez Pixel Studio",
        ),
        r(
            "https://fr.linkedin.com/in/marie-l",
            "Marie Laurent – Pixel Studio | LinkedIn",
            "CEO chez Pixel Studio. Lyon.",
        ),
        r(
            "https://www.leprogres.fr/economie/pixel",
            "Pixel Studio lève 1 M€",
            "Selon Paul Martin, cofondateur de Pixel Studio, la croissance continue.",
        ),
        r(
            "https://www.lyon-entreprises.com/x",
            "Pixel Studio",
            "Pixel Studio, agence fondée par Claire Bernard en 2015.",
        ),
    ]
    found = people_from_results(results, company_name="Pixel Studio", domain="pixel-studio-lyon.fr")
    assert names(found) == [
        ("Jean Dupont", "Fondateur"),
        ("Marie Laurent", "CEO"),
        ("Paul Martin", "cofondateur"),
        ("Claire Bernard", "Fondateur"),
    ]
    jean = found[0]
    assert jean.source_type == "search_snippet" and jean.method == "search_result"
    assert jean.profile_url == "https://fr.linkedin.com/in/jean-dupont-12" and jean.confidence == 0.65
    assert "Fondateur chez Pixel Studio" in jean.evidence
    assert found[2].profile_url is None and found[2].source_url == "https://www.leprogres.fr/economie/pixel"


@pytest.mark.parametrize(
    "result",
    [
        r(
            "https://fr.linkedin.com/in/bob", "Bob Martin - Ancien CEO - Pixel Studio | LinkedIn"
        ),  # former role
        r(
            "https://fr.linkedin.com/in/al",
            "Alice Martin - Head of Sales - Other Corp | LinkedIn",
            "Previously at Pixel Studio",
        ),
        r("https://fr.linkedin.com/in/x", "Jean Dupont - CEO - Pixel Factory | LinkedIn"),  # homonym company
        r("https://fr.linkedin.com/company/pixel", "Pixel Studio | LinkedIn", "Agence"),  # company page
        r(
            "https://www.pixel.fr/", "Agence Pixel Studio - Lyon", "Pixel Studio, agence web à Lyon."
        ),  # no person
    ],
)
def test_rejections(result: SearchResult) -> None:
    assert people_from_results([result], company_name="Pixel Studio", domain="pixel-studio-lyon.fr") == []


@respx.mock
async def test_serp_people_profile_query_first(stats: list[dict[str, Any]]) -> None:
    route = respx.get(SEARXNG_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json=sx_payload(
                [
                    sx_result(
                        "https://fr.linkedin.com/in/jd", "Jean Dupont - Gérant - Pixel Studio | LinkedIn", ""
                    )
                ]
            ),
        )
    )
    found = await serp_people(
        "Pixel Studio", domain="pixel-studio-lyon.fr", country="FR", titles=["Gérant/CEO"]
    )
    assert names(found) == [("Jean Dupont", "Gérant")]
    params = route.calls.last.request.url.params
    assert params["q"] == '"Pixel Studio" (Gérant OR CEO) site:linkedin.com' and params["language"] == "fr-FR"
    assert route.call_count == 1
    assert [(e["dimension"], e["key"], e["produced"]) for e in stats] == [
        ("search.engine", "searxng", True),
        ("people.source", "linkedin_snippet", True),
    ]


@respx.mock
async def test_serp_people_open_web_second_then_unresolved(stats: list[dict[str, Any]]) -> None:
    route = respx.get(SEARXNG_SEARCH).mock(
        return_value=httpx.Response(
            200, json=sx_payload([sx_result("https://other.fr/", "Unrelated", "nothing")])
        )
    )
    found = await serp_people("Pixel Studio", domain="pixel-studio-lyon.fr", country="FR")
    assert found == [] and route.call_count == 2
    assert "site:linkedin.com" not in route.calls.last.request.url.params["q"]
    assert "fondateur" in route.calls.last.request.url.params["q"]  # default French roles
    people_events = [(e["key"], e["produced"]) for e in stats if e["dimension"] == "people.source"]
    assert people_events == [("linkedin_snippet", False), ("searxng_result", False)]


@respx.mock
@pytest.mark.parametrize("env", [{"SEARXNG_URL": None}, {"GEMINI_SEARCH_FALLBACK_ONLY": "false"}])
async def test_serp_people_noop_without_provider_or_in_legacy_mode(
    settings_env: Any, env: dict[str, Any]
) -> None:
    settings_env(**env)
    route = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload([])))
    assert await serp_people("Pixel Studio", domain="pixel-studio-lyon.fr", country="FR") == []
    assert not route.called


@respx.mock
async def test_serp_people_searxng_down_is_unresolved() -> None:
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(502))
    assert await serp_people("Pixel Studio", domain="pixel-studio-lyon.fr") == []
