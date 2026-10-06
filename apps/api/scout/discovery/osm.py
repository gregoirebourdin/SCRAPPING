"""OpenStreetMap discovery via the Overpass API (free; ≥ 5 s between requests, public_api pool).

Only POIs carrying a website tag are returned: they are the ones the pipeline can crawl and qualify.
"""

from __future__ import annotations

from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    Throttle,
    candidate_domain,
    http_request,
    profiles_for,
    target_countries,
)
from scout.errors import FetchError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

_throttle = Throttle(5.0)
WEBSITE_TAGS = ("website", "contact:website", "url")
MAX_ELEMENTS = 500


def _q(s: str) -> str:
    """Overpass QL string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(tags: list[tuple[str, str]], city: str, country: str | None, *, timeout: int = 90) -> str:
    """Overpass QL: POIs with any of ``tags`` and a website, inside the administrative area named ``city``."""
    lines = [f"[out:json][timeout:{timeout}];"]
    if country:
        lines.append(f'area["ISO3166-1"={_q(country)}][admin_level=2]->.country;')
        lines.append(
            f'rel(area.country)["boundary"="administrative"]["name"={_q(city)}]["admin_level"~"^[4-8]$"];'
        )
    else:
        lines.append(f'rel["boundary"="administrative"]["name"={_q(city)}]["admin_level"~"^[4-8]$"];')
    lines.append("map_to_area->.city;")
    lines.append("(")
    for k, v in tags:
        for wt in WEBSITE_TAGS[:2]:
            lines.append(f"  nwr[{_q(k)}={_q(v)}][{_q(wt)}](area.city);")
    lines.append(");")
    lines.append(f"out center tags {MAX_ELEMENTS};")
    return "\n".join(lines)


def parse_elements(body: dict[str, Any], *, query: DiscoveryQuery | None = None) -> list[RawCandidate]:
    params = query.params if query else {}
    wanted = {tuple(t) for t in params.get("tags", [])}
    out: list[RawCandidate] = []
    seen: set[str] = set()
    for el in body.get("elements") or []:
        tags: dict[str, str] = el.get("tags") or {}
        name = (tags.get("name") or tags.get("brand") or "").strip()
        website = next((tags[t] for t in WEBSITE_TAGS if tags.get(t)), None)
        dom = candidate_domain(website)
        if not name or not dom or tags.get("disused") == "yes" or "disused:amenity" in tags:
            continue
        osm_id = f"{el.get('type')}/{el.get('id')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)
        center = el.get("center") or {}
        street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
        category = next((f"{k}={v}" for k, v in tags.items() if (k, v) in wanted), None)
        location = {
            "country": (tags.get("addr:country") or params.get("country") or "").upper() or None,
            "city": tags.get("addr:city") or params.get("city"),
            "postal_code": tags.get("addr:postcode"),
            "address": ", ".join(x for x in (street, tags.get("addr:postcode"), tags.get("addr:city")) if x) or None,
            "lat": el.get("lat", center.get("lat")),
            "lng": el.get("lon", center.get("lon")),
        }
        out.append(
            RawCandidate(
                source="osm",
                source_entity_id=osm_id,
                name=name,
                website=normalize_website(website),
                domain=dom,
                location={k: v for k, v in location.items() if v not in (None, "")},
                category=category,
                source_url=f"https://www.openstreetmap.org/{osm_id}",
                phone=tags.get("phone") or tags.get("contact:phone"),
                emails=[e.strip().lower() for e in (tags.get("email") or tags.get("contact:email") or "").split(";") if "@" in e],
                status="active",
                raw_data={"osm_tags": dict(list(tags.items())[:40])},
            )
        )
    return out


class OsmSource:
    key = "osm"
    name = "OpenStreetMap (Overpass)"
    quality = 0.7
    cost_class = "FREE"

    def __init__(self, *, throttle: Throttle | None = None) -> None:
        self.throttle = throttle or _throttle

    def is_configured(self) -> bool:
        return bool(get_settings().overpass_url)

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = [p for p in profiles_for(defn) if p.osm_tags]
        if not profiles or not (target_countries(defn) or defn.company_filters.cities):
            return 0.0
        return 0.9 if profiles[0].local_business else 0.3

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        cf = defn.company_filters
        profiles = [p for p in profiles_for(defn) if p.osm_tags][:2]
        if not profiles:
            return []
        countries: list[str | None] = list(target_countries(defn)[:3]) or [None]
        queries: list[DiscoveryQuery] = []
        for cc in countries:
            if cc:
                cities = geo.cities_for(cc, regions=cf.regions, cities=cf.cities, expansion=expansion)
            else:
                cities = [geo.City(name=c.strip(), country="") for c in cf.cities]
            for prof in profiles:
                for rank, city in enumerate(cities):
                    queries.append(
                        DiscoveryQuery(
                            key=f"osm:{prof.key}:{cc or '-'}:{city.name}",
                            params={"tags": [list(t) for t in prof.osm_tags], "city": city.name, "country": cc},
                            weight=round(1.0 / (1 + 0.1 * rank), 4),
                        )
                    )
        return queries

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        p = query.params
        ql = build_query([tuple(t) for t in p["tags"]], p["city"], p.get("country"))
        async with pool("public_api"):
            await self.throttle.wait()
            resp = await http_request(
                "POST", get_settings().overpass_url, source=self.key, data={"data": ql}, timeout_s=120.0
            )
        await record_usage(UsageCategory.registry_request, source_key=self.key, resolver="discovery")
        try:
            body = resp.json()
        except ValueError as exc:
            raise FetchError(f"{self.key}: invalid JSON (Overpass busy?)") from exc
        remark = str(body.get("remark") or "")
        if "timed out" in remark or "out of memory" in remark:
            raise FetchError(f"{self.key}: {remark[:200]}")
        candidates = parse_elements(body, query=query)
        log.debug("osm.page", query=query.key, elements=len(body.get("elements") or []), kept=len(candidates))
        return DiscoveryPage(candidates=candidates, next_cursor=None)
