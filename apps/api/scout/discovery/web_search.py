"""Free web-search discovery through the search chain (``scout.search``): SearXNG first when configured, the
DuckDuckGo HTML endpoint as fallback (or alone). No key, no per-query cost.

One candidate per registrable company domain; social networks, directories, media and gov/edu hosts are
dropped. "Top 10 / meilleures agences" pages are not companies: at expansion ≥ 1 they are expanded into their
outbound company links (``scout.discovery.listicle``). When every provider fails, the most relevant typed error
is raised (anti-bot / anomaly page → ``BlockedError``) so the discovery job backs off and source health counts it.

Cursor: ``{"engine": provider, "form": provider state (DDG next-page form / SearXNG {"pageno"}), "page", "seen"}``;
cursors stored before the chain existed (``form`` only) continue on DuckDuckGo.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from rapidfuzz import fuzz

from scout.config import get_settings
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    Throttle,
    candidate_domain,
    is_digital_icp,
    is_local_icp,
    profiles_for,
    target_countries,
    website_terms,
)
from scout.schemas.campaign import CampaignDefinition
from scout.search.assess import LISTICLE_RE, assess_companies, is_listicle
from scout.search.chain import SearchChain, discovery_chain
from scout.search.duckduckgo import DEFAULT_THROTTLE, is_blocked_page, parse_results, region_from_kl
from scout.search.types import SearchResult
from scout.util.text import normalize_key
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

__all__ = [
    "LISTICLE_RE",
    "SearchResult",
    "WebSearchSource",
    "company_name_from_title",
    "is_blocked_page",
    "is_listicle",
    "parse_results",
]

_SEPARATORS = re.compile(r"\s+[|\-–—:·•»]\s+")
_GENERIC_SEGMENTS = {
    "accueil",
    "home",
    "homepage",
    "site officiel",
    "official site",
    "official website",
    "welcome",
    "bienvenue",
    "startseite",
    "inicio",
    "home page",
}


def company_name_from_title(title: str, domain: str | None) -> str:
    """'Agence Web Lyon - Pixel Studio' + pixelstudio.fr → 'Pixel Studio'."""
    segments = [s.strip() for s in _SEPARATORS.split(title or "") if s.strip()]
    segments = [s for s in segments if normalize_key(s) not in _GENERIC_SEGMENTS] or segments
    if not segments:
        return (domain or "").split(".")[0].capitalize()
    if domain and len(segments) > 1:
        label = domain.split(".")[0].replace("-", "")
        scored = [
            (fuzz.partial_ratio(normalize_key(s).replace(" ", ""), label), -i, s)
            for i, s in enumerate(segments)
        ]
        best = max(scored)
        if best[0] >= 70:
            return best[2]
    return segments[0]


class WebSearchSource:
    key = "web_search"
    name = "Web search (SearXNG / DuckDuckGo)"
    quality = 0.6
    cost_class = "FREE"

    def __init__(self, *, throttle: Throttle | None = None, chain: SearchChain | None = None) -> None:
        self.throttle = throttle or DEFAULT_THROTTLE
        self._chain = chain

    def chain(self) -> SearchChain:
        return self._chain or discovery_chain(ddg_throttle=self.throttle)

    def is_configured(self) -> bool:
        return self.chain().configured

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = profiles_for(defn)
        if not profiles and not defn.company_filters.keywords:
            return 0.0
        if is_digital_icp(profiles):
            return 0.8
        return 0.5 if is_local_icp(profiles) else 0.6

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        cf = defn.company_filters
        profiles = profiles_for(defn)[:2]
        countries: list[str | None] = list(target_countries(defn)[:2]) or [None]
        refinements = website_terms(defn, limit=1)
        max_pages = 2 if expansion == 0 else 4
        queries: list[DiscoveryQuery] = []

        def add(q: str, kl: str, cc: str | None, lang: str, weight: float) -> None:
            key = f"ddg:{kl}:{q.lower()}"  # stable keys: plans stored before the search chain stay valid
            if all(x.key != key for x in queries):
                queries.append(
                    DiscoveryQuery(
                        key=key,
                        params={
                            "q": q,
                            "kl": kl,
                            "region": cc,
                            "lang": lang,
                            "max_pages": max_pages,
                            "expand_listicles": expansion >= 1,
                        },
                        weight=weight,
                    )
                )

        for cc in countries:
            lang = geo.country_language(cc) if cc else "en"
            kl = geo.ddg_region(cc)
            terms = [kw for prof in profiles for kw in prof.keywords(lang)[:2]] or list(cf.keywords[:3])
            cities = (
                geo.cities_for(cc, regions=cf.regions, cities=cf.cities, expansion=expansion)[
                    : 8 + 12 * expansion
                ]
                if cc
                else []
            )
            for kw in dict.fromkeys(terms):
                if cities:
                    for rank, city in enumerate(cities):
                        add(f"{kw} {city.name}", kl, cc, lang, 1.0 / (1 + 0.1 * rank))
                        for term in refinements:
                            add(f"{kw} {city.name} {term}", kl, cc, lang, 0.9 / (1 + 0.1 * rank))
                else:
                    where = geo.country_name(cc, "fr" if lang == "fr" else "en") if cc else ""
                    add(f"{kw} {where}".strip(), kl, cc, lang, 1.0)
                    for term in refinements:
                        add(f"{kw} {where} {term}".strip(), kl, cc, lang, 0.9)
        return queries

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        cur = dict(cursor or {})
        page = int(cur.get("page", 1))
        params = query.params
        region = params.get("region") if "region" in params else region_from_kl(params.get("kl"))
        lang = params.get("lang") or (geo.country_language(region) if region else None)
        state = cur.get("form") or None
        engine = cur.get("engine") or ("duckduckgo" if state else None)
        min_companies = max(1, int(get_settings().search_min_companies))
        res = await self.chain().search_page(
            params["q"],
            lang=lang,
            region=region,
            page=page,
            state=state,
            provider=engine,
            assess=lambda rs: assess_companies(rs, min_companies=min_companies),
        )
        res.raise_if_failed()
        results = res.results
        seen_order: list[str] = list(cur.get("seen") or [])
        seen: set[str] = set(seen_order)
        candidates: list[RawCandidate] = []
        listicles: list[str] = []
        for r in results:
            if is_listicle(r):
                listicles.append(r.url)
                continue
            dom = candidate_domain(r.url)
            if not dom or dom in seen:
                continue
            seen.add(dom)
            seen_order.append(dom)
            candidates.append(
                RawCandidate(
                    source=self.key,
                    source_entity_id=dom,
                    name=company_name_from_title(r.title, dom),
                    website=normalize_website(r.url),
                    domain=dom,
                    source_url=r.url,
                    raw_data={
                        "title": r.title,
                        "snippet": r.snippet,
                        "rank": r.position,
                        "page": page,
                        "query": params["q"],
                        "kl": params.get("kl"),
                        "engine": res.provider,
                        "engines": list(r.engines),
                    },
                )
            )
        requests = 0 if res.from_cache else sum(1 for a in res.attempts if a.skipped is None)
        if params.get("expand_listicles") and listicles:
            from scout.discovery.listicle import expand_listicle

            for url in listicles[:2]:
                requests += 1
                try:
                    extra = await expand_listicle(url)
                except Exception as exc:  # a broken listicle must not fail the search page
                    log.info("web_search.listicle_failed", url=url, error=str(exc)[:200])
                    continue
                for c in extra:
                    if c.domain and c.domain not in seen:
                        seen.add(c.domain)
                        seen_order.append(c.domain)
                        candidates.append(c)
        max_pages = int(params.get("max_pages", 2))
        has_next = bool(res.next_state) and bool(results) and page < max_pages
        next_cursor = (
            {"engine": res.provider, "form": res.next_state, "page": page + 1, "seen": seen_order[-300:]}
            if has_next
            else None
        )
        return DiscoveryPage(candidates=candidates, next_cursor=next_cursor, requests=max(1, requests))
