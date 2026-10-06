"""Free web-search discovery via the DuckDuckGo HTML endpoint (no key).

One candidate per registrable company domain; social networks, directories, media and gov/edu hosts are
dropped. "Top 10 / meilleures agences" pages are not companies: at expansion ≥ 1 they are expanded into their
outbound company links (``scout.discovery.listicle``). Anomaly / bot-challenge pages raise ``BlockedError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog
from rapidfuzz import fuzz
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    Throttle,
    candidate_domain,
    http_request,
    is_digital_icp,
    is_local_icp,
    profiles_for,
    target_countries,
    website_terms,
)
from scout.errors import BlockedError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.text import collapse_ws, normalize_key
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

_throttle = Throttle(2.0)  # be gentle with the free endpoint

_ANOMALY_MARKERS = (
    "anomaly-modal",
    "anomaly_modal",
    "challenge-form",
    "bots use DuckDuckGo too",
    "/anomaly.js",
)
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
LISTICLE_RE = re.compile(
    r"(\btop\s*\d+|\b\d+\s+(meilleur|best|top|agences|agencies|entreprises|companies|startups|soci[ée]t[ée]s|firms|cabinets)"
    r"|\bmeilleur(e|es|s)?\b|\bbest\b|\bclassement\b|\bpalmar[eè]s\b|\branking\b|\bliste des\b|\blist of\b|\bannuaire\b"
    r"|\bdirectory\b|\bcomparatif\b|\bbeste[nr]?\b|\bmejores\b|\bmigliori\b)",
    re.I,
)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    rank: int


def _unwrap(href: str | None) -> str | None:
    """DDG redirect links (//duckduckgo.com/l/?uddg=<url>) → target URL; ad links (y.js) → None."""
    if not href:
        return None
    h = href.strip()
    if h.startswith("//"):
        h = "https:" + h
    parts = urlsplit(h)
    host = (parts.hostname or "").lower()
    if (not host or host.endswith("duckduckgo.com")) and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [None])[0]
        return target
    if host.endswith("duckduckgo.com"):
        return None  # ads (/y.js) and internal links
    return h if parts.scheme in ("http", "https") else None


def is_blocked_page(html: str) -> bool:
    return any(m in html for m in _ANOMALY_MARKERS)


def parse_results(html: str) -> tuple[list[SearchResult], dict[str, str] | None]:
    """Organic results (ads skipped) and the hidden inputs of the "next page" form, if any."""
    tree = HTMLParser(html)
    results: list[SearchResult] = []
    for node in tree.css("div.result"):
        classes = node.attributes.get("class") or ""
        if "result--ad" in classes:
            continue
        a = node.css_first("a.result__a")
        if a is None:
            continue
        url = _unwrap(a.attributes.get("href"))
        if not url:
            continue
        snip = node.css_first(".result__snippet")
        results.append(
            SearchResult(
                url=url,
                title=collapse_ws(a.text(separator=" ")),
                snippet=collapse_ws(snip.text(separator=" ")) if snip else "",
                rank=len(results) + 1,
            )
        )
    next_form: dict[str, str] | None = None
    best_s = -1
    for form in tree.css("div.nav-link form"):
        fields = {
            (i.attributes.get("name") or ""): (i.attributes.get("value") or "")
            for i in form.css("input[type=hidden]")
            if i.attributes.get("name")
        }
        try:
            s = int(fields.get("s", "-1"))
        except ValueError:
            continue
        if s > best_s:
            best_s, next_form = s, fields
    return results, next_form


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


def is_listicle(result: SearchResult) -> bool:
    return bool(LISTICLE_RE.search(result.title)) or bool(
        re.search(r"/(top-\d+|meilleur|best-|classement|ranking|liste-)", urlsplit(result.url).path, re.I)
    )


class WebSearchSource:
    key = "web_search"
    name = "Web search (DuckDuckGo HTML)"
    quality = 0.6
    cost_class = "FREE"

    def __init__(self, *, throttle: Throttle | None = None) -> None:
        self.throttle = throttle or _throttle

    def is_configured(self) -> bool:
        return bool(get_settings().ddg_html_url)

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

        def add(q: str, kl: str, weight: float) -> None:
            key = f"ddg:{kl}:{q.lower()}"
            if all(x.key != key for x in queries):
                queries.append(
                    DiscoveryQuery(
                        key=key,
                        params={"q": q, "kl": kl, "max_pages": max_pages, "expand_listicles": expansion >= 1},
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
                        add(f"{kw} {city.name}", kl, 1.0 / (1 + 0.1 * rank))
                        for term in refinements:
                            add(f"{kw} {city.name} {term}", kl, 0.9 / (1 + 0.1 * rank))
                else:
                    where = geo.country_name(cc, "fr" if lang == "fr" else "en") if cc else ""
                    add(f"{kw} {where}".strip(), kl, 1.0)
                    for term in refinements:
                        add(f"{kw} {where} {term}".strip(), kl, 0.9)
        return queries

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        cur = dict(cursor or {})
        page = int(cur.get("page", 1))
        form = cur.get("form") or {"q": query.params["q"], "kl": query.params.get("kl", "wt-wt")}
        async with pool("search"):
            await self.throttle.wait()
            resp = await http_request(
                "POST",
                get_settings().ddg_html_url,
                source=self.key,
                data=form,
                headers={"Referer": "https://html.duckduckgo.com/", "Accept": "text/html"},
            )
        await record_usage(UsageCategory.web_search, source_key=self.key, resolver="discovery")
        html = resp.text
        if resp.status_code == 202 or is_blocked_page(html):
            raise BlockedError(f"{self.key}: anomaly / bot challenge page")
        results, next_form = parse_results(html)
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
                        "rank": r.rank,
                        "page": page,
                        "query": query.params["q"],
                        "kl": form.get("kl"),
                    },
                )
            )
        requests = 1
        if query.params.get("expand_listicles") and listicles:
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
        max_pages = int(query.params.get("max_pages", 2))
        has_next = bool(next_form) and bool(results) and page < max_pages
        next_cursor = {"form": next_form, "page": page + 1, "seen": seen_order[-300:]} if has_next else None
        return DiscoveryPage(candidates=candidates, next_cursor=next_cursor, requests=requests)
