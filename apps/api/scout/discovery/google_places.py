"""Google Maps businesses through the official Places API (New) — Text Search.

Local businesses come with their website, phone and exact address, so they skip the slow, lossy website
resolution that registry entries need (most registry companies of a small city have no findable site). Queries
are "<trade> <city>" in the country's language, restricted to the country (``regionCode``); the location gate
still checks every address. Contact fields bill the Text Search Enterprise SKU, free up to Google's monthly cap
(1,000 calls in 2026): ``GOOGLE_PLACES_MONTHLY_CAP`` (default 900 calls, counted in ``usage_events``) keeps the
source inside the free tier — at the cap it stops for the month instead of billing. Requires
``GOOGLE_PLACES_API_KEY`` (a key restricted to the Places API).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    Throttle,
    candidate_domain,
    http_request,
    is_local_icp,
    profiles_for,
    target_countries,
)
from scout.errors import FetchError, PermanentError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.primaryType",
        "places.primaryTypeDisplayName",
        "places.businessStatus",
        "places.googleMapsUri",
        "nextPageToken",
    ]
)
MAX_PAGES = 3  # 20 places per page, 60 per query
_throttle = Throttle(0.2)


def _component(place: dict[str, Any], kind: str) -> str | None:
    for c in place.get("addressComponents") or []:
        if kind in (c.get("types") or []):
            return c.get("longText") or c.get("shortText")
    return None


def _country(place: dict[str, Any]) -> str | None:
    for c in place.get("addressComponents") or []:
        if "country" in (c.get("types") or []):
            return (c.get("shortText") or "").upper() or None
    return None


def map_place(place: dict[str, Any], query: DiscoveryQuery) -> RawCandidate | None:
    name = ((place.get("displayName") or {}).get("text") or "").strip()
    if not name:
        return None
    website = normalize_website(place.get("websiteUri")) if place.get("websiteUri") else None
    status = place.get("businessStatus")
    return RawCandidate(
        source="google_places",
        source_entity_id=place.get("id"),
        name=name[:300],
        website=website,
        domain=candidate_domain(website) if website else None,
        location={
            k: v
            for k, v in {
                "country": _country(place) or query.params.get("country"),
                "city": _component(place, "locality") or _component(place, "postal_town"),
                "postal_code": _component(place, "postal_code"),
                "region": _component(place, "administrative_area_level_1"),
                "address": place.get("formattedAddress"),
            }.items()
            if v
        },
        category=(place.get("primaryTypeDisplayName") or {}).get("text") or place.get("primaryType"),
        source_url=place.get("googleMapsUri"),
        raw_data={"place_id": place.get("id"), "primary_type": place.get("primaryType")},
        phone=place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"),
        status="closed" if status in ("CLOSED_PERMANENTLY",) else "active",
    )


async def _calls_this_month() -> int:
    from scout.db.engine import session_scope
    from scout.db.models import UsageEvent

    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as s:
        n = await s.scalar(
            sa.select(sa.func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.source_key == "google_places", UsageEvent.created_at >= start)
        )
    return int(n or 0)


class GooglePlacesSource:
    key = "google_places"
    name = "Google Maps (Places API)"
    quality = 0.9
    cost_class = "CHEAP"

    def __init__(self, *, throttle: Throttle | None = None) -> None:
        self.throttle = throttle or _throttle

    def is_configured(self) -> bool:
        key = get_settings().google_places_api_key
        return bool(key and key.get_secret_value())

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = profiles_for(defn)
        if not profiles or not target_countries(defn):
            return 0.0
        if is_local_icp(profiles) or defn.company_filters.cities:
            return 1.2  # businesses that come with their website, phone and address: the best local source
        return 0.5

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        cf = defn.company_filters
        profiles = profiles_for(defn)[:2]
        countries = target_countries(defn)[:3]
        queries: list[DiscoveryQuery] = []
        for cc in countries:
            lang = geo.country_language(cc)
            cities = geo.cities_for(cc, regions=cf.regions, cities=cf.cities, expansion=expansion)
            for prof in profiles:
                terms = list(dict.fromkeys([*prof.maps(lang)[:2], *cf.keywords[:1]]))
                for rank, city in enumerate(cities):
                    for term in terms:
                        queries.append(
                            DiscoveryQuery(
                                key=f"places:{prof.key}:{cc}:{city.name}:{term}",
                                params={
                                    "text": f"{term} {city.name}",
                                    "lang": lang if len(lang) == 2 else "en",
                                    "country": cc,
                                    "city": city.name,
                                },
                                weight=round(1.0 / (1 + rank * 0.1), 4),
                            )
                        )
        return queries

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        s = get_settings()
        if not self.is_configured():
            raise PermanentError("google_places: GOOGLE_PLACES_API_KEY not configured")
        if await _calls_this_month() >= s.google_places_monthly_cap:
            raise PermanentError(
                f"google_places: monthly cap of {s.google_places_monthly_cap} calls reached (free tier kept)"
            )
        p = query.params
        page = int((cursor or {}).get("page", 1))
        body: dict[str, Any] = {
            "textQuery": p["text"],
            "languageCode": p.get("lang", "en"),
            "regionCode": p.get("country"),
            "pageSize": 20,
        }
        if cursor and cursor.get("token"):
            body["pageToken"] = cursor["token"]
        assert s.google_places_api_key is not None
        async with pool("public_api"):
            await self.throttle.wait()
            resp = await http_request(
                "POST",
                URL,
                source=self.key,
                json=body,
                headers={
                    "X-Goog-Api-Key": s.google_places_api_key.get_secret_value(),
                    "X-Goog-FieldMask": FIELDS,
                    "Content-Type": "application/json",
                },
                timeout_s=20.0,
            )
        await record_usage(UsageCategory.maps_request, source_key=self.key, resolver="discovery")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError(f"{self.key}: invalid JSON") from exc
        candidates = [c for c in (map_place(x, query) for x in data.get("places") or []) if c]
        token = data.get("nextPageToken")
        nxt = {"page": page + 1, "token": token} if token and page < MAX_PAGES else None
        log.debug("google_places.page", query=query.key, page=page, n=len(candidates))
        return DiscoveryPage(candidates=candidates, next_cursor=nxt)
