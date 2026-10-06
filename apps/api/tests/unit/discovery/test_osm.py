"""OpenStreetMap / Overpass adapter."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from scout.discovery.base import DiscoveryQuery
from scout.discovery.common import Throttle
from scout.discovery.osm import OsmSource, build_query, parse_elements
from scout.errors import FetchError, RateLimitedError

from .conftest import defn, fixture_json

OVERPASS = "https://overpass-api.de/api/interpreter"


def query() -> DiscoveryQuery:
    return DiscoveryQuery(
        key="osm:dentist:FR:Lyon",
        params={"tags": [["amenity", "dentist"], ["healthcare", "dentist"]], "city": "Lyon", "country": "FR"},
    )


@pytest.fixture
def src() -> OsmSource:
    return OsmSource(throttle=Throttle(0))


def test_build_query() -> None:
    ql = build_query([("amenity", "dentist")], "Villeneuve-d'Ascq", "FR")
    assert ql.startswith("[out:json]")
    assert 'area["ISO3166-1"="FR"][admin_level=2]->.country;' in ql
    assert '["name"="Villeneuve-d\'Ascq"]' in ql
    assert 'nwr["amenity"="dentist"]["website"](area.city);' in ql
    assert 'nwr["amenity"="dentist"]["contact:website"](area.city);' in ql
    assert ql.rstrip().endswith("out center tags 500;")
    assert '\\"' in build_query([("shop", "x")], 'Say "hi"', None)


def test_parse_only_pois_with_company_websites() -> None:
    cands = parse_elements(fixture_json("overpass_dentists.json"), query=query())
    assert [c.name for c in cands] == ["Cabinet Dentaire des Terreaux", "Clinique Dentaire Jean Macé"]
    a, b = cands
    assert (
        a.source_entity_id == "node/4567890123"
        and a.source_url == "https://www.openstreetmap.org/node/4567890123"
    )
    assert a.website == "https://dentiste-terreaux.fr/" and a.phone == "+33 4 72 00 00 10"
    assert a.category == "amenity=dentist" and a.location["postal_code"] == "69001"
    assert a.location["address"] == "3 Place des Terreaux, 69001, Lyon" and a.location["country"] == "FR"
    assert a.raw_data["osm_tags"]["opening_hours"] == "Mo-Fr 09:00-19:00"
    assert b.location["lat"] == 45.7485 and b.emails == ["accueil@clinique-jeanmace.fr"]
    assert b.category == "healthcare=dentist"


@respx.mock
async def test_discover(src: OsmSource) -> None:
    route = respx.post(OVERPASS).mock(
        return_value=httpx.Response(200, json=fixture_json("overpass_dentists.json"))
    )
    page = await src.discover(query(), None)
    assert len(page.candidates) == 2 and page.next_cursor is None
    body = parse_qs(route.calls.last.request.content.decode())
    assert '"name"="Lyon"' in body["data"][0]


MIRRORS = ["https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]


@respx.mock
async def test_errors(src: OsmSource) -> None:
    for url in (OVERPASS, *MIRRORS):
        respx.post(url).mock(return_value=httpx.Response(429, text="rate_limited"))
    with pytest.raises(RateLimitedError):
        await src.discover(query(), None)
    respx.post(OVERPASS).mock(
        return_value=httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out"})
    )
    with pytest.raises(FetchError):
        await src.discover(query(), None)


def test_plan_and_suitability() -> None:
    src = OsmSource()
    d = defn(industries=["dentistes"], cities=["Lyon"])
    assert src.suitability(d) == 0.9
    plan = src.plan(d)
    assert [q.key for q in plan] == ["osm:dentist:FR:Lyon"]
    assert ["amenity", "dentist"] in plan[0].params["tags"]
    assert src.suitability(defn(industries=["SaaS"], countries=["US"])) == 0.3
    assert src.suitability(defn(industries=["ecommerce brand"], countries=["US"])) == 0  # no OSM tags


@respx.mock
async def test_fails_over_to_a_mirror_when_the_main_instance_refuses(src: OsmSource) -> None:
    main = respx.post(OVERPASS).mock(return_value=httpx.Response(406, text="<html>Not Acceptable</html>"))
    mirror = respx.post(MIRRORS[0]).mock(
        return_value=httpx.Response(200, json=fixture_json("overpass_dentists.json"))
    )
    page = await src.discover(query(), None)
    assert main.called and mirror.called and len(page.candidates) == 2


@respx.mock
async def test_a_bad_query_is_not_retried_on_mirrors(src: OsmSource) -> None:
    respx.post(OVERPASS).mock(return_value=httpx.Response(400, text="parse error"))
    mirror = respx.post(MIRRORS[0]).mock(return_value=httpx.Response(200, json={"elements": []}))
    with pytest.raises(Exception, match="400"):
        await src.discover(query(), None)
    assert not mirror.called
