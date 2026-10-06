"""Google Places (official API) source: query plan, mapping, pagination and the free-tier monthly cap."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scout.config import get_settings
from scout.discovery import google_places
from scout.discovery.base import DiscoveryQuery
from scout.discovery.common import Throttle
from scout.discovery.google_places import URL, GooglePlacesSource
from scout.errors import PermanentError
from scout.schemas.campaign import CampaignDefinition

PLACE = {
    "id": "ChIJ123",
    "displayName": {"text": "Agence Pixel"},
    "formattedAddress": "3 Rue Royale, 74000 Annecy, France",
    "addressComponents": [
        {"longText": "Annecy", "shortText": "Annecy", "types": ["locality", "political"]},
        {"longText": "74000", "shortText": "74000", "types": ["postal_code"]},
        {"longText": "France", "shortText": "FR", "types": ["country", "political"]},
    ],
    "websiteUri": "https://www.agence-pixel.fr/?utm_source=gmb",
    "nationalPhoneNumber": "04 50 00 00 00",
    "primaryTypeDisplayName": {"text": "Concepteur de sites Web"},
    "businessStatus": "OPERATIONAL",
    "googleMapsUri": "https://maps.google.com/?cid=1",
}


@pytest.fixture
def src(monkeypatch) -> GooglePlacesSource:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    get_settings.cache_clear()

    async def calls() -> int:
        return 0

    monkeypatch.setattr(google_places, "_calls_this_month", calls)
    yield GooglePlacesSource(throttle=Throttle(0))
    get_settings.cache_clear()


def _defn() -> CampaignDefinition:
    return CampaignDefinition.model_validate(
        {"company_filters": {"industries": ["web agency"], "countries": ["FR"], "cities": ["Annecy"]}}
    )


def test_plan_is_trade_city_in_the_country_language(src: GooglePlacesSource) -> None:
    assert src.is_configured() and src.suitability(_defn()) > 1
    q = src.plan(_defn())[0]
    assert q.params["text"].endswith("Annecy") and q.params["country"] == "FR" and q.params["lang"] == "fr"


@respx.mock
async def test_places_map_to_candidates_with_website_phone_and_address(src: GooglePlacesSource) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={"places": [PLACE], "nextPageToken": "t2"})
    )
    q = DiscoveryQuery(key="places:x", params={"text": "agence web Annecy", "lang": "fr", "country": "FR"})
    page = await src.discover(q, None)
    (c,) = page.candidates
    assert c.name == "Agence Pixel" and c.domain == "agence-pixel.fr" and c.phone
    assert (
        c.location["city"] == "Annecy"
        and c.location["postal_code"] == "74000"
        and c.location["country"] == "FR"
    )
    assert page.next_cursor == {"page": 2, "token": "t2"}
    sent = route.calls.last.request
    assert sent.headers["X-Goog-Api-Key"] == "test-key" and "websiteUri" in sent.headers["X-Goog-FieldMask"]
    assert json.loads(sent.content)["regionCode"] == "FR"


async def test_the_monthly_cap_keeps_the_free_tier(src: GooglePlacesSource, monkeypatch) -> None:
    async def full() -> int:
        return 10_000

    monkeypatch.setattr(google_places, "_calls_this_month", full)
    with pytest.raises(PermanentError, match="monthly cap"):
        await src.discover(DiscoveryQuery(key="k", params={"text": "x", "country": "FR"}), None)
