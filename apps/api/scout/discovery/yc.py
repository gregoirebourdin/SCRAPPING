"""Y Combinator company directory (public yc-oss JSON mirror). Free; the dataset is cached in-process for 24 h.

Filters: Active status, YC industries/tags of the ICP's profiles (or keywords), countries/cities (from
``regions`` and ``all_locations``), team size within the employee range. Paginated locally (cursor offset).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    candidate_domain,
    employee_bounds,
    http_request,
    profiles_for,
    size_fits,
    target_countries,
)
from scout.errors import FetchError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.text import normalize_key
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

PAGE_SIZE = 50
CACHE_TTL_S = 24 * 3600

# YC "regions" use these names for the main countries.
_YC_COUNTRY_NAMES = {"US": "United States of America", "GB": "United Kingdom"}

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_locks: dict[int, asyncio.Lock] = {}


async def load_companies(url: str | None = None, *, force: bool = False) -> list[dict[str, Any]]:
    """Fetch (or reuse) the YC company list."""
    url = url or get_settings().yc_companies_url
    hit = _cache.get(url)
    if hit and not force and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    lock = _locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
    async with lock:
        hit = _cache.get(url)
        if hit and not force and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
        async with pool("public_api"):
            resp = await http_request("GET", url, source="yc", timeout_s=60.0)
        await record_usage(UsageCategory.registry_request, source_key="yc", resolver="discovery")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError("yc: invalid JSON") from exc
        if not isinstance(data, list):
            raise FetchError("yc: unexpected payload")
        _cache[url] = (time.monotonic(), data)
        return data


def clear_cache() -> None:
    _cache.clear()


def _country_names(cc: str) -> set[str]:
    names = {geo.country_name(cc), _YC_COUNTRY_NAMES.get(cc, "")}
    if cc == "US":
        names.add("USA")
    return {normalize_key(n) for n in names if n}


def _matches_geo(c: dict[str, Any], countries: list[str], cities: list[str]) -> bool:
    locs = normalize_key(c.get("all_locations") or "")
    regions = {normalize_key(r) for r in c.get("regions") or []}
    if countries:
        ok = False
        for cc in countries:
            names = _country_names(cc)
            if names & regions or any(n and n in locs for n in names):
                ok = True
                break
        if not ok:
            return False
    if cities:
        return any(normalize_key(city) in locs for city in cities)
    return True


def filter_companies(
    companies: list[dict[str, Any]],
    *,
    tags: list[str],
    keywords: list[str],
    countries: list[str],
    cities: list[str],
    bounds: tuple[int | None, int | None],
) -> list[dict[str, Any]]:
    want_tags = {t.lower() for t in tags}
    want_kw = [normalize_key(k) for k in keywords if k.strip()]
    out: list[dict[str, Any]] = []
    for c in companies:
        if (c.get("status") or "").lower() != "active" or not c.get("website"):
            continue
        labels = {
            str(x).lower()
            for x in [*(c.get("industries") or []), *(c.get("tags") or []), c.get("industry") or ""]
        }
        if want_tags and not (labels & want_tags):
            continue
        if not want_tags and want_kw:
            text = normalize_key(
                " ".join([c.get("one_liner") or "", c.get("long_description") or "", *labels])
            )
            if not any(k in text for k in want_kw):
                continue
        if not _matches_geo(c, countries, cities):
            continue
        if not size_fits(c.get("team_size"), bounds):
            continue
        out.append(c)
    return out


def _pick_location(c: dict[str, Any], countries: list[str], cities: list[str]) -> dict[str, Any]:
    parts = [p.strip() for p in (c.get("all_locations") or "").split(";") if p.strip()]
    chosen = parts[0] if parts else ""
    for p in parts:
        k = normalize_key(p)
        if any(normalize_key(city) in k for city in cities) or any(
            n in k for cc in countries for n in _country_names(cc)
        ):
            chosen = p
            break
    segs = [s.strip() for s in chosen.split(",") if s.strip()]
    loc: dict[str, Any] = {}
    if segs:
        loc["city"] = segs[0]
        cc = geo.country_code(segs[-1]) if len(segs) > 1 else None
        if cc:
            loc["country"] = cc
        if len(segs) > 2:
            loc["region"] = segs[1]
    return loc


def to_candidate(c: dict[str, Any], countries: list[str], cities: list[str]) -> RawCandidate | None:
    dom = candidate_domain(c.get("website"))
    if not dom:
        return None
    size = c.get("team_size") if isinstance(c.get("team_size"), int) else None
    return RawCandidate(
        source="yc",
        source_entity_id=c.get("slug") or str(c.get("id")),
        name=(c.get("name") or "").strip(),
        website=normalize_website(c.get("website")),
        domain=dom,
        location=_pick_location(c, countries, cities),
        category=c.get("subindustry") or c.get("industry"),
        source_url=c.get("url"),
        employee_min=size,
        employee_max=size,
        status="active",
        raw_data={
            "one_liner": c.get("one_liner"),
            "long_description": (c.get("long_description") or "")[:600] or None,
            "batch": c.get("batch"),
            "stage": c.get("stage"),
            "tags": c.get("tags"),
            "industries": c.get("industries"),
            "regions": c.get("regions"),
            "all_locations": c.get("all_locations"),
            "team_size": size,
            "is_hiring": c.get("isHiring"),
            "top_company": c.get("top_company"),
        },
    )


class YCSource:
    key = "yc"
    name = "Y Combinator company directory"
    quality = 0.85
    cost_class = "FREE"

    def is_configured(self) -> bool:
        return bool(get_settings().yc_companies_url)

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = [p for p in profiles_for(defn) if p.yc_industries]
        if not profiles:
            return 0.0
        base = 1.0 if profiles[0].digital else 0.3
        countries = target_countries(defn)
        if countries and "US" not in countries:
            base *= 0.5  # YC is US-heavy: still useful, rarely sufficient
        return base

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        profiles = [p for p in profiles_for(defn) if p.yc_industries][:3]
        if not profiles:
            return []
        tags = list(dict.fromkeys(t for p in profiles for t in p.yc_industries))
        lo, hi = employee_bounds(defn)
        params = {
            "tags": tags,
            "keywords": [],
            "countries": target_countries(defn),
            "cities": list(defn.company_filters.cities),
            "min": lo,
            "max": hi,
        }
        return [DiscoveryQuery(key="yc:" + ",".join(sorted(p.key for p in profiles)), params=params)]

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        offset = int((cursor or {}).get("offset", 0))
        p = query.params
        companies = await load_companies()
        matches = filter_companies(
            companies,
            tags=p.get("tags") or [],
            keywords=p.get("keywords") or [],
            countries=p.get("countries") or [],
            cities=p.get("cities") or [],
            bounds=(p.get("min"), p.get("max")),
        )
        window = matches[offset : offset + PAGE_SIZE]
        candidates = [
            c for c in (to_candidate(x, p.get("countries") or [], p.get("cities") or []) for x in window) if c
        ]
        nxt = offset + PAGE_SIZE
        return DiscoveryPage(
            candidates=candidates, next_cursor={"offset": nxt} if nxt < len(matches) else None
        )
