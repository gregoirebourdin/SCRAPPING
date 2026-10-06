"""Grounded web research (Gemini + Google Search) as a discovery source of last resort for hard ICPs.

Each query is one segmented research question (industry × city/region). Every returned company must carry a
website whose registrable domain is a real company domain; grounding sources and search queries are kept in
``raw_data`` as provenance. Skipped entirely when the workspace is near its budget.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

from scout.ai.factory import get_ai
from scout.ai.prompts import UNTRUSTED_CONTENT_RULES
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    candidate_domain,
    employee_bounds,
    is_digital_icp,
    is_local_icp,
    profiles_for,
    target_countries,
)
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import allow_expensive
from scout.util.text import collapse_ws
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)


class GroundedCompany(BaseModel):
    name: str
    website: str | None = None
    city: str | None = None
    evidence: str | None = Field(default=None, description="Short quote or fact from a cited source")


class GroundedCompanies(BaseModel):
    companies: list[GroundedCompany] = Field(default_factory=list)


INSTRUCTIONS = (
    "You are a B2B research assistant building a list of real, currently operating companies.\n"
    "Use Google Search. Only list companies that appear in the search results you actually consulted, with their "
    "official website (their own domain, never a directory, marketplace, social network or news article). "
    "Skip any company whose website you cannot find. Never invent companies, websites or facts. "
    "Return up to 20 companies.\n\n" + UNTRUSTED_CONTENT_RULES
)


def _plural(phrase: str) -> str:
    """'marketing agency' → 'marketing agencies', 'dentist' → 'dentists'."""
    if phrase.endswith("y") and phrase[-2:-1] not in "aeiou":
        return phrase[:-1] + "ies"
    if phrase.endswith(("s", "x", "ch", "sh")):
        return phrase + "es"
    return phrase + "s"


def _size_phrase(bounds: tuple[int | None, int | None]) -> str:
    lo, hi = bounds
    if lo is not None and hi is not None:
        return f" with {lo} to {hi} employees"
    if hi is not None:
        return f" with fewer than {hi + 1} employees"
    if lo is not None:
        return f" with at least {lo} employees"
    return ""


class GeminiSearchSource:
    key = "gemini_search"
    name = "Grounded web research (Gemini + Google Search)"
    quality = 0.7
    cost_class = "WEB_SEARCH"

    def is_configured(self) -> bool:
        try:
            return bool(get_ai().available)
        except Exception:
            return False

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = profiles_for(defn)
        if not profiles and not defn.company_filters.keywords:
            return 0.0
        if is_digital_icp(profiles):
            return 0.8
        return 0.4 if is_local_icp(profiles) else 0.6

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        cf = defn.company_filters
        profiles = profiles_for(defn)[:2]
        subjects = [
            _plural(p.label if len(p.label) > 4 else next((n for n in p.names("en") if " " in n), p.label))
            for p in profiles
        ]
        if not subjects:
            subjects = list(cf.keywords[:2])
        if not subjects:
            return []
        size = _size_phrase(employee_bounds(defn))
        areas: list[str] = []
        for cc in target_countries(defn)[:2]:
            country = geo.country_name(cc)
            if cf.cities or cf.regions or expansion > 0:
                cities = geo.cities_for(cc, regions=cf.regions, cities=cf.cities, expansion=expansion)
                areas.extend(f"{c.name}, {country}" for c in cities[: 3 + 4 * expansion])
            else:
                areas.extend(f"{c.name}, {country}" for c in geo.cities_for(cc)[:3])
                areas.append(country)
        if not areas:
            areas = cf.cities[:3] or cf.regions[:3] or [""]
        queries: list[DiscoveryQuery] = []
        for subject in subjects:
            for rank, area in enumerate(dict.fromkeys(areas)):
                where = f" in {area}" if area else ""
                question = f"Which {subject}{where}{size} are there? List their names and official websites."
                queries.append(
                    DiscoveryQuery(
                        key=f"gs:{subject.lower()}|{area.lower()}|{size.strip()}",
                        params={"question": question, "subject": subject, "area": area},
                        weight=round(1.0 / (1 + 0.15 * rank), 4),
                    )
                )
        return queries

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        if not await allow_expensive():
            log.info("gemini_search.skipped_budget", query=query.key)
            return DiscoveryPage(candidates=[], next_cursor=None, requests=0)
        res = await get_ai().grounded_search(
            query=query.params["question"], instructions=INSTRUCTIONS, schema=GroundedCompanies
        )
        sources = [{"uri": s.uri, "title": s.title, "domain": s.domain} for s in res.sources]
        candidates: list[RawCandidate] = []
        seen: set[str] = set()
        companies = res.value.companies if res.value else []
        for c in companies:
            name = collapse_ws(c.name or "")
            dom = candidate_domain(c.website)
            if not name or not dom or dom in seen:
                continue  # no verifiable company website → not a candidate
            seen.add(dom)
            location = {"city": c.city} if c.city else {}
            candidates.append(
                RawCandidate(
                    source=self.key,
                    source_entity_id=dom,
                    name=name,
                    website=normalize_website(c.website),
                    domain=dom,
                    location=location,
                    source_url=next(
                        (s["uri"] for s in sources if s.get("domain") and dom in str(s["domain"])), None
                    ),
                    raw_data={
                        "evidence": c.evidence,
                        "question": query.params["question"],
                        "grounding_sources": sources[:20],
                        "search_queries": list(res.search_queries),
                        "grounded": bool(sources),
                    },
                )
            )
        log.info("gemini_search.page", query=query.key, returned=len(companies), kept=len(candidates))
        return DiscoveryPage(candidates=candidates, next_cursor=None)
