"""Site crawler: home → parked check → robots → sitemap → page selection → bounded fetch (+ JS tiers).

``crawl_site`` never raises for site-level problems: unreachable / blocked / parked sites are
reported through ``CrawlResult.status``. Per-page failures only increment ``pages_failed``.
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit, urlunsplit

import structlog

from scout.crawl import http, render, robots
from scout.crawl.page_selector import parse_sitemap_entries, pick_child_sitemaps, select_pages
from scout.crawl.parser import ParsedPage, parse_html
from scout.crawl.ssrf import SSRFBlocked, validate_url
from scout.crawl.types import CrawlResult, FetchedPage
from scout.db.enums import ErrorCategory, FetchTier, PageType, WebsiteStatus
from scout.errors import BlockedError, JobError, RateLimitedError
from scout.util.pools import domain_slot
from scout.util.text import content_hash
from scout.util.urls import NON_COMPANY_DOMAINS, canonical_url, registrable_domain

log = structlog.get_logger(__name__)

CRAWL_TIME_BUDGET_S = 60.0
HOME_TIME_BUDGET_S = 30.0  # robots.txt + home page variants
MAX_SITEMAP_FETCHES = 2
_TIER_ORDER = {FetchTier.http: 0, FetchTier.crawl4ai: 1, FetchTier.browser: 2}

KnownPages = dict[str, tuple[str | None, str | None, str]]

# ---- parked / for-sale domains ----------------------------------------------------------------

_PARKING_HOSTS = (
    "sedo.com",
    "sedoparking.com",
    "dan.com",
    "afternic.com",
    "hugedomains.com",
    "bodis.com",
    "parkingcrew.net",
    "above.com",
    "undeveloped.com",
    "domainmarket.com",
    "buydomains.com",
    "parklogic.com",
    "uniregistry.com",
    "sav.com",
    "efty.com",
    "atom.com",
    "squadhelp.com",
    "domainlore.com",
    "voodoo.com",
    "parked.com",
    "domainnamesales.com",
    "brandbucket.com",
    "namebright.com",
)
_PARKED_STRONG = (
    "this domain may be for sale",
    "this domain is for sale",
    "the domain is for sale",
    "domain is for sale",
    "this domain name is for sale",
    "buy this domain",
    "make an offer on this domain",
    "the domain name you entered is for sale",
    "ce nom de domaine est à vendre",
    "ce domaine est à vendre",
    "le nom de domaine est à vendre",
    "ce nom de domaine est en vente",
    "nom de domaine à vendre",
    "diese domain steht zum verkauf",
    "diese domain kaufen",
    "este dominio está a la venta",
    "questo dominio è in vendita",
    "parked free, courtesy of godaddy",
    "this web page is parked free",
    "is parked free",
    "this domain is parked",
    "domaine parqué",
)
_PARKED_WEAK = (
    "related searches",
    "domain name",
    "sponsored listings",
    "this domain",
    "for sale",
    "à vendre",
    "parked",
)
_PARKING_REFS = (
    "dan.com/",
    "sedo.com/",
    "sedoparking.com",
    "afternic.com",
    "hugedomains.com",
    "parkingcrew.net",
    "bodis.com",
    "above.com/",
    "domainmarket.com",
    "buydomains.com",
    "parklogic.com",
    "img1.wsimg.com/parking",
)


def detect_parked(html: str, parsed: ParsedPage, final_url: str) -> str | None:
    """Reason string when the page is a parked / for-sale domain, else None.

    "Site en construction" / "coming soon" pages are NOT parked (the company exists).
    """
    host = (urlsplit(final_url).hostname or "").lower()
    for ph in _PARKING_HOSTS:
        if host == ph or host.endswith("." + ph):
            return f"redirected to parking host {ph}"
    low_html = (html or "")[:300_000].lower()
    low_text = " ".join((parsed.content_text or "").lower().split())
    for marker in _PARKED_STRONG:
        if marker in low_text:
            return f"parked marker: {marker}"
    for ph in _PARKING_REFS:
        if re.search(rf"(?:src|href|action|content)\s*=\s*[\"'][^\"']*{re.escape(ph)}", low_html):
            return f"parking service reference: {ph}"
    if re.search(
        r"window\.location(?:\.href)?\s*=\s*[\"'][^\"']*(?:sedoparking|parkingcrew|bodis|dan\.com)", low_html
    ):
        return "parking redirect script"
    if len(low_text) < 1200 and sum(1 for w in _PARKED_WEAK if w in low_text) >= 3:
        return "parked page heuristics"
    return None


# ---- helpers ----------------------------------------------------------------------------------


def _home_candidates(website_url: str) -> list[str]:
    raw = website_url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    first = urlunsplit((scheme, netloc, path, parts.query, ""))
    alt = "http" if scheme == "https" else "https"
    out = [first, urlunsplit((alt, netloc, path, parts.query, ""))]
    if host and not host.startswith("www.") and parts.port is None and host.count(".") == 1:
        out += [f"https://www.{host}/", f"http://www.{host}/"]
    return list(dict.fromkeys(out))


def _fetched_page(
    url: str,
    resp: http.HttpResponse,
    parsed: ParsedPage,
    page_type: PageType,
    *,
    tier: FetchTier,
    is_home: bool,
) -> FetchedPage:
    return FetchedPage(
        url=url,
        final_url=resp.final_url,
        canonical_url=canonical_url(resp.final_url),
        status_code=resp.status_code,
        content_type=resp.content_type,
        page_type=page_type,
        title=parsed.title,
        meta_description=parsed.meta_description,
        content_text=parsed.content_text,
        content_hash=content_hash(parsed.content_text),
        language=parsed.language,
        headers=dict(resp.headers) if is_home else {},
        head_html=parsed.head_html if is_home else None,
        links=parsed.links,
        emails=parsed.emails,
        phones=parsed.phones,
        structured_data=parsed.structured_data,
        headings=parsed.headings,
        word_count=parsed.word_count,
        fetch_tier=tier,
        etag=resp.etag,
        last_modified=resp.last_modified,
        not_modified=False,
    )


async def _maybe_render(
    url: str, html: str, parsed: ParsedPage, *, is_home: bool
) -> tuple[str, ParsedPage, FetchTier]:
    """Upgrade to a JS tier only when the HTTP response looks client-rendered."""
    if not render.needs_js(html, parsed):
        return html, parsed, FetchTier.http
    for tier, renderer in (
        (FetchTier.crawl4ai, render.render_crawl4ai),
        (FetchTier.browser, render.render_playwright),
    ):
        rendered = await renderer(url)
        if rendered:
            reparsed = parse_html(rendered, url, is_home=is_home)
            if len(reparsed.content_text) > len(parsed.content_text):
                if is_home and parsed.head_html and not reparsed.head_html:
                    reparsed.head_html = parsed.head_html
                return rendered, reparsed, tier
    return html, parsed, FetchTier.http


async def _fetch_home(candidates: list[str]) -> tuple[http.HttpResponse | None, CrawlResult | None]:
    """Try the home URL variants. Returns (response, None) or (None, failure result)."""
    last_error: str | None = None
    last_category: ErrorCategory | None = None
    for url in candidates:
        try:
            resp = await http.fetch(url)
        except SSRFBlocked as exc:
            return None, _failure(url, WebsiteStatus.unreachable, ErrorCategory.validation, str(exc))
        except RateLimitedError as exc:
            return None, _failure(url, WebsiteStatus.blocked, ErrorCategory.rate_limited, str(exc))
        except BlockedError as exc:
            return None, _failure(url, WebsiteStatus.blocked, ErrorCategory.blocked, str(exc))
        except JobError as exc:
            last_error, last_category = str(exc), exc.category
            continue
        if resp.status_code >= 400 or not resp.text.strip():
            last_error = (
                f"home returned HTTP {resp.status_code}" if resp.status_code >= 400 else "empty home page"
            )
            last_category = (
                ErrorCategory.not_found if resp.status_code in (404, 410) else ErrorCategory.network
            )
            continue
        return resp, None
    return None, _failure(
        candidates[0], WebsiteStatus.unreachable, last_category or ErrorCategory.network, last_error
    )


def _failure(
    url: str, status: WebsiteStatus, category: ErrorCategory | None, error: str | None
) -> CrawlResult:
    return CrawlResult(
        domain=registrable_domain(url) or (urlsplit(url).hostname or ""),
        home_url=None,
        status=status,
        error_category=category,
        error=(error or "")[:500] or None,
    )


async def _sitemap_urls(home_url: str, domain: str) -> list[str]:
    """Robots-listed sitemaps (same site) or /sitemap.xml; at most MAX_SITEMAP_FETCHES requests."""
    try:
        declared = [u for u in await robots.sitemaps(home_url) if registrable_domain(u) == domain]
    except Exception:
        declared = []
    parts = urlsplit(home_url)
    first = declared[0] if declared else f"{parts.scheme}://{parts.netloc}/sitemap.xml"
    queue = [first]
    urls: list[str] = []
    fetches = 0
    while queue and fetches < MAX_SITEMAP_FETCHES:
        sm = queue.pop(0)
        fetches += 1
        try:
            resp = await http.fetch(
                sm, accept="application/xml,text/xml;q=0.9,*/*;q=0.5", max_bytes=2_000_000
            )
        except (JobError, SSRFBlocked):
            continue
        if resp.status_code != 200 or not resp.text:
            continue
        entries = parse_sitemap_entries(resp.text)
        urls.extend(entries.urls)
        if entries.sitemaps and not urls:
            queue.extend(
                u for u in pick_child_sitemaps(entries.sitemaps, 1) if registrable_domain(u) == domain
            )
    return urls


# ---- main entry point -------------------------------------------------------------------------


async def crawl_site(
    website_url: str,
    *,
    max_pages: int | None = None,
    known: KnownPages | None = None,
) -> CrawlResult:
    """Crawl a company website (5–12 pages). ``known`` maps canonical URL → (etag, last_modified,
    content_hash) from the cache so unchanged pages can be revalidated with conditional requests."""
    started = time.monotonic()
    deadline = started + CRAWL_TIME_BUDGET_S
    known = known or {}
    candidates = _home_candidates(website_url)
    try:
        validate_url(candidates[0])
    except SSRFBlocked as exc:
        return _failure(candidates[0], WebsiteStatus.unreachable, ErrorCategory.validation, str(exc))

    try:
        async with asyncio.timeout(HOME_TIME_BUDGET_S):
            if not await robots.allowed(candidates[0]):
                res = _failure(
                    candidates[0], WebsiteStatus.blocked, ErrorCategory.blocked, "disallowed by robots.txt"
                )
                res.robots_blocked = True
                return res
            home_resp, failure = await _fetch_home(candidates)
    except TimeoutError:
        home_resp, failure = (
            None,
            _failure(
                candidates[0],
                WebsiteStatus.unreachable,
                ErrorCategory.timeout,
                "home page time budget exhausted",
            ),
        )
    if failure is not None or home_resp is None:
        assert failure is not None
        log.info("crawl_home_failed", url=website_url, status=failure.status, error=failure.error)
        return failure

    final_url = home_resp.final_url
    domain = registrable_domain(final_url) or (urlsplit(final_url).hostname or "")
    result = CrawlResult(
        domain=domain, home_url=final_url, status=WebsiteStatus.ok, bytes=home_resp.size_bytes
    )
    if domain in NON_COMPANY_DOMAINS:
        result.status = WebsiteStatus.unreachable
        result.error_category = ErrorCategory.validation
        result.error = f"website redirects to a non-company host ({domain})"
        return result

    home_parsed = parse_html(home_resp.text, final_url, is_home=True)
    reason = detect_parked(home_resp.text, home_parsed, final_url)
    if reason:
        result.status = WebsiteStatus.parked
        result.error = reason
        return result

    if final_url != candidates[0] and not await robots.allowed(final_url):
        result.status = WebsiteStatus.blocked
        result.robots_blocked = True
        result.error_category = ErrorCategory.blocked
        result.error = "disallowed by robots.txt"
        return result

    _html, home_parsed, tier = await _maybe_render(final_url, home_resp.text, home_parsed, is_home=True)
    home_page = _fetched_page(final_url, home_resp, home_parsed, PageType.home, tier=tier, is_home=True)
    result.pages.append(home_page)
    result.tier_max = tier

    try:
        async with asyncio.timeout(max(1.0, min(15.0, deadline - time.monotonic()))):
            sitemap_urls = await _sitemap_urls(final_url, domain)
    except TimeoutError:
        sitemap_urls = []
    plan = select_pages(final_url, home_parsed.links.get("internal", []), sitemap_urls, max_pages)
    seen = {home_page.canonical_url, canonical_url(final_url)}
    lock = asyncio.Lock()

    async def fetch_one(url: str, page_type: PageType) -> None:
        if not await robots.allowed(url):
            return
        key = canonical_url(url)
        etag, last_mod, cached_hash = known.get(key, (None, None, ""))
        try:
            async with domain_slot(domain):
                resp = await http.fetch(
                    url, etag=etag if cached_hash else None, last_modified=last_mod if cached_hash else None
                )
        except (JobError, SSRFBlocked) as exc:
            log.debug("crawl_page_failed", url=url, error=str(exc))
            result.pages_failed += 1
            return
        result.bytes += resp.size_bytes
        final_key = canonical_url(resp.final_url)
        if resp.not_modified:
            async with lock:
                if key in seen:
                    return
                seen.add(key)
                result.pages.append(
                    FetchedPage(
                        url=url,
                        final_url=resp.final_url,
                        canonical_url=key,
                        status_code=304,
                        content_type=resp.content_type,
                        page_type=page_type,
                        content_hash=cached_hash,
                        etag=resp.etag or etag,
                        last_modified=resp.last_modified or last_mod,
                        not_modified=True,
                    )
                )
            return
        if resp.status_code >= 400 or registrable_domain(resp.final_url) != domain:
            result.pages_failed += 1
            return
        async with lock:
            if final_key in seen:
                return  # redirected to an already-fetched page (often the home page)
            seen.add(final_key)
        parsed = parse_html(resp.text, resp.final_url)
        _html, parsed, page_tier = await _maybe_render(resp.final_url, resp.text, parsed, is_home=False)
        result.pages.append(_fetched_page(url, resp, parsed, page_type, tier=page_tier, is_home=False))
        if _TIER_ORDER[page_tier] > _TIER_ORDER[result.tier_max]:
            result.tier_max = page_tier

    tasks = [asyncio.create_task(fetch_one(u, pt)) for u, pt in plan if pt != PageType.home]
    if tasks:
        remaining = deadline - time.monotonic()
        done, pending = await asyncio.wait(tasks, timeout=max(1.0, remaining))
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            result.pages_failed += len(pending)
            log.info("crawl_time_budget_exhausted", domain=domain, pending=len(pending))
        for t in done:
            err = t.exception()
            if err is not None:
                log.warning("crawl_page_error", domain=domain, error=repr(err))
                result.pages_failed += 1
    # Stable order: home first, then by plan order.
    order = {canonical_url(u): i for i, (u, _pt) in enumerate(plan)}
    result.pages.sort(key=lambda p: (p.page_type != PageType.home, order.get(canonical_url(p.url), 99)))
    log.info(
        "crawl_done",
        domain=domain,
        pages=len(result.pages),
        failed=result.pages_failed,
        tier_max=result.tier_max,
        ms=int((time.monotonic() - started) * 1000),
    )
    return result
