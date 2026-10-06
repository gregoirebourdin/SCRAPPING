"""City-level geocoding outside France (OpenStreetMap Nominatim): is a company really in the requested city?

France uses ``scout.discovery.communes`` (INSEE commune + postal codes). Everywhere else a named city is resolved
to its centre and extent (Nominatim bounding box), and a candidate's own city is geocoded the same way: inside
the requested city's extent (+ a small margin) → match; elsewhere → no match ("web agencies in Austin" never
returns Dallas). Nominatim's usage policy is honoured: identifying User-Agent, ≤ 1 request/second, every answer
cached per process. Unresolvable names give no verdict (``None``), never a guess.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from scout.config import get_settings
from scout.discovery.common import Throttle
from scout.util.text import normalize_key

log = structlog.get_logger(__name__)

MARGIN_KM = 2.0
MIN_RADIUS_KM = 3.0
MAX_RADIUS_KM = 40.0  # a metropolis' extent, never a whole region

_throttle = Throttle(1.1)
_cache: dict[tuple[str, str], Place | None] = {}


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    radius_km: float
    name: str


def _key(city: str, country: str | None) -> tuple[str, str]:
    return normalize_key(city or "").replace("-", " ").strip(), (country or "").upper()


def haversine_km(a: Place, b: Place) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def place_from(row: dict) -> Place | None:
    try:
        lat, lon = float(row["lat"]), float(row["lon"])
        s, n, w, e = (float(x) for x in row.get("boundingbox") or (lat, lat, lon, lon))
    except (KeyError, TypeError, ValueError):
        return None
    half_diag = haversine_km(Place(s, w, 0, ""), Place(n, e, 0, "")) / 2
    radius = min(MAX_RADIUS_KM, max(MIN_RADIUS_KM, half_diag))
    return Place(lat, lon, radius, str(row.get("display_name") or row.get("name") or ""))


def remember(city: str, country: str | None, place: Place | None) -> None:
    _cache[_key(city, country)] = place


def clear_cache() -> None:
    _cache.clear()


async def locate(city: str, country: str | None) -> Place | None:
    key = _key(city, country)
    if not key[0]:
        return None
    if key in _cache:
        return _cache[key]
    base = get_settings().nominatim_url
    if not base:
        return None
    from scout.discovery.common import http_request
    from scout.errors import JobError
    from scout.util.pools import pool

    params = {"city": city, "format": "jsonv2", "limit": 1, "addressdetails": 0}
    if country:
        params["countrycodes"] = country.lower()
    try:
        async with pool("public_api"):
            await _throttle.wait()
            resp = await http_request(
                "GET", base.rstrip("/") + "/search", source="geocode", params=params, timeout_s=10.0
            )
        rows = resp.json()
    except (JobError, ValueError) as exc:  # transient: not cached
        log.info("geocode.failed", city=city, country=country, error=str(exc)[:200])
        return None
    place = place_from(rows[0]) if isinstance(rows, list) and rows else None
    _cache[key] = place
    return place


async def in_requested_city(
    requested: Sequence[str], requested_country: str | None, city: str | None, country: str | None
) -> bool | None:
    """True inside one of the requested cities, False clearly elsewhere, None when it cannot be told."""
    if not city or not requested:
        return None
    cand = await locate(city, country or requested_country)
    if cand is None:
        return None
    unresolved = False
    for name in requested:
        want = await locate(name, requested_country)
        if want is None:
            unresolved = True
            continue
        if haversine_km(want, cand) <= want.radius_km + MARGIN_KM:
            return True
    return None if unresolved else False
