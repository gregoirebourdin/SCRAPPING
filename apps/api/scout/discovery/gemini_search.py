"""Grounded web research (Gemini + Google Search) as a discovery source of last resort for hard ICPs.

Each query is one segmented research question (industry × city/region). Every returned company must carry a
website whose registrable domain is a real company domain; grounding sources and search queries are kept in
``raw_data`` as provenance. Skipped entirely when the workspace is near its budget.

Gemini is the fallback, not the default search engine (``GEMINI_SEARCH_FALLBACK_ONLY``, default on): before
asking Gemini, the segment is searched for free through the lookup chain (self-hosted SearXNG). When that
already yields ≥ ``SEARCH_MIN_COMPANIES`` distinct company domains, the segment is left to the ``web_search``
source (same free results, honest ``search_snippet`` provenance) and no grounded call is made. Without a
lookup provider configured, or when ``web_search`` is excluded from the campaign, Gemini runs as before.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from scout.ai.factory import get_ai
from scout.ai.prompts import UNTRUSTED_CONTENT_RULES
from scout.config import get_settings
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.catalog import canonical_source_key
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
        excluded = {canonical_source_key(k) for k in defn.sources.excluded}
        serp_first = "web_search" not in excluded  # the web_search source covers segments free search answers
        # (area label for Gemini, place for the free-search pass, its country)
        areas: list[tuple[str, str, str | None]] = []
        for cc in target_countries(defn)[:2]:
            country = geo.country_name(cc)
            local_country = geo.country_name(cc, "fr" if geo.country_language(cc) == "fr" else "en")
            if cf.cities or cf.regions or expansion > 0:
                cities = geo.cities_for(cc, regions=cf.regions, cities=cf.cities, expansion=expansion)
                areas.extend((f"{c.name}, {country}", c.name, cc) for c in cities[: 3 + 4 * expansion])
            else:
                areas.extend((f"{c.name}, {country}", c.name, cc) for c in geo.cities_for(cc)[:3])
                areas.append((country, local_country, cc))
        if not areas:
            areas = [(a, a, None) for a in (cf.cities[:3] or cf.regions[:3] or [""])]
        unique: dict[str, tuple[str, str | None]] = {}
        for area, place, area_cc in areas:
            unique.setdefault(area, (place, area_cc))
        queries: list[DiscoveryQuery] = []
        for i, subject in enumerate(subjects):
            profile = profiles[i] if i < len(profiles) else None
            for rank, (area, (place, area_cc)) in enumerate(unique.items()):
                where = f" in {area}" if area else ""
                question = f"Which {subject}{where}{size} are there? List their names and official websites."
                lang = geo.country_language(area_cc) if area_cc else "en"
                # Free-search pass: the industry in the target language, like the web_search source queries.
                term = next(iter(profile.keywords(lang)), subject) if profile is not None else subject
                queries.append(
                    DiscoveryQuery(
                        key=f"gs:{subject.lower()}|{area.lower()}|{size.strip()}",
                        params={
                            "question": question,
                            "subject": subject,
                            "area": area,
                            "serp_q": f"{term} {place}".strip(),
                            "serp_region": area_cc,
                            "serp_lang": lang,
                            "serp_first": serp_first,
                        },
                        weight=round(1.0 / (1 + 0.15 * rank), 4),
                    )
                )
        return queries

    async def _free_search_suffices(self, query: DiscoveryQuery) -> bool:
        """True when the free lookup chain already finds enough company domains for this segment."""
        from scout.search.assess import assess_companies
        from scout.search.chain import gemini_fallback_only, lookup_chain

        params = query.params
        if not gemini_fallback_only() or not params.get("serp_first", True):
            return False
        chain = lookup_chain("gemini_precheck")
        if not chain.available():
            return False
        q = params.get("serp_q") or f"{params.get('subject', '')} {params.get('area', '')}".strip()
        if not q:
            return False
        min_companies = max(1, int(get_settings().search_min_companies))
        res = await chain.search(
            q,
            num=30,
            lang=params.get("serp_lang"),
            region=params.get("serp_region"),
            assess=lambda rs: assess_companies(rs, min_companies=min_companies),
        )
        log.info(
            "gemini_search.free_search_pass",
            query=query.key,
            provider=res.provider,
            sufficient=res.sufficient,
            reason=res.assessment.reason,
        )
        return res.sufficient

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        if not await allow_expensive():
            log.info("gemini_search.skipped_budget", query=query.key)
            return DiscoveryPage(candidates=[], next_cursor=None, requests=0)
        if await self._free_search_suffices(query):
            log.info("gemini_search.skipped_free_search_sufficient", query=query.key)
            return DiscoveryPage(candidates=[], next_cursor=None, requests=1)
        started = time.monotonic()
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
        from scout.search.telemetry import record_gemini

        await record_gemini(
            produced=bool(candidates),
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_usd=res.usage.cost_usd,
        )
        return DiscoveryPage(candidates=candidates, next_cursor=None)
