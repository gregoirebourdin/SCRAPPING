"""Deterministic fixture source for tests / E2E (never in production).

Reads ``DISCOVERY_FIXTURE_MANIFEST``: ``{"companies": [{"name", "website", "city", "country", "category",
"employees"?, "registry_id"?, "people"?: [...]}, ...]}`` and serves pages of 10 candidates, loosely filtered by
country and industry. When configured, the router uses it alone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson
import structlog

from scout.config import get_settings
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import industry_texts, profiles_for, target_countries
from scout.discovery.employee_bands import parse_range
from scout.discovery.taxonomy import canonical, match_industries
from scout.errors import PermanentError
from scout.schemas.campaign import CampaignDefinition
from scout.util.text import normalize_key
from scout.util.urls import normalize_website, registrable_domain

log = structlog.get_logger(__name__)

PAGE_SIZE = 10
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def load_manifest(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        raise PermanentError(f"fixture manifest not found: {path}") from exc
    hit = _cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    data = orjson.loads(p.read_bytes())
    companies = data.get("companies") if isinstance(data, dict) else data
    if not isinstance(companies, list):
        raise PermanentError("fixture manifest: 'companies' must be a list")
    _cache[str(p)] = (mtime, companies)
    return companies


def _employees(v: Any) -> tuple[int | None, int | None]:
    if isinstance(v, dict):
        return (v.get("min"), v.get("max"))
    if isinstance(v, int):
        return (v, v)
    return parse_range(v) if v else (None, None)


def _matches(c: dict[str, Any], *, countries: list[str], profile_keys: set[str], terms: list[str]) -> bool:
    cc = geo.country_code(c.get("country")) if c.get("country") else None
    if countries and cc and cc not in countries:
        return False
    if not profile_keys and not terms:
        return True
    category = c.get("category") or ""
    if not category:
        return True
    if profile_keys and {p.key for p in match_industries(category)} & profile_keys:
        return True
    hay = canonical(" ".join([c.get("name") or "", category, c.get("description") or ""]))
    return any(t in hay for t in terms)


def to_candidate(c: dict[str, Any]) -> RawCandidate | None:
    name = (c.get("name") or "").strip()
    if not name:
        return None
    website = normalize_website(c.get("website")) if c.get("website") else None
    domain = registrable_domain(website) if website else None
    country = geo.country_code(c.get("country")) if c.get("country") else None
    registry_id = str(c["registry_id"]) if c.get("registry_id") else None
    registry_source = c.get("registry_source") or (
        "fr_sirene"
        if registry_id and country == "FR" and registry_id.isdigit() and len(registry_id) == 9
        else None
    )
    emp_min, emp_max = _employees(c.get("employees"))
    people = []
    for person in c.get("people") or []:
        if isinstance(person, str):
            person = {"full_name": person}
        if isinstance(person, dict) and person.get("full_name"):
            people.append({"source_url": website, **person})
    location = {
        "country": country,
        "city": c.get("city"),
        "postal_code": c.get("postal_code"),
        "address": c.get("address"),
        "region": c.get("region"),
    }
    known = {
        "name",
        "website",
        "city",
        "country",
        "category",
        "employees",
        "registry_id",
        "registry_source",
        "people",
        "postal_code",
        "address",
        "region",
        "phone",
        "emails",
        "status",
    }
    return RawCandidate(
        source="fixture",
        source_entity_id=registry_id or domain or normalize_key(name),
        name=name,
        website=website,
        domain=domain,
        location={k: v for k, v in location.items() if v},
        category=c.get("category"),
        source_url=website,
        phone=c.get("phone"),
        emails=list(c.get("emails") or []),
        employee_min=emp_min,
        employee_max=emp_max,
        registry_source=registry_source,
        registry_id=registry_id,
        status=c.get("status") or "active",
        people=people,
        raw_data={k: v for k, v in c.items() if k not in known},
    )


class FixtureSource:
    key = "fixture"
    name = "Fixture manifest (test / E2E)"
    quality = 0.9
    cost_class = "FREE"

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.discovery_fixture_manifest) and s.app_env != "production"

    def suitability(self, defn: CampaignDefinition) -> float:
        return 100.0 if self.is_configured() else 0.0

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        return [DiscoveryQuery(key="fixture:all", params=self._params(defn))]  # expansion: nothing broader

    def _params(self, defn: CampaignDefinition) -> dict[str, Any]:
        terms = [t for text in industry_texts(defn) for t in canonical(text).split() if len(t) > 3]
        return {
            "countries": target_countries(defn),
            "profiles": [p.key for p in profiles_for(defn)],
            "terms": list(dict.fromkeys(terms)),
        }

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        s = get_settings()
        if not self.is_configured() or not s.discovery_fixture_manifest:
            raise PermanentError("fixture source is not configured")
        path = s.discovery_fixture_manifest
        if not os.path.isabs(path):
            path = str(Path.cwd() / path)
        companies = load_manifest(path)
        p = query.params
        matched = [
            c
            for c in companies
            if _matches(
                c,
                countries=p.get("countries") or [],
                profile_keys=set(p.get("profiles") or []),
                terms=p.get("terms") or [],
            )
        ]
        offset = int((cursor or {}).get("offset", 0))
        window = matched[offset : offset + PAGE_SIZE]
        candidates = [x for x in (to_candidate(c) for c in window) if x]
        nxt = offset + PAGE_SIZE
        return DiscoveryPage(
            candidates=candidates, next_cursor={"offset": nxt} if nxt < len(matched) else None
        )
