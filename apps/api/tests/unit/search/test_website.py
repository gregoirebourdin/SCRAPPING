"""Website candidates from free search (identity is still proven by scout.crawl.resolve)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from scout.search.website import search_domain_candidates

from .conftest import COMPANY_RESULTS, SEARXNG_SEARCH, sx_payload, sx_result


@respx.mock
async def test_candidates_name_the_company_and_skip_directories() -> None:
    results = [
        sx_result("https://www.pagesjaunes.fr/pros/123", "Pixel Studio - Lyon - PagesJaunes"),
        sx_result("https://fr.linkedin.com/company/pixel-studio", "Pixel Studio | LinkedIn"),
        sx_result("https://www.pixel-studio-lyon.fr/contact", "Contact - Pixel Studio"),
        sx_result("https://pixelstudio.com/", "Pixel Studio — design agency"),
        sx_result("https://www.other-agency.fr/", "Other agency", "web agency in Lyon"),
        *COMPANY_RESULTS[5:6],  # listicle
    ]
    route = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(results)))
    out = await search_domain_candidates("Pixel Studio", city="Lyon", country="fr")
    assert out == ["pixel-studio-lyon.fr", "pixelstudio.com"]
    params = route.calls.last.request.url.params
    assert params["q"] == '"Pixel Studio" Lyon' and params["language"] == "fr-FR"


@respx.mock
@pytest.mark.parametrize("env", [{"SEARXNG_URL": None}])
async def test_noop_without_lookup_provider(settings_env: Any, env: dict[str, Any]) -> None:
    settings_env(**env)
    route = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload([])))
    assert await search_domain_candidates("Pixel Studio") == [] and not route.called
