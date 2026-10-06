"""Search layer unit-test helpers: settings overrides, SearXNG JSON payloads, stats capture. Offline only
(respx mocks every HTTP call)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from scout.ai.factory import FakeProvider, set_ai
from scout.config import Settings, get_settings
from scout.search import reset_state
from scout.services.usage import BudgetState, UsageContext, usage_scope

SEARXNG = "http://searxng.test:8080"
SEARXNG_SEARCH = f"{SEARXNG}/search"
DDG = "https://html.duckduckgo.com/html/"


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Settings]]:
    """Apply env overrides (None = unset) and return fresh settings; cache is reset afterwards."""

    def apply(**env: str | None) -> Settings:
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        return get_settings()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _search_defaults(settings_env: Callable[..., Settings]) -> None:
    """SearXNG configured at a fake private URL, default provider order, fresh health + cache."""
    settings_env(
        SEARXNG_URL=SEARXNG,
        SEARCH_PROVIDERS="searxng,duckduckgo",
        SEARCH_LOOKUP_PROVIDERS="searxng",
        GEMINI_SEARCH_FALLBACK_ONLY="true",
        WEB_RESEARCH_CRAWL_TOP="0",
        SEARCH_MIN_COMPANIES="3",
        SEARXNG_ENGINES=None,
        GMAPS_SCRAPER_URL=None,
        DISCOVERY_FIXTURE_MANIFEST=None,
    )
    reset_state()


@pytest.fixture
def fake_ai() -> Iterator[FakeProvider]:
    provider = FakeProvider()
    set_ai(provider)
    yield provider
    set_ai(None)


@pytest.fixture
def stats(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture learning-stats events (inside a usage scope, without touching the database)."""
    events: list[dict[str, Any]] = []

    class _Mod:
        @staticmethod
        def StatEvent(**kw: Any) -> dict[str, Any]:  # noqa: N802 - mirrors scout.learning.stats.StatEvent
            return kw

        @staticmethod
        async def record(evs: list[dict[str, Any]]) -> None:
            events.extend(evs)

    async def no_usage(*_a: Any, **_k: Any) -> None:
        return None

    async def budget(*_a: Any, **_k: Any) -> BudgetState:
        return BudgetState(month_spend_usd=0.0, monthly_budget_usd=30.0, hard_cap=True)

    monkeypatch.setattr("scout.search.telemetry._stats_module", lambda: _Mod)
    monkeypatch.setattr("scout.services.usage.record_usage", no_usage)
    monkeypatch.setattr("scout.services.usage.workspace_budget", budget)  # no database in unit tests
    with usage_scope(UsageContext(workspace_id=uuid.uuid4())):
        yield events


def sx_result(url: str, title: str, content: str = "", engines: tuple[str, ...] = ("bing",)) -> dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "content": content,
        "engine": engines[0],
        "engines": list(engines),
        "positions": [1],
        "score": 1.0,
        "category": "general",
        "publishedDate": None,
    }


def sx_payload(results: list[dict[str, Any]], unresponsive: list[list[str]] | None = None) -> dict[str, Any]:
    return {
        "query": "q",
        "number_of_results": 0,
        "results": results,
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": unresponsive or [],
    }


COMPANY_RESULTS = [
    sx_result("https://www.pixel-studio-lyon.fr/", "Agence Web Lyon - Pixel Studio", "Agence marketing à Lyon"),
    sx_result("https://lumiere-digitale.fr/agence", "Lumière Digitale – Agence marketing digital à Lyon"),
    sx_result("https://www.pagesjaunes.fr/annuaire/lyon/agences", "Agences marketing Lyon - PagesJaunes"),
    sx_result("https://fr.linkedin.com/company/kreacom", "Kreacom | LinkedIn"),
    sx_result("https://kreacom.fr/", "Kreacom, agence de communication", engines=("brave", "duckduckgo")),
    sx_result("https://blog.x.fr/top-10-agences-lyon", "Top 10 des meilleures agences marketing à Lyon"),
    sx_result("https://atelier-nord.studio/", "Accueil - Atelier Nord"),
]
