"""City precision outside France: Nominatim geocoding of the requested city and the company's own city."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from scout.discovery import geocode
from scout.discovery.geocode import Place, in_requested_city, place_from
from scout.pipeline.scoring import location_fit
from scout.schemas.campaign import CampaignDefinition

AUSTIN = Place(30.2672, -97.7431, 20.0, "Austin, Texas")
ROUND_ROCK = Place(30.5083, -97.6789, 6.0, "Round Rock, Texas")  # ~27 km north: another city
DALLAS = Place(32.7767, -96.7970, 25.0, "Dallas, Texas")


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    geocode.clear_cache()
    yield
    geocode.clear_cache()


def test_extent_comes_from_the_bounding_box_and_is_bounded() -> None:
    p = place_from({"lat": "30.27", "lon": "-97.74", "boundingbox": ["30.10", "30.52", "-97.94", "-97.56"]})
    assert p is not None and 15 < p.radius_km <= geocode.MAX_RADIUS_KM
    tiny = place_from({"lat": "45.9", "lon": "6.1", "boundingbox": ["45.9", "45.9", "6.1", "6.1"]})
    assert tiny is not None and tiny.radius_km == geocode.MIN_RADIUS_KM


async def test_inside_the_requested_city_only() -> None:
    geocode.remember("Austin", "US", AUSTIN)
    geocode.remember("Dallas", "US", DALLAS)
    geocode.remember("Round Rock", "US", ROUND_ROCK)
    assert await in_requested_city(["Austin"], "US", "Austin", "US") is True
    assert await in_requested_city(["Austin"], "US", "Dallas", "US") is False
    assert await in_requested_city(["Austin"], "US", "Round Rock", "US") is False
    assert await in_requested_city(["Austin"], "US", None, "US") is None


def test_the_gate_uses_the_geocoded_verdict_outside_france() -> None:
    d = CampaignDefinition.model_validate({"company_filters": {"industries": ["web agency"], "cities": ["Austin"], "countries": ["US"]}})
    assert location_fit(d, "US", "Dallas", None, None, geo_match=False) == 0.0
    assert location_fit(d, "US", "Austin", None, None, geo_match=True) == 1.0


@respx.mock
async def test_locate_honours_the_cache() -> None:
    route = respx.get("https://nominatim.openstreetmap.org/search").mock(
        return_value=httpx.Response(200, json=[{"lat": "30.27", "lon": "-97.74", "boundingbox": ["30.1", "30.5", "-97.9", "-97.5"]}])
    )
    geocode._throttle.min_interval = 0  # type: ignore[attr-defined]
    assert await geocode.locate("Austin", "US") is not None
    assert await geocode.locate("austin", "us") is not None
    assert route.call_count == 1
