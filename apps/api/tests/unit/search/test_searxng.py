"""SearXNG adapter: request parameters, JSON parsing, typed errors (respx-mocked JSON API)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scout.errors import BlockedError, FetchError, RateLimitedError
from scout.search.searxng import SearXNGProvider, language_param, parse_payload
from scout.search.types import SearchBlockedError, SearchProviderError, SearchRateLimitedError

from .conftest import COMPANY_RESULTS, SEARXNG_SEARCH, sx_payload, sx_result


def test_language_param() -> None:
    assert language_param("fr", "fr") == "fr-FR"
    assert language_param(None, "DE") == "de-DE"
    assert language_param("en", None) == "en"
    assert language_param(None, None) is None


def test_parse_payload_keeps_order_engines_and_dates() -> None:
    payload = sx_payload(
        [
            sx_result("https://a.fr/", "A", "snippet a", engines=("bing", "brave")),
            sx_result("https://a.fr/", "A dup", "dup"),
            {"url": "ftp://x", "title": "not web"},
            {**sx_result("https://b.fr/news", "B"), "publishedDate": "2026-09-01T10:00:00"},
        ],
        unresponsive=[["google", "CAPTCHA"]],
    )
    results, unresponsive = parse_payload(payload, offset=10)
    assert [r.url for r in results] == ["https://a.fr/", "https://b.fr/news"]
    assert results[0].engines == ("bing", "brave") and results[0].engine == "bing"
    assert results[0].position == 11 and results[0].rank == 11 and results[0].snippet == "snippet a"
    assert results[1].published_at is not None and results[1].published_at.year == 2026
    assert unresponsive == ["google: CAPTCHA"]


@respx.mock
async def test_search_sends_json_query_and_parses() -> None:
    route = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    provider = SearXNGProvider()
    assert provider.is_configured()
    results = await provider.search(
        "agence marketing Lyon", num=3, lang="fr", region="FR", time_range="month"
    )
    assert len(results) == 3 and results[0].url == "https://www.pixel-studio-lyon.fr/"
    params = dict(route.calls.last.request.url.params)
    assert params == {
        "q": "agence marketing Lyon",
        "format": "json",
        "pageno": "1",
        "safesearch": "0",
        "categories": "general",
        "language": "fr-FR",
        "time_range": "month",
    }


@respx.mock
async def test_pagination_state_and_site_filter() -> None:
    route = respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    provider = SearXNGProvider()
    page = await provider.search_page("acme ceo", site="linkedin.com")
    assert page.next_state == {"pageno": 2} and page.provider == "searxng"
    assert route.calls.last.request.url.params["q"] == "acme ceo site:linkedin.com"
    page2 = await provider.search_page("acme ceo", state=page.next_state)
    assert route.calls.last.request.url.params["pageno"] == "2"
    assert page2.results[0].position == 11


def test_unconfigured() -> None:
    assert not SearXNGProvider(base_url="").is_configured()


@respx.mock
@pytest.mark.parametrize(
    ("response", "error", "base"),
    [
        (httpx.Response(403, text="Forbidden"), SearchProviderError, FetchError),
        (httpx.Response(429), SearchRateLimitedError, RateLimitedError),
        (httpx.Response(502), SearchProviderError, FetchError),
        (httpx.Response(200, text="<html>not json</html>"), SearchProviderError, FetchError),
        (
            httpx.Response(
                200, json=sx_payload([], unresponsive=[["bing", "CAPTCHA"], ["brave", "timeout"]])
            ),
            SearchBlockedError,
            BlockedError,
        ),
    ],
)
async def test_typed_errors(response: httpx.Response, error: type[Exception], base: type[Exception]) -> None:
    respx.get(SEARXNG_SEARCH).mock(return_value=response)
    with pytest.raises(error) as info:
        await SearXNGProvider().search("x")
    assert isinstance(info.value, base) and info.value.provider == "searxng"  # type: ignore[attr-defined]


@respx.mock
async def test_timeout_and_network_errors() -> None:
    respx.get(SEARXNG_SEARCH).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(SearchProviderError, match="timeout"):
        await SearXNGProvider().search("x")
    respx.get(SEARXNG_SEARCH).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(SearchProviderError, match="network error"):
        await SearXNGProvider().search("x")


@respx.mock
async def test_empty_without_engine_errors_is_a_valid_empty_page() -> None:
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload([])))
    page = await SearXNGProvider().search_page("very rare query")
    assert page.results == [] and page.next_state is None
