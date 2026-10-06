"""YC adapter: filtering a real subset of the yc-oss dataset, local pagination, candidate mapping."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from scout.discovery import yc
from scout.discovery.base import DiscoveryQuery
from scout.discovery.yc import YCSource, filter_companies

from .conftest import defn, fixture_json

URL = "https://yc-oss.github.io/api/companies/all.json"
SAAS_TAGS = ["B2B", "SaaS", "Enterprise Software", "Productivity", "Engineering, Product and Design", "Developer Tools"]


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    yc.clear_cache()
    yield
    yc.clear_cache()


def names(rows: list[dict]) -> list[str]:
    return [r["name"] for r in rows]


def test_filter_active_us_b2b_small_teams() -> None:
    rows = filter_companies(fixture_json("yc_companies.json"), tags=SAAS_TAGS, keywords=[], countries=["US"],
                            cities=[], bounds=(2, 30))
    assert names(rows) == ["Screenleap", "Videopixie", "WorkFlowy", "Olark", "Volantio", "Dagger", "OpenReplay"]
    # Rescale/Gusto too big, WireOver inactive, SendHub acquired, consumer companies not tagged


def test_filter_by_country_city_and_keywords() -> None:
    data = fixture_json("yc_companies.json")
    fr = filter_companies(data, tags=SAAS_TAGS, keywords=[], countries=["FR"], cities=[], bounds=(None, None))
    assert names(fr) == ["Dagger", "OpenReplay"]
    paris = filter_companies(data, tags=SAAS_TAGS, keywords=[], countries=[], cities=["Paris"], bounds=(None, None))
    assert names(paris) == ["OpenReplay"]
    kw = filter_companies(data, tags=[], keywords=["session replay"], countries=[], cities=[], bounds=(None, None))
    assert names(kw) == ["OpenReplay"]


def test_suitability_and_plan() -> None:
    src = YCSource()
    us = defn(industries=["SaaS"], countries=["US"], employee_range=(2, 30))
    assert src.suitability(us) == 1.0
    assert src.suitability(defn(industries=["SaaS"], countries=["FR"])) == 0.5
    assert src.suitability(defn(industries=["dentist"], countries=["US"])) == 0
    assert src.suitability(defn(industries=["marketing agency"], countries=["US"])) == 0
    plan = src.plan(us)
    assert len(plan) == 1 and plan[0].params["countries"] == ["US"]
    assert plan[0].params["min"] == 2 and plan[0].params["max"] == 30 and "SaaS" in plan[0].params["tags"]


@respx.mock
async def test_discover_paginates_locally_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=fixture_json("yc_companies.json")))
    monkeypatch.setattr(yc, "PAGE_SIZE", 3)
    q = DiscoveryQuery(key="yc:software_saas", params={"tags": SAAS_TAGS, "keywords": [], "countries": ["US"],
                                                       "cities": [], "min": 2, "max": 30})
    src = YCSource()
    p1 = await src.discover(q, None)
    assert [c.name for c in p1.candidates] == ["Screenleap", "Videopixie", "WorkFlowy"] and p1.next_cursor == {"offset": 3}
    p2 = await src.discover(q, p1.next_cursor)
    p3 = await src.discover(q, p2.next_cursor)
    assert [c.name for c in p3.candidates] == ["OpenReplay"] and p3.next_cursor is None
    assert route.call_count == 1  # dataset cached in-process

    c = p1.candidates[0]
    assert c.source == "yc" and c.source_entity_id == "screenleap-inc"
    assert c.website == "https://screenleap.com/" and c.domain == "screenleap.com"
    assert c.employee_min == c.employee_max == 4
    assert c.location == {"city": "San Francisco", "country": "US", "region": "CA"}
    assert c.raw_data["one_liner"].startswith("One-click screen sharing")
    assert c.source_url == "https://www.ycombinator.com/companies/screenleap-inc"


@respx.mock
async def test_location_prefers_requested_country() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=fixture_json("yc_companies.json")))
    q = DiscoveryQuery(key="yc", params={"tags": SAAS_TAGS, "countries": ["FR"], "cities": []})
    page = await YCSource().discover(q, None)
    locs = {c.name: c.location for c in page.candidates}
    assert locs["OpenReplay"] == {"city": "Paris", "country": "FR", "region": "Île-de-France"}
    assert locs["Dagger"]["city"] == "Lyon"
