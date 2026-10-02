"""Founder identification + LinkedIn profile resolution.

1. Links to ``linkedin.com/in/...`` on the agency's own site (about/team/footer) with the surrounding text.
2. JSON-LD ``Organization.founder`` / ``Person`` with a founder-like ``jobTitle``.
3. Text patterns: "Jane Doe, Founder & CEO", "Founded by Jane Doe", "Hi, I'm Jane — founder of ...".
4. Search engines: ``"Jane Doe" "Agency" site:linkedin.com/in`` — the profile URL and the headline come straight from
   the SERP title ("Jane Doe - Founder - Agency | LinkedIn"), so LinkedIn itself is never requested.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from ..discovery.router import SearchRouter
from ..fetch.crawler import CrawledSite
from ..lexicon import NAME_STOPWORDS
from ..util.text import clean_ws, titlecase_ratio

log = logging.getLogger(__name__)

FOUNDER_TITLE_RE = re.compile(r"\b(co-?founder|founder|ceo|owner|managing director|managing partner|president|principal|director|chief executive)\b", re.I)
NAME_TOKEN = r"[A-Z][a-zA-Z'’.-]+"
NAME_PAT = rf"({NAME_TOKEN}(?:[ \t]+{NAME_TOKEN}){{1,2}})"  # never across a line break
PATTERNS = [
    re.compile(rf"{NAME_PAT}\s*[,|–—\-]\s*((?:co-?)?founder[^.\n|]{{0,40}}|ceo[^.\n|]{{0,40}}|owner[^.\n|]{{0,30}}|managing director[^.\n|]{{0,30}})", re.I),
    re.compile(rf"\b(?:founded|started|created|launched|run|led|owned)\s+by\s+{NAME_PAT}", re.I),
    re.compile(rf"\b(?:founder|co-?founder|ceo|owner|managing director)\s*(?:&\s*ceo\s*)?(?:[:,–—\-]|of [A-Za-z0-9&'’. ]{{2,40}}[,:])?\s*{NAME_PAT}"),
    re.compile(rf"\b(?:hi|hey|hello),?\s+i'?m\s+{NAME_PAT}[^.\n]{{0,80}}\b(founder|ceo|owner|started|built|run)\b", re.I),
    re.compile(rf"\bi'?m\s+{NAME_PAT},\s*(?:the\s+)?(?:co-?)?(founder|ceo|owner)", re.I),
    re.compile(rf"{NAME_PAT}\s+(?:is|,)\s+(?:the|our|a)\s+(?:co-?)?(founder|ceo|owner|managing director)", re.I),
]
LINKEDIN_IN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9%._-]+)/?", re.I)
LINKEDIN_COMPANY_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9%._-]+)/?", re.I)
SERP_TITLE_RE = re.compile(r"^(?P<name>[^|\-–—]{3,60}?)\s*[-–—]\s*(?P<headline>.{3,160}?)(?:\s*\|\s*LinkedIn)?$", re.I)


@dataclass
class FounderHit:
    name: str | None = None
    title: str | None = None
    linkedin: str | None = None
    source: str = "none"
    confidence: float = 0.0
    evidence: str = ""


def _ok_name(n: str) -> str | None:
    n = clean_ws(n).strip(" ,.-|")
    n = re.sub(r"^(?:I'?m|I am|The|Coach|Dr\.?|Mr\.?|Mrs\.?|Ms\.?)\s+", "", n, flags=re.I)
    toks = n.split()
    if not 2 <= len(toks) <= 3 or len(n) > 50 or titlecase_ratio(n) < 0.9:
        return None
    if n.lower() in NAME_STOPWORDS or any(t.lower().strip("'’.") in {"i'm", "im", "i", "we", "the", "our", "and", "team", "agency", "marketing", "ads", "media", "founder", "ceo", "story", "about", "meet", "hi", "hello", "hey"} for t in toks):
        return None
    if re.search(r"\b(Inc|LLC|Ltd|Agency|Media|Marketing|Digital|Group|Co)\b", n):
        return None
    return n


def _name_matches_slug(name: str, slug: str) -> bool:
    toks = [t for t in re.findall(r"[a-z]+", name.lower()) if len(t) > 1]
    s = re.sub(r"[^a-z]", "", slug.lower())
    return bool(toks) and all(t in s for t in toks[:2]) or (len(toks) >= 2 and toks[-1] in s and toks[0][:3] in s)


def _title_from_context(ctx: str) -> str | None:
    m = FOUNDER_TITLE_RE.search(ctx)
    if not m:
        return None
    t = m.group(1)
    full = re.search(re.escape(t) + r"(?:\s*(?:&|and)\s*(?:ceo|founder|cmo|coo))?(?:\s+(?:of|at)\s+[A-Z][\w'’&. -]{2,40})?", ctx, re.I)
    return clean_ws(full.group(0) if full else t)[:100]


def extract_founder(site: CrawledSite) -> FounderHit:
    """Founder name/title and, when the site links it, their LinkedIn profile."""
    best = FounderHit()
    linkedin_links: list[tuple[str, str, str]] = []  # (url, slug, context)

    for p in site.pages:
        if not p.parsed.html:
            continue
        tree = HTMLParser(p.parsed.html)
        for n in tree.css("style, script, noscript, svg"):
            n.decompose()
        for a in tree.css('a[href*="linkedin.com/in/"]'):
            href = a.attributes.get("href") or ""
            m = LINKEDIN_IN_RE.search(href)
            if not m:
                continue
            node = a
            ctx = ""
            for _ in range(4):
                node = node.parent
                if node is None:
                    break
                ctx = clean_ws(node.text(separator=" "))
                if len(ctx) > 60:
                    break
            linkedin_links.append((href.split("?", 1)[0], m.group(1), ctx[:400]))

    # 1) LinkedIn link whose surrounding text names a founder
    for url, slug, ctx in linkedin_links:
        title = _title_from_context(ctx)
        for pat in PATTERNS:
            m = pat.search(ctx)
            if m:
                name = _ok_name(m.group(1))
                if name and _name_matches_slug(name, slug):
                    return FounderHit(name=name, title=title or _title_from_context(m.group(0)) or "Founder", linkedin=url, source="site_link", confidence=0.95, evidence=ctx[:200])
        if title:
            # founder title near a profile link: derive the name from the slug (john-doe-1a2b3c)
            raw = re.sub(r"-?[0-9a-f]{6,}$", "", slug)
            parts = [t for t in raw.split("-") if t.isalpha()]
            if 2 <= len(parts) <= 3:
                name = " ".join(t.capitalize() for t in parts)
                best = FounderHit(name=name, title=title, linkedin=url, source="site_link", confidence=0.75, evidence=ctx[:200])

    # 2) JSON-LD
    for p in site.pages:
        for d in p.parsed.jsonld:
            founder = d.get("founder")
            cands = founder if isinstance(founder, list) else [founder] if founder else []
            t = str(d.get("@type", "")).lower()
            if t == "person" and FOUNDER_TITLE_RE.search(str(d.get("jobTitle", ""))):
                cands.append(d)
            for f in cands:
                name = _ok_name(f.get("name", "")) if isinstance(f, dict) else _ok_name(str(f))
                if name:
                    same = (f.get("sameAs") if isinstance(f, dict) else None) or []
                    same = same if isinstance(same, list) else [same]
                    li = next((s for s in same if isinstance(s, str) and "linkedin.com/in/" in s), None)
                    hit = FounderHit(name=name, title=(f.get("jobTitle") if isinstance(f, dict) else None) or "Founder", linkedin=li, source="site_link" if li else "site_name", confidence=0.9 if li else 0.7, evidence="json-ld")
                    if hit.confidence > best.confidence:
                        best = hit

    # 3) text patterns on about/home/team pages
    if best.confidence < 0.7:
        for p in sorted(site.pages, key=lambda x: 0 if x.kind == "about" else 1 if x.kind == "home" else 2):
            text = p.text[:40_000]
            for pat in PATTERNS:
                for m in pat.finditer(text):
                    name = _ok_name(m.group(1))
                    if not name:
                        continue
                    ctx = text[max(0, m.start() - 80): m.end() + 120]
                    if re.search(r"\b(client|testimonial|case study|review|helped)\b", ctx, re.I) and not re.search(r"\b(our|my) (founder|ceo|agency|company|team)\b", ctx, re.I):
                        continue
                    title = _title_from_context(m.group(0)) or _title_from_context(ctx) or "Founder"
                    hit = FounderHit(name=name, title=title, source="site_name", confidence=0.6 if p.kind in ("about", "home") else 0.5, evidence=clean_ws(ctx)[:200])
                    # pair with a LinkedIn link whose slug matches the name
                    for url, slug, _ in linkedin_links:
                        if _name_matches_slug(name, slug):
                            hit.linkedin, hit.source, hit.confidence = url, "site_link", 0.9
                            break
                    if hit.confidence > best.confidence:
                        best = hit
                if best.confidence >= 0.9:
                    break
            if best.confidence >= 0.9:
                break

    # 4) a personal LinkedIn link whose slug spells a name that appears on the site (or in a social handle)
    if not best.linkedin and linkedin_links:
        names = _names_on_site(site)
        handles = _social_handles(site)
        slugs = {s for _, s, _ in linkedin_links}
        for url, slug, ctx in linkedin_links:
            letters = re.sub(r"[^a-z]", "", re.sub(r"-?[0-9a-f]{5,}$", "", slug.lower()))
            letters = re.sub(r"^(the|coach|dr|iam|im|real|official)", "", letters)
            if len(letters) < 5:
                continue
            match = next((n for n in names if re.sub(r"[^a-z]", "", n.lower()) == letters), None)
            if match is None:
                match = next((h for h in handles if re.sub(r"[^a-z]", "", h.lower()) == letters), None)
            if match is None and len(slugs) == 1:
                parts = [t for t in re.sub(r"-?[0-9a-f]{5,}$", "", slug).split("-") if t.isalpha()]
                if 2 <= len(parts) <= 3:
                    match = " ".join(t.capitalize() for t in parts)
            if match:
                title = _title_from_context(ctx) or _title_near_name(site, match) or "Founder"
                conf = 0.8 if len(slugs) == 1 else 0.65
                if any(t for t in re.findall(r"[a-z]{4,}", match.lower()) if t in site.domain.split(".")[0]):
                    conf = 0.9  # last name in the agency domain: owner-operated
                if conf > best.confidence:
                    best = FounderHit(name=match, title=title, linkedin=url, source="site_link", confidence=conf, evidence=ctx[:200] or "linkedin slug matches name")
    if not best.linkedin and len({s for _, s, _ in linkedin_links}) == 1 and best.name and _name_matches_slug(best.name, linkedin_links[0][1]):
        best.linkedin, best.source, best.confidence = linkedin_links[0][0], "site_link", max(best.confidence, 0.85)
    return best


def _names_on_site(site: CrawledSite) -> list[str]:
    """Capitalised 2-3 token names found in the about/home/contact copy."""
    out: list[str] = []
    seen: set[str] = set()
    for p in site.pages_of("about", "home", "contact", "services"):
        for m in re.finditer(NAME_PAT, p.text[:60_000]):
            n = _ok_name(m.group(1))
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
    return out


def _social_handles(site: CrawledSite) -> list[str]:
    """``instagram.com/jodi_sodini`` → ``Jodi Sodini`` (only handles with a separator can be split)."""
    out: list[str] = []
    for p in site.pages:
        for href, _ in p.parsed.links:
            m = re.search(r"(?:instagram\.com|twitter\.com|x\.com|tiktok\.com/@?|facebook\.com|youtube\.com/@)/?([A-Za-z0-9._-]{4,40})/?", href or "")
            if not m:
                continue
            parts = [t for t in re.split(r"[._-]", m.group(1)) if t.isalpha() and len(t) > 1]
            parts = [t for t in parts if t.lower() not in HANDLE_NOISE]
            if 2 <= len(parts) <= 3:
                out.append(" ".join(t.capitalize() for t in parts))
    return out


HANDLE_NOISE = {"the", "official", "real", "iam", "im", "coach", "mr", "mrs", "ms", "dr", "its", "hey", "hi", "team", "agency", "marketing", "official", "page", "tv", "hq", "co", "inc"}


def _title_near_name(site: CrawledSite, name: str) -> str | None:
    for p in site.pages:
        i = p.text.find(name)
        if i >= 0:
            t = _title_from_context(p.text[max(0, i - 100): i + len(name) + 160])
            if t:
                return t
    return None


def company_linkedin(site: CrawledSite) -> str | None:
    for p in site.pages:
        for href, _ in p.parsed.links:
            m = LINKEDIN_COMPANY_RE.search(href or "")
            if m:
                return f"https://www.linkedin.com/company/{m.group(1)}/"
    return None


async def resolve_linkedin(router: SearchRouter, agency_name: str, founder: FounderHit, domain: str) -> FounderHit:
    """Find the founder's LinkedIn URL via search-engine results only."""
    if founder.linkedin:
        return founder
    short = re.sub(r"\b(agency|marketing|media|digital|llc|inc|ltd|co|group)\b", "", agency_name, flags=re.I).strip() or agency_name
    queries: list[tuple[str, str]] = []
    if founder.name:
        queries.append((f'"{founder.name}" "{short}" site:linkedin.com/in', "name+agency"))
        queries.append((f'"{founder.name}" {short} linkedin', "name+agency"))
    queries.append((f'"{short}" founder site:linkedin.com/in', "agency"))
    queries.append((f'{domain} founder linkedin', "agency"))
    for q, mode in queries[:3]:
        results = await router.search(q, country="us", pages=1, max_engines=1)
        for r in results:
            m = LINKEDIN_IN_RE.search(r.url)
            if not m:
                continue
            slug = m.group(1)
            title = clean_ws(r.title)
            tm = SERP_TITLE_RE.match(title)
            serp_name = _ok_name(tm.group("name")) if tm else None
            headline = tm.group("headline") if tm else ""
            agency_tokens = {t for t in re.findall(r"[a-z0-9]+", short.lower()) if len(t) > 2}
            mentions_agency = bool(agency_tokens & set(re.findall(r"[a-z0-9]+", f"{title} {r.snippet}".lower())))
            if founder.name:
                if _name_matches_slug(founder.name, slug) or (serp_name and serp_name.lower() == founder.name.lower()):
                    conf = 0.85 if mentions_agency else 0.6
                    return FounderHit(name=founder.name, title=founder.title or (headline[:100] if headline else None), linkedin=f"https://www.linkedin.com/in/{slug}/", source="serp", confidence=conf, evidence=title[:200])
            else:
                if serp_name and mentions_agency and FOUNDER_TITLE_RE.search(f"{headline} {r.snippet}"):
                    return FounderHit(name=serp_name, title=_title_from_context(headline) or headline[:100], linkedin=f"https://www.linkedin.com/in/{slug}/", source="serp", confidence=0.7, evidence=title[:200])
    return founder


def linkedin_slug(url: str | None) -> str | None:
    if not url:
        return None
    m = LINKEDIN_IN_RE.search(url)
    return m.group(1) if m else None


def host(url: str) -> str:
    return urlparse(url).hostname or ""
