"""Turn a client *name* into a client *website* and its funnel(s).

Precision over recall: a wrong website (a namesake, a news article, a dictionary page) is worse than none,
so every candidate site must (1) carry the full name prominently or echo it in its domain and (2) read like
a coach / course creator site.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..config import settings
from ..discovery.router import SearchRouter
from ..extract.funnels import DetectedFunnel, detect_funnels
from ..fetch.crawler import CrawledSite, SiteCrawler
from ..lexicon import COACH_CONTEXT_RE, COACH_ROLE_RE
from ..util.urls import ensure_scheme, is_blocked_domain, registrable_domain, social_network

log = logging.getLogger(__name__)

_OFFER_HINT = re.compile(r"\b(book a call|apply now|apply here|enroll|join (the|my|our)|work with me|my (program|course|coaching)|free (training|masterclass|guide|workshop)|students|clients|webinar|masterclass|mastermind|membership|coaching)\b", re.I)
_BAD_CLIENT_DOMAIN = re.compile(r"(dictionary|thesaurus|wiki|news|times|post|tribune|herald|journal|daily|gazette|\btv\b|video|tube|movies?|film|college|university|school\.|\.edu$|\.gov$|godaddy|wix\.com|squarespace\.com|amazon|ebay|etsy|imdb|spotify|apple\.com|google|yahoo|bing|baby|parenting|recipe|weather|sports|espn|nfl|nba|mlb|realtor|zillow|hospital|clinic|pharma|bank|insurance)", re.I)
_BAD_PATH = re.compile(r"/(video|videos|dictionary|news|article|articles|blog|wiki|movie|movies|watch|signup|login|product|products|p/|tag|category|search)\b", re.I)


@dataclass
class ResolvedClient:
    website: str | None = None
    website_source: str | None = None
    status: str = "no_site"  # resolved | no_site | not_infopreneur
    funnels: list[DetectedFunnel] = field(default_factory=list)
    site: CrawledSite | None = None
    note: str = ""


def _name_tokens(name: str) -> list[str]:
    name = re.sub(r"[’']s\b", "", name)
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 1 and t not in {"dr", "mr", "mrs", "ms", "the", "and", "of"}]


def _full_name_in(text: str, name: str) -> bool:
    toks = _name_tokens(name)
    if len(toks) < 2:
        return False
    pat = r"\b" + r"[\s.\-']{1,3}".join(re.escape(t) for t in toks) + r"\b"
    return re.search(pat, text, re.I) is not None


def _domain_echoes(name: str, domain: str) -> bool:
    toks = _name_tokens(name)
    core = domain.split(".")[0].replace("-", "")
    if not toks:
        return False
    joined = "".join(toks)
    if len(joined) >= 6 and joined in core:
        return True
    last = toks[-1]
    return len(toks) >= 2 and len(last) >= 5 and last in core and toks[0][:3] in core


def _looks_like_infopreneur_site(site: CrawledSite) -> bool:
    text = site.all_text[:120_000]
    ctx = {m.group(0).lower() for m in COACH_CONTEXT_RE.finditer(text)}
    return len(ctx) >= 2 and bool(_OFFER_HINT.search(text))


async def _verify_site(crawler: SiteCrawler, url: str, name: str, kind: str, strict: bool) -> tuple[CrawledSite | None, bool, str]:
    dom = registrable_domain(url)
    if _BAD_CLIENT_DOMAIN.search(dom):
        return None, False, "bad_domain"
    site = await crawler.crawl(url, max_pages=settings.crawl_max_client_pages, allow_render=True)
    if not site.alive or site.home is None:
        return None, False, "dead"
    home = site.home
    head = f"{home.parsed.title} {home.parsed.meta_description} {home.parsed.og_title} {' '.join(home.parsed.headings[:10])}"
    echoes = _domain_echoes(name, site.domain)
    if kind == "person":
        named = echoes or _full_name_in(head, name) or (_full_name_in(site.all_text[:40_000], name) and not strict)
    else:
        named = echoes or _full_name_in(head, name) or name.lower() in head.lower()
    if not named:
        return site, False, "name_not_found"
    if not _looks_like_infopreneur_site(site):
        return site, False, "not_infopreneur"
    return site, True, "ok"


async def resolve_client(
    name: str,
    kind: str,
    role_title: str | None,
    website: str | None,
    router: SearchRouter | None,
    crawler: SiteCrawler,
    *,
    confidence: float = 0.5,
) -> ResolvedClient:
    out = ResolvedClient()
    tried: set[str] = set()
    toks = _name_tokens(name)

    async def attempt(url: str, source: str, strict: bool) -> bool:
        dom = registrable_domain(url)
        if not dom or dom in tried or is_blocked_domain(dom) or social_network(url):
            return False
        tried.add(dom)
        site, ok, note = await _verify_site(crawler, url, name, kind, strict)
        if ok and site is not None:
            out.website, out.website_source, out.status, out.site = site.final_url or ensure_scheme(url), source, "resolved", site
            out.funnels = await detect_funnels(site, crawler)
            return True
        if note == "not_infopreneur" and site is not None and source == "outbound_link":
            out.website, out.website_source, out.status, out.site, out.note = site.final_url, source, "not_infopreneur", site, note
        return False

    # 1) the agency linked to them: trust the link, verify loosely
    if website and await attempt(website, "outbound_link", strict=False):
        return out
    if out.status == "not_infopreneur":
        return out
    # 2) search — only for real person names we are reasonably sure about
    if router is None or settings.client_resolve_max_search <= 0 or kind != "person" or not 2 <= len(toks) <= 3 or confidence < 0.5:
        return out
    role_hint = ""
    if role_title:
        m = COACH_ROLE_RE.search(role_title)
        role_hint = m.group(1) if m else ""
    clean = " ".join(t.capitalize() for t in toks)
    queries = [f'"{clean}" {role_hint or "coach"}'.strip()]
    if role_hint and role_hint.lower() != "coach":
        queries.append(f'"{clean}" coach OR course OR program OR mentor')
    for q in queries[: settings.client_resolve_max_search]:
        results = await router.search(q, country="us", pages=1, max_engines=1)
        ranked: list[tuple[int, str]] = []
        for r in results[:10]:
            dom = registrable_domain(r.url)
            if not dom or is_blocked_domain(dom) or social_network(r.url) or _BAD_CLIENT_DOMAIN.search(dom) or _BAD_PATH.search(r.url):
                continue
            score = 0
            if _domain_echoes(name, dom):
                score += 3
            if _full_name_in(r.title, name):
                score += 2
            if _OFFER_HINT.search(f"{r.title} {r.snippet}") or COACH_CONTEXT_RE.search(f"{r.title} {r.snippet}"):
                score += 1
            if score >= 3:
                ranked.append((score, f"https://{dom}/"))
        ranked.sort(key=lambda t: -t[0])
        for _, url in ranked[:2]:
            if await attempt(url, "search", strict=True):
                return out
    return out
