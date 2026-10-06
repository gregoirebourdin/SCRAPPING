"""Expand "top 10 / meilleures agences / best agencies" pages into the companies they link to.

Pages are fetched through the SSRF-safe crawler fetcher (they are arbitrary third-party URLs). Only outbound
links sitting in list-like structures (list items, headings, table cells, cards) are kept; navigation, footer,
social, directory and same-site links are ignored. Every candidate comes from a link actually on the page.
"""

from __future__ import annotations

import re

import structlog
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from scout.discovery.base import RawCandidate
from scout.discovery.common import candidate_domain
from scout.util.text import collapse_ws, normalize_key
from scout.util.urls import absolutize, normalize_website, registrable_domain

log = structlog.get_logger(__name__)

_LIST_TAGS = {"li", "h2", "h3", "h4", "td", "dt", "dd"}
_SKIP_TAGS = {"nav", "header", "footer", "aside", "form"}
_CARD_CLASS = re.compile(r"(list|item|card|entry|agency|company|provider|listing|result)", re.I)
_GENERIC_ANCHORS = {
    "site web",
    "site internet",
    "voir le site",
    "visiter le site",
    "visit website",
    "website",
    "visit site",
    "leur site",
    "son site",
    "le site",
    "their website",
    "site",
    "voir",
    "visiter",
    "visit",
    "official website",
    "en savoir plus",
    "learn more",
    "read more",
    "lire la suite",
    "plus d infos",
    "more info",
    "cliquez ici",
    "click here",
    "ici",
    "here",
    "link",
    "lien",
    "webseite",
    "sitio web",
    "sito web",
    "go to website",
}
_ORDINAL = re.compile(r"^\s*(#?\d{1,3}[.)\-–:]?\s+)")


def _follows_heading(node: Node, max_siblings: int = 4) -> bool:
    """Block placed right after an h2/h3/h4 (the classic '## 1. Company' + paragraph listicle layout)."""
    prev, seen = node.prev, 0
    while prev is not None and seen < max_siblings:
        if prev.is_element_node:
            if prev.tag in ("h2", "h3", "h4"):
                return True
            seen += 1
        prev = prev.prev
    return False


def _in_list_structure(node: Node) -> bool:
    cur: Node | None = node.parent
    depth = 0
    while cur is not None and depth < 6:
        if cur.tag in _SKIP_TAGS:
            return False
        if cur.tag in _LIST_TAGS:
            return True
        cls = cur.attributes.get("class") or ""
        if cls and _CARD_CLASS.search(cls):
            return True
        if depth < 3 and _follows_heading(cur):
            return True
        cur, depth = cur.parent, depth + 1
    return False


def _in_skipped_region(node: Node) -> bool:
    cur: Node | None = node.parent
    while cur is not None:
        if cur.tag in _SKIP_TAGS:
            return True
        cur = cur.parent
    return False


def _preceding_heading(node: Node) -> str | None:
    """Closest previous h2/h3/h4 text (for 'Visit website' style anchors)."""
    cur: Node | None = node
    for _ in range(40):
        if cur is None:
            return None
        prev = cur.prev
        while prev is not None:
            if prev.tag in ("h2", "h3", "h4"):
                return collapse_ws(prev.text(separator=" "))
            inner = prev.css_first("h2, h3, h4") if prev.is_element_node else None
            if inner is not None:
                return collapse_ws(inner.text(separator=" "))
            prev = prev.prev
        cur = cur.parent
    return None


def _clean_name(text: str) -> str:
    t = _ORDINAL.sub("", collapse_ws(text))
    return t.strip(" -–—:|")[:120]


def extract_listicle_links(html: str, page_url: str, *, max_links: int = 60) -> list[RawCandidate]:
    """Pure parsing step (no network): outbound company links from list-like structures."""
    tree = HTMLParser(html)
    page_domain = registrable_domain(page_url)
    out: list[RawCandidate] = []
    seen: set[str] = set()
    for a in tree.css("a[href]"):
        if len(out) >= max_links:
            break
        url = absolutize(page_url, a.attributes.get("href") or "")
        dom = candidate_domain(url)
        if not url or not dom or dom == page_domain or dom in seen:
            continue
        if _in_skipped_region(a) or not _in_list_structure(a):
            continue
        anchor = collapse_ws(a.text(separator=" "))
        name = (
            anchor
            if anchor and normalize_key(anchor) not in _GENERIC_ANCHORS
            else (_preceding_heading(a) or "")
        )
        name = _clean_name(name) or dom.split(".")[0].capitalize()
        seen.add(dom)
        out.append(
            RawCandidate(
                source="web_search",
                source_entity_id=dom,
                name=name,
                website=normalize_website(url),
                domain=dom,
                source_url=page_url,
                raw_data={"listicle_url": page_url, "anchor_text": anchor[:200], "link": url},
            )
        )
    return out


async def expand_listicle(url: str, *, max_links: int = 60) -> list[RawCandidate]:
    """Fetch a listicle page (SSRF-safe) and return the companies it links to."""
    from scout.crawl.http import fetch  # lazy: crawler module is optional at import time

    resp = await fetch(url, max_bytes=2_000_000)
    if resp.status_code >= 400 or not resp.text:
        log.info("listicle.fetch_failed", url=url, status=resp.status_code)
        return []
    return extract_listicle_links(resp.text, resp.final_url or url, max_links=max_links)
