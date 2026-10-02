"""Site crawler: fetch a site's most informative pages (home, about, case studies, clients, contact...).

Strategy
--------
1. Fetch the homepage (static).  If the text is thin and the browser is available, render it.
2. Read ``sitemap.xml`` (and nested sitemaps) for a cheap full URL inventory + ``lastmod`` freshness signal.
3. Score every internal URL by *kind* (case-study > clients > about > contact > services > team > blog) using
   path + anchor text, and fetch the best ``max_pages``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Iterable

from ..config import settings
from ..util.text import ParsedPage, parse_html
from ..util.urls import absolutize, canonicalize, ensure_scheme, looks_like_asset, registrable_domain, same_site
from .browser import get_browser
from .http import FetchResult, Fetcher

log = logging.getLogger(__name__)

# kind → (priority, path regex, anchor regex)
PAGE_KINDS: list[tuple[str, int, str, str]] = [
    ("case_studies", 1, r"(case[-_]?stud|success[-_]?stor|client[-_]?(stor|result|win|success)|results|wins|proof|testimonial|review|portfolio|our[-_]?work|/work\b|projects?)", r"(case stud|success stor|results|testimonial|client (stor|result|win|success)|portfolio|our work|proof|reviews)"),
    ("clients", 1, r"(/clients?|/customers?|/partners?|/brands)", r"(^clients?$|our clients|who we work with|clients we|customers)"),
    ("about", 2, r"(/about|/our[-_]?story|/who[-_]?we[-_]?are|/team|/company|/mission|/meet)", r"(^about|about us|our story|who we are|meet the team|our team|the team)"),
    ("contact", 2, r"(/contact|/get[-_]?in[-_]?touch|/reach|/book|/apply|/schedule|/call|/consult|/start|/get[-_]?started|/work[-_]?with)", r"(contact|get in touch|book a call|apply|schedule|let.s talk|work with us|get started|free consult|strategy session)"),
    ("services", 3, r"(/services?|/what[-_]?we[-_]?do|/solutions?|/offers?|/ads|/funnels?|/marketing|/paid|/media|/growth|/pricing|/packages|/how[-_]?it[-_]?works)", r"(services|what we do|solutions|how it works|pricing|packages|our process)"),
    ("industries", 2, r"(/industr|/niche|/coach|/course|/creator|/expert|/consultant|/info|/who[-_]?we[-_]?(help|serve))", r"(industries|niches|for coaches|for course creators|who we (help|serve)|coaches|course creators|experts)"),
    ("blog", 5, r"(/blog|/news|/insights|/articles|/resources|/podcast)", r"(^blog$|news|insights|articles|resources|podcast)"),
]
_KIND_RES = [(k, p, re.compile(pr, re.I), re.compile(ar, re.I)) for k, p, pr, ar in PAGE_KINDS]

SKIP_PATH_RE = re.compile(
    r"(/wp-admin|/wp-login|/cart|/checkout|/account|/login|/signin|/sign-in|/register|/privacy|/terms|/cookie|/legal|/disclaimer|"
    r"/feed|/rss|/tag/|/tags/|/category/|/categories/|/author/|/page/\d|/search|/sitemap|/\d{4}/\d{2}/|/thank[-_]?you|/confirm|/unsubscribe|"
    r"/download|/attachment|/wp-json|/xmlrpc|/cdn-cgi|\.(php|asp|aspx|jsp)\?|/#)",
    re.I,
)

SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
SITEMAP_URL_RE = re.compile(r"<url>(.*?)</url>", re.I | re.S)
LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)


@dataclass
class CrawledPage:
    url: str
    kind: str
    parsed: ParsedPage
    status: int | None = None
    rendered: bool = False
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.parsed.text


@dataclass
class CrawledSite:
    domain: str
    home_url: str
    final_url: str = ""
    pages: list[CrawledPage] = field(default_factory=list)
    alive: bool = True
    alive_details: dict = field(default_factory=dict)
    sitemap_urls: list[str] = field(default_factory=list)
    sitemap_lastmod: str | None = None
    fetch_errors: int = 0
    redirected_domain: str | None = None  # when the site redirects to another registrable domain

    @property
    def home(self) -> CrawledPage | None:
        return next((p for p in self.pages if p.kind == "home"), None)

    def pages_of(self, *kinds: str) -> list[CrawledPage]:
        return [p for p in self.pages if p.kind in kinds]

    @property
    def all_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def all_html(self) -> str:
        return "\n".join(p.parsed.html for p in self.pages)


def classify_url(url: str, anchor: str = "") -> tuple[str, int]:
    """Return (kind, priority) for an internal URL.  Lower priority is better."""
    path = url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
    path = "/" + path
    best: tuple[str, int] | None = None
    for kind, prio, pre, are in _KIND_RES:
        if pre.search(path) or (anchor and are.search(anchor)):
            if best is None or prio < best[1]:
                best = (kind, prio)
    if best:
        return best
    depth = path.count("/")
    return ("other", 4 if depth <= 1 else 6)


class SiteCrawler:
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.browser = get_browser()

    async def _get_page(self, url: str, *, allow_render: bool = True) -> tuple[FetchResult, ParsedPage, bool]:
        res = await self.fetcher.fetch(url)
        parsed = parse_html(res.html, res.final_url or url) if res.html else ParsedPage(url=url)
        rendered = res.rendered
        thin = res.ok and parsed.text_len < settings.min_text_chars_for_static
        js_shell = res.ok and ("__NEXT_DATA__" in res.html or "ng-app" in res.html or 'id="root"' in res.html or 'id="app"' in res.html) and parsed.text_len < 1500
        if allow_render and self.browser.available and (thin or js_shell) and not res.rendered:
            html, final_url, status = await self.browser.render(res.final_url or url)
            if html:
                p2 = parse_html(html, final_url)
                if p2.text_len > parsed.text_len:
                    res = FetchResult(url=url, final_url=final_url, status=status or res.status, html=html, content_type="text/html", rendered=True)
                    await self.fetcher.cache_put(canonicalize(url), res)
                    parsed = p2
                    rendered = True
        return res, parsed, rendered

    async def _sitemap(self, base: str) -> tuple[list[str], str | None]:
        urls: list[str] = []
        lastmods: list[str] = []
        seen: set[str] = set()
        queue = [base.rstrip("/") + "/sitemap.xml", base.rstrip("/") + "/sitemap_index.xml"]
        depth = 0
        while queue and depth < 12:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            depth += 1
            res = await self.fetcher.fetch(sm, ttl_hours=settings.page_cache_ttl_hours, retries=0)
            if not res.ok or "<urlset" not in res.html and "<sitemapindex" not in res.html:
                continue
            if "<sitemapindex" in res.html:
                for loc in SITEMAP_LOC_RE.findall(res.html)[:15]:
                    if not re.search(r"(image|video|tag|category|author|attachment)", loc, re.I):
                        queue.append(loc.strip())
                continue
            for block in SITEMAP_URL_RE.findall(res.html):
                loc = SITEMAP_LOC_RE.search(block)
                if not loc:
                    continue
                u = loc.group(1).strip()
                if same_site(u, base) and not looks_like_asset(u):
                    urls.append(u)
                lm = LASTMOD_RE.search(block)
                if lm:
                    lastmods.append(lm.group(1).strip()[:10])
            if len(urls) > 3000:
                break
        return urls, (max(lastmods) if lastmods else None)

    def _rank_links(self, site: CrawledSite, links: Iterable[tuple[str, str]], home_url: str) -> list[tuple[int, str, str]]:
        ranked: dict[str, tuple[int, str]] = {}
        home_dom = registrable_domain(home_url)
        for href, anchor in links:
            url = absolutize(home_url, href)
            if not url or looks_like_asset(url) or SKIP_PATH_RE.search(url):
                continue
            if registrable_domain(url) != home_dom:
                continue
            url = canonicalize(url)
            if url == canonicalize(home_url):
                continue
            kind, prio = classify_url(url, anchor)
            cur = ranked.get(url)
            if cur is None or prio < cur[0]:
                ranked[url] = (prio, kind)
        return sorted(((p, k, u) for u, (p, k) in ranked.items()), key=lambda t: (t[0], len(t[2])))

    async def crawl(self, url: str, *, max_pages: int | None = None, allow_render: bool = True) -> CrawledSite:
        url = ensure_scheme(url)
        domain = registrable_domain(url)
        site = CrawledSite(domain=domain, home_url=url)
        max_pages = max_pages or settings.crawl_max_pages

        res, parsed, rendered = await self._get_page(url, allow_render=allow_render)
        # retry with www / http variants when the bare host fails at the network level
        if res.error and not res.status:
            for alt in (url.replace("://", "://www.", 1) if "://www." not in url else url.replace("://www.", "://", 1), url.replace("https://", "http://", 1)):
                res, parsed, rendered = await self._get_page(alt, allow_render=allow_render)
                if res.ok:
                    break
        site.final_url = res.final_url or url
        site.alive_details = {
            "status": res.status, "error": res.error, "final_url": res.final_url, "text_len": parsed.text_len,
            "rendered": rendered, "server": res.headers.get("server"), "last_modified": res.headers.get("last-modified"),
        }
        if not res.ok:
            site.alive = False
            site.alive_details["reason"] = f"http_{res.status}" if res.status else (res.error or "unreachable")
            return site
        if res.final_url and registrable_domain(res.final_url) != domain:
            site.redirected_domain = registrable_domain(res.final_url)
        site.pages.append(CrawledPage(url=site.final_url, kind="home", parsed=parsed, status=res.status, rendered=rendered, from_cache=res.from_cache))

        base = site.final_url.split("?", 1)[0]
        base = base[: base.index("/", 8)] if base.count("/") > 2 else base
        sm_urls, lastmod = await self._sitemap(base)
        site.sitemap_urls = sm_urls
        site.sitemap_lastmod = lastmod

        # candidate internal links: homepage links + sitemap URLs (sitemap gives deep case-study pages)
        links: list[tuple[str, str]] = list(parsed.links)
        links.extend((u, "") for u in sm_urls[:1500])
        ranked = self._rank_links(site, links, site.final_url)

        # make sure every important kind gets a slot, then fill with the best remaining
        chosen: list[tuple[str, str]] = []
        per_kind_cap = {"case_studies": 6, "clients": 2, "about": 2, "contact": 2, "services": 2, "industries": 2, "blog": 1, "other": 2}
        counts: dict[str, int] = {}
        for prio, kind, u in ranked:
            if counts.get(kind, 0) >= per_kind_cap.get(kind, 1):
                continue
            chosen.append((kind, u))
            counts[kind] = counts.get(kind, 0) + 1
            if len(chosen) >= max_pages - 1:
                break

        sem = asyncio.Semaphore(settings.per_host_concurrency)

        async def _one(kind: str, u: str) -> CrawledPage | None:
            async with sem:
                r, p, rend = await self._get_page(u, allow_render=False)
            if r.ok and p.text_len > 80:
                return CrawledPage(url=r.final_url or u, kind=kind, parsed=p, status=r.status, rendered=rend, from_cache=r.from_cache)
            if r.error:
                site.fetch_errors += 1
            return None

        results = await asyncio.gather(*(_one(k, u) for k, u in chosen), return_exceptions=True)
        seen_text: set[int] = {hash(parsed.text[:2000])}
        for item in results:
            if isinstance(item, CrawledPage):
                h = hash(item.text[:2000])
                if h in seen_text:
                    continue
                seen_text.add(h)
                site.pages.append(item)
        return site


def newest_date(site: CrawledSite) -> str | None:
    """Best-effort 'last activity' ISO date from sitemap lastmod, meta dates and HTTP Last-Modified."""
    cands: list[str] = []
    if site.sitemap_lastmod:
        cands.append(site.sitemap_lastmod[:10])
    for p in site.pages:
        for d in p.parsed.dates:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", d)
            if m:
                cands.append(m.group(1))
    lm = site.alive_details.get("last_modified")
    if lm:
        try:
            cands.append(datetime.strptime(lm[:25].strip(), "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d"))
        except ValueError:
            pass
    cands = [c for c in cands if "2000" <= c[:4] <= str(datetime.utcnow().year + 1)]
    return max(cands) if cands else None
