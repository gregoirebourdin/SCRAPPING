"""Funnel detection on a client (coach / infopreneur) website.

Output per funnel: entry URL, type (webinar, VSL, application, lead magnet, challenge, quiz, low-ticket, course sales...),
hosting platform, offer headline, price hint, observed steps (opt-in form, video, calendar, checkout...) and evidence.

Signals are read from the *visible* text and the DOM, never from raw markup alone (``application/json`` in a script tag
is not an application form).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..config import settings
from ..fetch.crawler import CrawledSite, SiteCrawler
from ..lexicon import EXTERNAL_FUNNEL_HOSTS, FUNNEL_PATH_HINTS, FUNNEL_TYPE_RE, PRICE_RE
from ..util.text import ParsedPage, clean_ws, parse_html
from ..util.urls import absolutize, canonicalize, looks_like_asset, registrable_domain, social_network
from .tech import detect_tech, primary_platform

log = logging.getLogger(__name__)

CTA_RE = re.compile(r"\b(apply( now| here| today)?|book (a|your|my) (call|session)|register( now| here| free)?|join( now| the| today| us)?|enrol?l( now| today)?|get (started|access|instant access|the (guide|training|free))|start (now|today|here)|watch (now|the (free )?(training|video|masterclass))|download( now| the| free)?|free (training|masterclass|guide|workshop|class|webinar)|claim (your|my)|reserve (your|my) (seat|spot)|save (my|your) (seat|spot)|sign ?up|work with me|yes,? i|learn more)\b", re.I)
TYPE_PRIORITY = ["webinar", "vsl", "application", "challenge", "quiz", "lead_magnet", "course_sales", "low_ticket", "book_funnel", "community", "newsletter", "link_in_bio"]
SKIP_FUNNEL_URL = re.compile(r"(privacy|terms|legal|cookie|login|log-in|signin|sign-in|cart|checkout/|account|/blog/|/news/|/tag/|/category/|/author/|facebook\.com|instagram\.com|/wp-|/feed|\.pdf$|/about|/contact|/faq|/press|/careers|/jobs|/podcast/|/episode)", re.I)
APPLICATION_TEXT_RE = re.compile(r"\b(apply now|apply here|apply today|apply to work|application form|submit (your|an) application|fill (out|in) (the|this|your) application|see if you qualify|do you qualify)\b", re.I)
WEBINAR_TEXT_RE = re.compile(r"\b(register (now|here|for the|for this|free)|save (my|your) seat|reserve (my|your) (seat|spot)|choose a time|watch the (free )?training|free (training|masterclass|workshop|class|webinar)|on[- ]demand training)\b", re.I)
URGENCY_RE = re.compile(r"\b(countdown|deadline|expires in|offer ends|closes in|doors close|cart closes|limited (time|spots)|only \d+ (spots|seats))\b", re.I)


@dataclass
class FunnelCandidate:
    url: str
    anchor: str
    score: float
    external: bool


@dataclass
class DetectedFunnel:
    url: str
    funnel_type: str
    platform: str | None
    offer: str | None
    price_hint: str | None
    steps: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    confidence: float = 0.4

    def summary(self) -> str:
        plat = f" on {self.platform}" if self.platform else ""
        steps = " → ".join(s["step"] for s in self.steps) if self.steps else ""
        return f"{self.funnel_type.replace('_', ' ')} funnel{plat}" + (f": {steps}" if steps else "")


def _classify(url: str, anchor: str, title: str, h1: str, text_head: str) -> tuple[str, dict[str, int]]:
    """Strong signals (url / anchor / title / h1) weigh 3, body text matches weigh 1 and need ≥2 distinct hits."""
    strong_blob = f"{url.split('://', 1)[-1]} {anchor} {title} {h1}"
    weak_blob = text_head[:2500]
    scores: dict[str, int] = {}
    for t, rx in FUNNEL_TYPE_RE.items():
        strong = len(rx.findall(strong_blob))
        weak = len({m.group(0).lower() for m in rx.finditer(weak_blob)})
        s = strong * 3 + (weak if weak >= 2 else 0)
        if s:
            scores[t] = s
    if not scores or max(scores.values()) < 3:
        return "landing_page", scores
    best = max(scores.items(), key=lambda kv: (kv[1], -TYPE_PRIORITY.index(kv[0]) if kv[0] in TYPE_PRIORITY else -99))
    return best[0], scores


def _steps(html: str, parsed: ParsedPage) -> list[dict]:
    steps: list[dict] = []
    tree = HTMLParser(html[:800_000]) if html else None
    low = html.lower() if html else ""
    text = parsed.text[:60_000]
    if tree is not None:
        if tree.css_first('input[type="email"], input[name*="email" i], input[placeholder*="email" i]'):
            steps.append({"step": "opt-in form (email)", "evidence": "email input"})
        if tree.css_first('input[type="tel"], input[name*="phone" i], input[placeholder*="phone" i]'):
            steps.append({"step": "phone capture", "evidence": "phone input"})
        if APPLICATION_TEXT_RE.search(text) and (tree.css_first("form textarea, form select") or "typeform" in low or "jotform" in low or "paperform" in low):
            steps.append({"step": "application form", "evidence": APPLICATION_TEXT_RE.search(text).group(0)})
    if re.search(r"(wistia|player\.vimeo\.com|youtube\.com/embed|youtube-nocookie|vidalytics|vidyard|loom\.com/embed|<video[\s>]|bunny\.net|muse\.ai)", low):
        steps.append({"step": "video (VSL / training)", "evidence": "video embed"})
    if re.search(r"(calendly\.com|acuityscheduling|hubspot\.com/meetings|savvycal|tidycal|oncehub|cal\.com/|zcal\.co)", low) or re.search(r"\b(book (a|your) (call|session)|schedule (a|your) call)\b", text, re.I):
        steps.append({"step": "calendar booking (sales call)", "evidence": "calendar embed / booking CTA"})
    if re.search(r"(js\.stripe\.com|checkout\.stripe|samcart|thrivecart|paypal\.com/sdk|/checkout|add to cart|buy now|enroll now|enrol now|pay in full|payment plan)", low) or re.search(r"\b(enroll now|buy now|pay in full|payment plan|add to cart)\b", text, re.I):
        steps.append({"step": "checkout / enrollment", "evidence": "payment or enrollment element"})
    if re.search(r"(webinarjam|everwebinar|demio|zoom\.us/webinar|stealthseminar|ewebinar)", low) or WEBINAR_TEXT_RE.search(text):
        steps.append({"step": "webinar / training registration", "evidence": "webinar wording or platform"})
    if URGENCY_RE.search(text) or re.search(r"(countdown-timer|class=\"[^\"]*countdown|deadline-funnel|deadlinefunnel)", low):
        steps.append({"step": "deadline / countdown", "evidence": "urgency timer"})
    seen = set()
    uniq = []
    for s in steps:
        if s["step"] not in seen:
            seen.add(s["step"])
            uniq.append(s)
    return uniq


def funnel_candidates(site: CrawledSite) -> list[FunnelCandidate]:
    """Internal + external links on a client site that look like funnel entry points."""
    cands: dict[str, FunnelCandidate] = {}
    home_dom = site.domain
    for p in site.pages:
        for href, anchor in p.parsed.links:
            url = absolutize(p.url, href)
            if not url or looks_like_asset(url) or SKIP_FUNNEL_URL.search(url):
                continue
            dom = registrable_domain(url)
            if not dom:
                continue
            ext = dom != home_dom
            is_funnel_host = bool(EXTERNAL_FUNNEL_HOSTS.search(url))
            if ext and not is_funnel_host:
                continue  # only follow external links to known funnel platforms
            if social_network(url) and "facebook.com/groups" not in url:
                continue
            a = (anchor or "").strip()
            path = url.split("://", 1)[-1]
            path = path.split("/", 1)[1] if "/" in path else ""
            score = 0.0
            if is_funnel_host:
                score += 3.0
            path_hint = bool(FUNNEL_PATH_HINTS.search(path))
            type_hits = [t for t, rx in FUNNEL_TYPE_RE.items() if rx.search(a) or rx.search(path)]
            cta = bool(CTA_RE.search(a))
            if path_hint:
                score += 1.0
            score += sum(1.0 if t not in ("newsletter", "community", "link_in_bio") else 0.4 for t in type_hits)
            if cta:
                score += 1.5
            # an internal page needs two independent hints (path + anchor/type), one hint alone is noise
            if not is_funnel_host and (path_hint + bool(type_hits) + cta) < 2:
                continue
            if score <= 0:
                continue
            key = canonicalize(url)
            cur = cands.get(key)
            if cur is None or score > cur.score:
                cands[key] = FunnelCandidate(url=url, anchor=a[:100], score=score, external=ext)
    return sorted(cands.values(), key=lambda c: -c.score)


async def detect_funnels(site: CrawledSite, crawler: SiteCrawler, *, max_pages: int | None = None) -> list[DetectedFunnel]:
    max_pages = max_pages or settings.crawl_max_client_pages
    found: list[DetectedFunnel] = []
    home = site.home
    cands = funnel_candidates(site)[:max_pages]
    base_tech = detect_tech(site.all_html)
    base_platform = primary_platform(base_tech)

    # The homepage itself is often the funnel for coaches (single VSL / opt-in page)
    pages: list[tuple[str, str, ParsedPage, str, bool]] = []
    if home is not None:
        pages.append((home.url, "", home.parsed, home.parsed.html, False))
    for c in cands:
        res = await crawler.fetcher.fetch(c.url, ttl_hours=settings.page_cache_ttl_hours, retries=1)
        if not res.ok:
            continue
        parsed = parse_html(res.html, res.final_url or c.url)
        if parsed.text_len < 100 and crawler.browser.available:
            html, final_url, _ = await crawler.browser.render(res.final_url or c.url)
            if html:
                parsed = parse_html(html, final_url)
        pages.append((res.final_url or c.url, c.anchor, parsed, parsed.html, c.external))

    for url, anchor, parsed, html, external in pages:
        h1 = parsed.headings[0] if parsed.headings else ""
        ftype, scores = _classify(url, anchor, parsed.title, h1, parsed.text)
        steps = _steps(html, parsed)
        tech = detect_tech(html)
        platform = primary_platform(tech) or (base_platform if not external else None)
        price = None
        pm = PRICE_RE.search(parsed.text[:20_000])
        if pm:
            price = pm.group(0).strip()
        offer = h1 or parsed.og_title or parsed.title
        offer = clean_ws(offer)[:200] if offer else None
        is_home = home is not None and canonicalize(url) == canonicalize(home.url)
        strong_steps = [s for s in steps if s["step"] not in ("phone capture",)]
        if ftype == "landing_page" and len(strong_steps) < 2:
            continue  # a page with one email box and no funnel wording is just a page
        conf = 0.25
        conf += 0.1 * min(3, len(strong_steps))
        if ftype != "landing_page":
            conf += 0.15
        if external and EXTERNAL_FUNNEL_HOSTS.search(url):
            conf += 0.15
        if platform in ("clickfunnels", "kajabi", "gohighlevel", "kartra", "systeme.io", "leadpages", "funnelish", "groovefunnels"):
            conf += 0.1
        if is_home and ftype == "landing_page":
            conf -= 0.1
        conf = max(0.05, min(0.98, conf))
        if conf < 0.5:
            continue
        found.append(DetectedFunnel(
            url=url, funnel_type=ftype, platform=platform, offer=offer, price_hint=price, steps=steps,
            evidence={"anchor": anchor, "type_scores": scores, "tech": tech[:12], "is_homepage": is_home, "external": external},
            confidence=conf,
        ))
    found.sort(key=lambda f: -f.confidence)
    uniq: list[DetectedFunnel] = []
    seen: set[str] = set()
    for f in found:
        key = canonicalize(f.url)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq[:3]
