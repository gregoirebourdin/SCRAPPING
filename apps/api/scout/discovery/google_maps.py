"""Google Maps discovery through an isolated gosom/google-maps-scraper web service (Railway service C).

Lifecycle per query: create a job (POST /api/v1/jobs) → poll (bounded) → download CSV → delete job.
While the remote job runs, ``discover`` returns an empty page whose cursor holds ``job_id`` so the caller
reschedules; the next call resumes polling. A vanished job (404) is recreated; a failed one raises a retryable
error after cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import time
from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import candidate_domain, http_request, profiles_for, target_countries
from scout.errors import FetchError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

STATUS_PENDING = {"pending", "working"}
_CLOSED_MARKERS = (
    "permanently closed",
    "définitivement fermé",
    "definitivement ferme",
    "dauerhaft geschlossen",
    "cerrado permanentemente",
    "chiuso definitivamente",
    "permanentemente chiuso",
    "fechado permanentemente",
)


def _closed(status: str | None) -> bool:
    s = (status or "").lower()
    return any(m in s for m in _CLOSED_MARKERS)


def _float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _float(v)
    return int(f) if f is not None else None


def _json(v: str | None) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


def parse_csv(text: str, *, query: DiscoveryQuery | None = None) -> list[RawCandidate]:
    """gosom CSV export → candidates (one per row with a title)."""
    csv.field_size_limit(max(csv.field_size_limit(), 32 * 1024 * 1024))
    params = query.params if query else {}
    out: list[RawCandidate] = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        name = (row.get("title") or "").strip()
        if not name:
            continue
        addr = _json(row.get("complete_address")) or {}
        if not isinstance(addr, dict):
            addr = {}
        country = geo.country_code(addr.get("country")) or params.get("country")
        website_raw = (row.get("website") or "").strip() or None
        website = normalize_website(website_raw) if candidate_domain(website_raw) else None
        emails = [e.strip().lower() for e in (row.get("emails") or "").split(",") if "@" in e]
        entity_id = next((row.get(k) for k in ("place_id", "cid", "data_id") if row.get(k)), None) or row.get(
            "link"
        )
        location = {
            "country": country,
            "city": addr.get("city") or params.get("city"),
            "postal_code": addr.get("postal_code"),
            "region": addr.get("state"),
            "address": (row.get("address") or "").strip() or None,
            "lat": _float(row.get("latitude")),
            "lng": _float(row.get("longitude")),
        }
        out.append(
            RawCandidate(
                source="google_maps",
                source_entity_id=entity_id,
                name=name,
                website=website,
                location={k: v for k, v in location.items() if v not in (None, "")},
                category=(row.get("category") or "").strip() or None,
                source_url=(row.get("link") or "").strip() or None,
                phone=(row.get("phone") or "").strip() or None,
                status="closed" if _closed(row.get("status")) else "active",
                emails=emails,
                raw_data={
                    "place_id": row.get("place_id") or None,
                    "cid": row.get("cid") or None,
                    "data_id": row.get("data_id") or None,
                    "rating": _float(row.get("review_rating")),
                    "review_count": _int(row.get("review_count")),
                    "maps_status": row.get("status") or None,
                    "plus_code": row.get("plus_code") or None,
                    "price_range": row.get("price_range") or None,
                    "timezone": row.get("timezone") or None,
                    "description": (row.get("descriptions") or "")[:500] or None,
                    "website_raw": website_raw,
                    "complete_address": addr or None,
                    "query": row.get("input_id") or params.get("keywords"),
                },
            )
        )
    return out


class GoogleMapsSource:
    key = "google_maps"
    name = "Google Maps (gosom scraper service)"
    quality = 0.9  # same Google Maps data as the Places API
    cost_class = "CHEAP"

    def __init__(self, *, poll_budget_s: float = 20.0, max_time_s: int = 240) -> None:
        self.poll_budget_s = poll_budget_s
        self.max_time_s = max_time_s
        self.sleep = asyncio.sleep  # injectable for tests

    def is_configured(self) -> bool:
        return bool(get_settings().gmaps_scraper_url)

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = profiles_for(defn)
        if not profiles:
            return 0.0
        if not target_countries(defn):
            return 0.0  # queries are "<term> <city>": a country (given or inferred) is required
        if profiles[0].local_business or defn.company_filters.cities:
            return 1.2  # businesses with their website, phone and address: the best source for a named city
        return 0.5 if not profiles[0].digital else 0.25

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        cf = defn.company_filters
        profiles = profiles_for(defn)[:2]
        countries = target_countries(defn)[:3]
        if not profiles or not countries:
            return []
        depth = 1 if expansion == 0 else 2
        queries: list[DiscoveryQuery] = []
        for cc in countries:
            lang = geo.country_language(cc)
            cities = geo.cities_for(cc, regions=cf.regions, cities=cf.cities, expansion=expansion)
            for prof in profiles:
                base = prof.maps(lang)[:2]
                alt = [q for q in [*prof.maps(lang)[2:], *prof.names(lang)] if q not in base][:2]
                variants = [("", base)] + ([(":alt", alt)] if expansion >= 2 and alt else [])
                for rank, city in enumerate(cities):
                    for suffix, terms in variants:
                        queries.append(
                            DiscoveryQuery(
                                key=f"maps:{prof.key}:{cc}:{city.name}{suffix}",
                                params={
                                    "keywords": [f"{t} {city.name}" for t in terms],
                                    "lang": lang if len(lang) == 2 else "en",
                                    "city": city.name,
                                    "country": cc,
                                    "depth": depth,
                                },
                                weight=round(1.0 / (1 + rank * 0.1), 4),
                            )
                        )
        return queries

    # ---- remote job lifecycle ---------------------------------------------------------------

    def _base(self) -> str:
        url = get_settings().gmaps_scraper_url
        if not url:
            raise FetchError("google_maps: GMAPS_SCRAPER_URL not configured")
        return url.rstrip("/")

    async def _create_job(self, query: DiscoveryQuery) -> str:
        p = query.params
        body = {
            "Name": f"scout {query.key}"[:120],
            "keywords": p["keywords"],
            "lang": p.get("lang", "en"),
            "zoom": 15,
            "lat": "",
            "lon": "",
            "fast_mode": False,
            "radius": 10000,
            "depth": max(1, int(p.get("depth", 1))),
            "email": False,
            "extra_reviews": False,
            "max_time": self.max_time_s,
            "proxies": [],
        }
        async with pool("maps"):
            resp = await http_request(
                "POST", f"{self._base()}/api/v1/jobs", source=self.key, json=body, timeout_s=30.0
            )
        await record_usage(UsageCategory.maps_request, source_key=self.key, resolver="discovery")
        job_id = (resp.json() or {}).get("id")
        if not job_id:
            raise FetchError("google_maps: job creation returned no id")
        log.info("google_maps.job_created", job_id=job_id, query=query.key)
        return str(job_id)

    async def _status(self, job_id: str) -> str | None:
        resp = await http_request(
            "GET", f"{self._base()}/api/v1/jobs/{job_id}", source=self.key, allow=(404,)
        )
        if resp.status_code == 404:
            return None
        data = resp.json() or {}
        return str(data.get("Status") or data.get("status") or "").lower()

    async def _poll(self, job_id: str) -> tuple[str | None, int]:
        """Poll with backoff within the budget. Returns (status, requests made)."""
        deadline = time.monotonic() + self.poll_budget_s
        delay, requests = 2.0, 0
        while True:
            status = await self._status(job_id)
            requests += 1
            if status not in STATUS_PENDING:
                return status, requests
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return status, requests
            await self.sleep(min(delay, remaining))
            delay = min(delay * 2, 15.0)

    async def _download(self, job_id: str) -> str:
        resp = await http_request(
            "GET",
            f"{self._base()}/api/v1/jobs/{job_id}/download",
            source=self.key,
            allow=(404,),
            timeout_s=60.0,
        )
        return "" if resp.status_code == 404 else resp.text

    async def _delete(self, job_id: str) -> None:
        with contextlib.suppress(Exception):  # best effort: the scraper's data folder is ephemeral anyway
            await http_request(
                "DELETE", f"{self._base()}/api/v1/jobs/{job_id}", source=self.key, allow=(404,)
            )

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        cur = dict(cursor or {})
        requests = 0
        created_now = not cur.get("job_id")
        if created_now:
            cur = {"job_id": await self._create_job(query), "created_at": time.time(), "polls": 0}
            requests += 1
        job_id = cur["job_id"]
        status, n = await self._poll(job_id)
        requests += n
        if status is None and created_now:
            status = "pending"  # not visible yet: treat as queued
        if status is None:  # job vanished (scraper restarted / deleted after a failure): start over
            log.warning("google_maps.job_missing", job_id=job_id, query=query.key)
            cur = {"job_id": await self._create_job(query), "created_at": time.time(), "polls": 0}
            return DiscoveryPage([], next_cursor=cur, requests=requests + 1)
        if status in STATUS_PENDING:
            age = time.time() - float(cur.get("created_at") or time.time())
            if age > 2 * self.max_time_s + 300:
                await self._delete(job_id)
                raise FetchError(f"google_maps: job {job_id} stuck in '{status}' for {int(age)} s")
            cur["polls"] = int(cur.get("polls", 0)) + 1
            cur["poll_after_s"] = 30
            return DiscoveryPage([], next_cursor=cur, requests=requests)
        if status != "ok":
            await self._delete(job_id)
            raise FetchError(f"google_maps: job {job_id} ended with status '{status}'")
        text = await self._download(job_id)
        candidates = parse_csv(text, query=query)
        await self._delete(job_id)
        log.info("google_maps.job_done", job_id=job_id, query=query.key, n=len(candidates))
        return DiscoveryPage(candidates=candidates, next_cursor=None, requests=requests + 2)
