"""SearchChain: provider order and fallback, explicit sufficiency, cooldown, cache, telemetry."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from scout.discovery.common import Throttle
from scout.errors import BlockedError
from scout.search import (
    ProviderHealth,
    SearchCache,
    SearchChain,
    assess_companies,
    assess_subject,
    build_providers,
    discovery_chain,
    lookup_chain,
)
from scout.search.types import SearchPage, SearchProviderError, SearchResult

from ..discovery.conftest import fixture_text
from .conftest import COMPANY_RESULTS, DDG, SEARXNG_SEARCH, sx_payload


class Scripted:
    """In-memory provider: returns pages or raises, counting calls."""

    def __init__(self, name: str, *outcomes: Any, configured: bool = True) -> None:
        self.name = name
        self.cost_usd = 0.0
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured

    async def search(self, query: str, **kw: Any) -> list[SearchResult]:
        return (await self.search_page(query, **kw)).results

    async def search_page(self, query: str, **kw: Any) -> SearchPage:
        self.calls.append({"query": query, **kw})
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, Exception):
            raise out
        return SearchPage(results=list(out), provider=self.name, next_state={"n": 2} if out else None)


def r(url: str, title: str = "", snippet: str = "") -> SearchResult:
    return SearchResult(url=url, title=title, snippet=snippet, position=1)


COMPANIES = [r(f"https://company{i}.fr/", f"Company {i}") for i in range(5)]


def chain(*providers: Any, **kw: Any) -> SearchChain:
    return SearchChain(providers, health=ProviderHealth(), cache=SearchCache(), cache_ttl_s=60, **kw)


async def test_first_provider_error_falls_back_to_next() -> None:
    a = Scripted("searxng", SearchProviderError("searxng: timeout", provider="searxng"))
    b = Scripted("duckduckgo", COMPANIES)
    res = await chain(a, b).search("agence lyon", assess=lambda rs: assess_companies(rs, min_companies=3))
    assert res.provider == "duckduckgo" and res.sufficient and len(res.results) == 5
    assert [(x.provider, x.ok) for x in res.attempts] == [("searxng", False), ("duckduckgo", True)]
    assert "timeout" in (res.attempts[0].error or "")


async def test_sufficient_first_answer_stops_the_chain() -> None:
    a, b = Scripted("searxng", COMPANIES), Scripted("duckduckgo", COMPANIES)
    res = await chain(a, b).search("q", assess=lambda rs: assess_companies(rs, min_companies=3))
    assert res.provider == "searxng" and res.sufficient and b.calls == []


async def test_insufficient_is_explicit_and_does_not_escalate_by_default() -> None:
    junk = [r("https://www.pagesjaunes.fr/x", "Annuaire"), r("https://fr.linkedin.com/company/a", "A | LinkedIn")]
    a, b = Scripted("searxng", junk), Scripted("duckduckgo", COMPANIES)
    res = await chain(a, b).search("q", assess=lambda rs: assess_companies(rs, min_companies=3))
    assert res.provider == "searxng" and not res.sufficient and b.calls == []
    assert res.assessment.reason.startswith("only 0 company domain(s) < 3")


async def test_escalate_on_insufficient_merges_results() -> None:
    a = Scripted("searxng", COMPANIES[:1])
    b = Scripted("duckduckgo", [COMPANIES[0], *COMPANIES[1:3]])
    res = await chain(a, b, escalate_on_insufficient=True).search(
        "q", assess=lambda rs: assess_companies(rs, min_companies=3)
    )
    assert res.sufficient and res.provider == "searxng" and len(res.results) == 3
    assert [x.sufficient for x in res.attempts] == [False, True]


async def test_nobody_answers_raise_if_failed_prefers_blocked() -> None:
    from scout.search.types import SearchBlockedError

    a = Scripted("searxng", SearchProviderError("down", provider="searxng"))
    b = Scripted("duckduckgo", SearchBlockedError("anomaly", provider="duckduckgo"))
    res = await chain(a, b).search("q")
    assert res.failed and not res.sufficient and res.results == []
    assert "no search provider answered" in res.assessment.reason
    with pytest.raises(BlockedError):
        res.raise_if_failed()


async def test_unconfigured_and_cooling_down_providers_are_skipped() -> None:
    health = ProviderHealth(failure_threshold=2, base_cooldown_s=60)
    a = Scripted("searxng", SearchProviderError("down", provider="searxng"))
    b = Scripted("duckduckgo", COMPANIES, configured=False)
    c = SearchChain([a, b], health=health, cache=SearchCache())
    for _ in range(2):
        res = await c.search("q")
        assert res.failed
    assert not health.available("searxng") and len(a.calls) == 2
    res = await c.search("q")
    assert [x.skipped for x in res.attempts] == ["cooling_down", "unconfigured"] and len(a.calls) == 2
    assert not c.available()
    with pytest.raises(BlockedError, match="cooling down"):
        res.raise_if_failed()
    health.success("searxng")
    assert c.available()


def test_blocked_cools_down_immediately_and_doubles() -> None:
    h = ProviderHealth(block_cooldown_s=100, max_cooldown_s=1000)
    h.failure("duckduckgo", "anomaly", blocked=True)
    first = h.cooldown_remaining("duckduckgo")
    h.failure("duckduckgo", "anomaly", blocked=True)
    assert 90 < first <= 100 and 190 < h.cooldown_remaining("duckduckgo") <= 200
    h.success("duckduckgo")
    assert h.available("duckduckgo")


async def test_first_pages_are_cached_per_normalized_query() -> None:
    a = Scripted("searxng", COMPANIES)
    c = chain(a)
    await c.search("Agence  Lyon", region="fr")
    hit = await c.search("agence lyon", region="FR", assess=lambda rs: assess_companies(rs, min_companies=9))
    assert len(a.calls) == 1 and hit.from_cache and hit.provider == "searxng" and not hit.sufficient
    await c.search("agence lyon", region="FR", time_range="month")
    await c.search_page("agence lyon", region="FR", page=2, state={"n": 2}, provider="searxng")
    assert len(a.calls) == 3  # other time range and later pages are not served from the cache


async def test_pagination_continues_on_the_same_provider() -> None:
    a, b = Scripted("searxng", COMPANIES), Scripted("duckduckgo", COMPANIES)
    res = await chain(a, b).search_page("q", page=2, state={"form": 1}, provider="duckduckgo")
    assert res.provider == "duckduckgo" and a.calls == [] and b.calls[0]["state"] == {"form": 1}


async def test_telemetry_events_per_attempt(stats: list[dict[str, Any]]) -> None:
    a = Scripted("searxng", SearchProviderError("down", provider="searxng"))
    b = Scripted("duckduckgo", COMPANIES)
    await chain(a, b).search("q", assess=lambda rs: assess_companies(rs, min_companies=3))
    assert [(e["dimension"], e["key"], e["produced"]) for e in stats] == [
        ("search.engine", "searxng", False),
        ("search.engine", "duckduckgo", True),
    ]
    stats.clear()
    await chain(Scripted("searxng", COMPANIES[:1])).search(
        "q", assess=lambda rs: assess_companies(rs, min_companies=3)
    )
    assert [(e["key"], e["produced"]) for e in stats] == [("searxng", False)]


def test_factories_follow_settings(settings_env: Any) -> None:
    assert discovery_chain().names == ["searxng", "duckduckgo"]
    assert lookup_chain().names == ["searxng"] and lookup_chain().available()
    assert build_providers(["ddg", "nope", "searx", "duckduckgo"])[0].name == "duckduckgo"
    settings_env(SEARXNG_URL=None, SEARCH_LOOKUP_PROVIDERS="searxng")
    assert not lookup_chain().configured and not lookup_chain().available()
    assert discovery_chain().configured  # DuckDuckGo HTML still configured
    settings_env(SEARCH_PROVIDERS="duckduckgo", SEARCH_LOOKUP_PROVIDERS="searxng,duckduckgo")
    assert discovery_chain().names == ["duckduckgo"] and lookup_chain().names == ["searxng", "duckduckgo"]


def test_assess_subject_by_name_or_domain() -> None:
    results = [
        r("https://larushesociale.fr/jobs", "Jobs", "Rejoignez-nous"),
        r("https://www.welcometothejungle.com/fr/companies/la-ruche-sociale", "La Ruche Sociale recrute", ""),
        r("https://www.other.fr/", "Une autre ruche", "apiculture"),
    ]
    v = assess_subject(results, names=["La Ruche Sociale SAS"], domain="larushesociale.fr")
    assert v.sufficient and [x.url for x in v.usable] == [results[0].url, results[1].url]
    miss = assess_subject(results[2:], names=["La Ruche Sociale"], domain="larushesociale.fr")
    assert not miss.sufficient and "0 result(s) mention" in miss.reason


@respx.mock
async def test_real_adapters_searxng_down_then_duckduckgo() -> None:
    """End-to-end over HTTP: SearXNG 502 → DuckDuckGo HTML answers."""
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(502))
    ddg = respx.post(DDG).mock(return_value=httpx.Response(200, text=fixture_text("ddg_results.html")))
    res = await discovery_chain(ddg_throttle=Throttle(0)).search(
        "agence marketing Lyon", region="FR", assess=lambda rs: assess_companies(rs, min_companies=3)
    )
    assert res.provider == "duckduckgo" and res.sufficient and ddg.called
    assert [a.provider for a in res.attempts] == ["searxng", "duckduckgo"]


@respx.mock
async def test_real_adapters_searxng_answers() -> None:
    respx.get(SEARXNG_SEARCH).mock(return_value=httpx.Response(200, json=sx_payload(COMPANY_RESULTS)))
    ddg = respx.post(DDG).mock(return_value=httpx.Response(500))
    res = await discovery_chain().search(
        "agence marketing Lyon", region="FR", assess=lambda rs: assess_companies(rs, min_companies=3)
    )
    assert res.provider == "searxng" and not ddg.called
    assert [x.url for x in res.usable] == [
        "https://www.pixel-studio-lyon.fr/",
        "https://lumiere-digitale.fr/agence",
        "https://kreacom.fr/",
        "https://atelier-nord.studio/",
    ]
