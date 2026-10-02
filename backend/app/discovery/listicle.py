"""Listicle expansion: "Top 15 Facebook ads agencies for coaches" pages are gold — harvest their outbound links."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..lexicon import LISTICLE_TITLE_RE
from ..util.text import ParsedPage
from ..util.urls import absolutize, is_blocked_domain, looks_like_asset, registrable_domain, social_network

_ANCHOR_NOISE = re.compile(r"^(read more|learn more|visit( website| site)?|website|click here|here|source|link|view|see more|get started)$", re.I)
_LIST_CONTEXT = re.compile(r"\b(agency|agencies|marketing|ads|funnel|growth|media|digital|partners|studio|collective|labs|consult)\b", re.I)


@dataclass
class ListicleLink:
    url: str
    domain: str
    anchor: str
    score: float


def is_listicle(title: str, snippet: str = "", url: str = "") -> bool:
    t = f"{title} {snippet}"
    if LISTICLE_TITLE_RE.search(t):
        return True
    return bool(re.search(r"/(top|best)-\d*-?[a-z-]*agenc", url, re.I))


def extract_listicle_links(page: ParsedPage, source_domain: str) -> list[ListicleLink]:
    """Outbound links that look like agency homepages (ranked by how 'agency-like' the anchor/heading is)."""
    found: dict[str, ListicleLink] = {}
    headings_text = " ".join(page.headings).lower()
    for href, anchor in page.links:
        url = absolutize(page.url, href)
        if not url or looks_like_asset(url):
            continue
        dom = registrable_domain(url)
        if not dom or dom == source_domain or is_blocked_domain(dom) or social_network(url):
            continue
        anchor_clean = (anchor or "").strip()
        score = 0.0
        # homepage-ish links are far more likely to be the agency itself
        path = url.split("://", 1)[-1].split("/", 1)[1] if "/" in url.split("://", 1)[-1] else ""
        if path.strip("/") == "":
            score += 2.0
        elif path.count("/") <= 1:
            score += 0.8
        if anchor_clean and not _ANCHOR_NOISE.match(anchor_clean):
            score += 0.5
            if _LIST_CONTEXT.search(anchor_clean):
                score += 1.0
            if anchor_clean.lower().replace(" ", "") in dom.replace("-", "").replace(".", ""):
                score += 1.0
        if dom.split(".")[0] in headings_text:
            score += 1.5
        if re.search(r"(utm_|/go/|/recommends/|/out/|affiliate|ref=)", url, re.I):
            score += 0.3  # listicles often use tracked links to the agencies they feature
        cur = found.get(dom)
        if cur is None or score > cur.score:
            found[dom] = ListicleLink(url=url, domain=dom, anchor=anchor_clean[:120], score=score)
    return sorted(found.values(), key=lambda l: -l.score)
