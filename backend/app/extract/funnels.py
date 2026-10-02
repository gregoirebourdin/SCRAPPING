"""Funnel detection on a client (coach / infopreneur) website.

Output per funnel: entry URL, type (webinar, VSL, application, lead magnet, challenge, quiz, low-ticket, course sales...),
hosting platform, offer headline, price hint, observed steps (opt-in form, video, calendar, checkout...) and evidence.
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
from ..util.urls import absolutize, canonicalize, is_blocked_domain, looks_like_asset, registrable_domain, social_network
from .tech import detect_tech, primary_platform

log = logging.getLogger(__name__)

CTA_RE = re.compile(r"\b(apply|book|register|join|enroll|enrol|get (started|access|instant|the|my|your)|start|watch|download|free|claim|reserve|save (my|your) seat|sign up|learn more|work with me|yes,? i)\b", re.I)
TYPE_PRIORITY = ["webinar", "vsl", "application", "challenge", "quiz", "lead_magnet", "course_sales", "low_ticket", "book_funnel", "community", "newsletter", "link_in_bio"]


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
    blob_strong = f"{url} {anchor} {title} {h1}"
    blob_weak = text_head[:1500]
    scores: dict[str, int] = {}
    for t, rx in FUNNEL_TYPE_RE.items():
        s = len(rx.findall(blob_strong)) * 3 + min(3, len(rx.findall(blob_weak)))
        if s:
            scores[t] = s
    if not scores:
        return "landing_page", scores
    best = max(scores.items(), key=lambda kv: (kv[1], -TYPE_PRIORITY.index(kv[0]) if kv[0] in TYPE_PRIORITY else -99))
    return best[0], scores


def _steps(html: str, parsed: ParsedPage) -> list[dict]:
    steps: list[dict] = []
    tree = HTMLParser(html[:800_000]) if html else None
    low = html.lower() if html else ""
    if tree is not None:
        if tree.css_first('input[type="email"], input[name*="email" i], input[placeholder*="email" i]'):
            steps.append({"step": "opt-in form (email)", "evidence": "email input"})
        if tree.css_first('input[type="tel"], input[name*="phone" i]'):
            steps.append({"step": "phone capture", "evidence": "phone input"})
        if tree.css_first("form textarea") or re.search(r"(application|apply now|qualify)", low[:200_000]):
            if re.search(r"(application|apply)", low[:200_000]):
                steps.append({"step": "application form", "evidence": "application wording / long form"})
    if re.search(r"(wistia|vimeo\.com/video|player\.vimeo|youtube\.com/embed|youtube-nocookie|vidalytics|vidyard|loom\.com/embed|<video)", low):
        steps.append({"step": "video (VSL / training)", "evidence": "video embed"})
    if re.search(r"(calendly\.com|acuityscheduling|hubspot\.com/meetings|savvycal|tidycal|oncehub|cal\.com/|zcal\.co|book(ing)? a call|schedule (a|your) call)", low):
        steps.append({"step": "calendar booking (sales call)", "evidence": "calendar embed / booking CTA"})
    if re.search(r"(js\.stripe\.com|checkout\.stripe|samcart|thrivecart|paypal\.com/sdk|kajabi.*checkout|clickfunnels.*order|/checkout|add to cart|buy now|enroll now)", low):
        steps.append({"step": "checkout / enrollment", "evidence": "payment or enrollment element"})
    if re.search(r"(webinarjam|everwebinar|demio|zoom\.us/webinar|register (now|for the)|save (my|your) seat|choose a time)", low):
        steps.append({"step": "webinar registration", "evidence": "webinar wording / platform"})
    if re.search(r"(countdown|deadline|expires in|offer ends|closes in|evergreen)", low):
        steps.append({"step": "deadline / countdown", "evidence": "urgency timer"})
    if re.search(r"(thank[- ]you|confirmation|next step|watch this video before)", low[:300_000]) and "thank" in parsed.url.lower():
        steps.append({"step": "thank-you / upsell page", "evidence": "url"})
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
            if not url or looks_like_asset(url):
                continue
            dom = registrable_domain(url)
            if not dom:
                continue
            ext = dom != home_dom
            if ext and (is_blocked_domain(dom) and not EXTERNAL_FUNNEL_HOSTS.search(url)):
                continue
            if social_network(url) and "facebook.com/groups" not in url:
                continue
            a = (anchor or "").strip()
            score = 0.0
            if EXTERNAL_FUNNEL_HOSTS.search(url):
                score += 3.0
            if FUNNEL_PATH_HINTS.search(url.split("://", 1)[-1].split("/", 1)[-1] if "/" in url.split("://", 1)[-1] else ""):
                score += 1.5
            for t, rx in FUNNEL_TYPE_RE.items():
                if rx.search(a) or rx.search(url):
                    score += 1.0 if t not in ("newsletter", "community", "link_in_bio") else 0.4
            if CTA_RE.search(a):
                score += 1.0
            if re.search(r"(privacy|terms|login|cart|blog|/tag/|/category/|/author/|facebook\.com|instagram\.com|/wp-|/feed)", url, re.I):
                continue
            if score <= 0:
                continue
            if ext and score < 2:
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
    cands = funnel_candidates(site)[: max_pages]
    base_tech = detect_tech(site.all_html)
    base_platform = primary_platform(base_tech)

    # The homepage itself is often the funnel for coaches (single VSL/opt-in page)
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
        conf = 0.3
        conf += 0.1 * min(3, len(steps))
        if ftype != "landing_page":
            conf += 0.15
        if external and EXTERNAL_FUNNEL_HOSTS.search(url):
            conf += 0.15
        if platform in ("clickfunnels", "kajabi", "gohighlevel", "kartra", "systeme.io", "leadpages", "funnelish", "groovefunnels"):
            conf += 0.1
        if is_home and ftype == "landing_page" and not steps:
            conf -= 0.2
        conf = max(0.05, min(0.98, conf))
        if ftype == "landing_page" and not steps:
            continue
        found.append(DetectedFunnel(
            url=url, funnel_type=ftype, platform=platform, offer=offer, price_hint=price, steps=steps,
            evidence={"anchor": anchor, "type_scores": scores, "tech": tech[:12], "is_homepage": is_home, "external": external},
            confidence=conf,
        ))
    # de-duplicate by (type, platform) keeping the most confident, max 5
    found.sort(key=lambda f: -f.confidence)
    uniq: list[DetectedFunnel] = []
    seen: set[str] = set()
    for f in found:
        key = canonicalize(f.url)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq[:5]
