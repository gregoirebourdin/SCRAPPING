"""Source router: ICP-dependent selection, preferences, health and the fixture override."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from scout.ai.factory import FakeProvider, set_ai
from scout.discovery.health import SourceHealth
from scout.discovery.router import all_sources, get_source, health_factor, select_sources

from .conftest import FIXTURES, defn


@pytest.fixture
def ai() -> Iterator[None]:
    set_ai(FakeProvider())
    yield
    set_ai(None)


@pytest.fixture
def maps(settings_env) -> None:
    settings_env(GMAPS_SCRAPER_URL="http://maps-scraper.railway.internal:8080")


def keys(selection) -> list[str]:
    return [s.key for s, _ in selection]


def health(key: str, **kw) -> SourceHealth:
    base = dict(
        key=key,
        success_rate=1.0,
        block_rate=0.0,
        avg_latency_ms=100.0,
        results_per_query=10.0,
        duplicate_rate=0.0,
        qualification_rate=0.0,
        unhealthy_until=None,
        healthy=True,
        requests=50,
    )
    base.update(kw)
    return SourceHealth(**base)


def test_registry_and_lookup() -> None:
    assert {s.key for s in all_sources()} == {
        "fixture",
        "fr_registry",
        "google_maps",
        "osm",
        "yc",
        "web_search",
        "gemini_search",
        "hn_hiring",
        "github",
    }
    assert get_source("maps").key == "google_maps" and get_source("HN").key == "hn_hiring"
    assert get_source("nope") is None


def test_fr_marketing_agencies_registry_first(maps, ai) -> None:
    sel = select_sources(defn(industries=["agences marketing"], countries=["FR"], employee_range=(2, 30)))
    assert keys(sel)[0] == "fr_registry"
    assert "web_search" in keys(sel) and "yc" not in keys(sel)
    prios = [p for _, p in sel]
    assert prios == sorted(prios, reverse=True) and len(set(prios)) == len(prios)


def test_dentists_in_lyon_local_sources_first(maps, ai) -> None:
    sel = keys(select_sources(defn(industries=["dentistes"], cities=["Lyon"])))
    assert set(sel[:3]) == {"google_maps", "osm", "fr_registry"}
    assert sel[0] == "google_maps"
    without_maps = keys(
        select_sources(
            defn(industries=["dentistes"], cities=["Lyon"], countries=["FR"]),
            health={"google_maps": health("google_maps", healthy=False)},
        )
    )
    assert set(without_maps[:2]) == {"fr_registry", "osm"}


def test_saas_us_digital_sources(maps, ai) -> None:
    sel = keys(
        select_sources(
            defn(
                industries=["SaaS startups"],
                countries=["US"],
                employee_range=(2, 30),
                _extra={"people_filters": {"titles": ["Founder"]}},
            )
        )
    )
    assert sel[0] == "yc"
    assert {"web_search", "gemini_search"} <= set(sel[:3])
    assert "fr_registry" not in sel and "google_maps" not in sel


def test_unconfigured_sources_are_skipped(ai) -> None:
    sel = keys(select_sources(defn(industries=["dentistes"], cities=["Lyon"]), limit=9))
    assert "google_maps" not in sel  # GMAPS_SCRAPER_URL unset
    set_ai(None)
    from scout.ai.factory import LocalProvider

    set_ai(LocalProvider())
    assert "gemini_search" not in keys(select_sources(defn(industries=["SaaS"], countries=["US"]), limit=9))


def test_preferences_and_exclusions(maps, ai) -> None:
    d = defn(
        industries=["agences marketing"],
        countries=["FR"],
        _extra={"sources": {"preferred": ["osm"], "excluded": ["registry", "duckduckgo"]}},
    )
    sel = keys(select_sources(d))
    assert sel[0] == "osm" and "fr_registry" not in sel and "web_search" not in sel


def test_health_factor_and_cooldown(maps, ai) -> None:
    assert health_factor(None) == 1.0
    assert health_factor(health("x", success_rate=0.2, block_rate=0.6)) < 0.5
    assert health_factor(health("x", requests=2, success_rate=0.0)) == 1.0  # not enough evidence
    d = defn(industries=["agences marketing"], countries=["FR"])
    cooling = health("fr_registry", healthy=False, unhealthy_until=datetime.now(UTC) + timedelta(minutes=15))
    assert "fr_registry" not in keys(select_sources(d, health={"fr_registry": cooling}))
    degraded = keys(
        select_sources(d, health={"fr_registry": health("fr_registry", success_rate=0.1, block_rate=0.9)})
    )
    assert degraded[0] != "fr_registry"


def test_fixture_is_used_alone(settings_env, maps, ai) -> None:
    settings_env(DISCOVERY_FIXTURE_MANIFEST=str(FIXTURES / "fixture_manifest.json"))
    sel = select_sources(defn(industries=["agences marketing"], countries=["FR"]))
    assert keys(sel) == ["fixture"] and sel[0][1] == 100
    excluded = defn(
        industries=["agences marketing"], countries=["FR"], _extra={"sources": {"excluded": ["fixture"]}}
    )
    assert keys(select_sources(excluded))[0] == "fr_registry"


def test_gemini_search_never_runs_without_the_free_web_search(maps, ai) -> None:
    # gemini_search skips segments that free search already covers, so it must never be selected alone
    d = defn(industries=["SaaS"], countries=["US"], _extra={"sources": {"preferred": ["gemini_search"]}})
    for limit in (1, 2, 3):
        sel = keys(select_sources(d, limit=limit))
        assert "gemini_search" in sel and "web_search" in sel, (limit, sel)
    # …unless the user excluded web search: then Gemini runs on its own as before
    alone = defn(
        industries=["SaaS"],
        countries=["US"],
        _extra={"sources": {"preferred": ["gemini_search"], "excluded": ["web_search"]}},
    )
    sel = keys(select_sources(alone, limit=1))
    assert sel == ["gemini_search"]
