"""Explicit "are these search results sufficient?" rules.

Every escalation to Gemini grounding is justified by an ``Assessment`` with a human-readable reason:

* discovery (company lists): at least *k* distinct company domains once directories, social networks, media,
  gov/edu hosts and "top 10" listicles are dropped (``assess_companies``);
* lookups about one company / person (people discovery, enrichment research): at least *k* results that
  mention the target company by name or domain (``assess_subject``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from scout.search.types import SearchResult
from scout.util.text import normalize_company_name, normalize_key
from scout.util.urls import registrable_domain

LISTICLE_RE = re.compile(
    r"(\btop\s*\d+|\b\d+\s+(meilleur|best|top|agences|agencies|entreprises|companies|startups|soci[ée]t[ée]s|firms|cabinets)"
    r"|\bmeilleur(e|es|s)?\b|\bbest\b|\bclassement\b|\bpalmar[eè]s\b|\branking\b|\bliste des\b|\blist of\b|\bannuaire\b"
    r"|\bdirectory\b|\bcomparatif\b|\bbeste[nr]?\b|\bmejores\b|\bmigliori\b)",
    re.I,
)
_LISTICLE_PATH_RE = re.compile(r"/(top-\d+|meilleur|best-|classement|ranking|liste-)", re.I)

# Login-walled / social hosts: useful as people evidence (profile titles), useless to crawl.
SOCIAL_DOMAINS = frozenset(
    {
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "youtube.com",
        "pinterest.com",
        "threads.net",
    }
)


def is_listicle(result: SearchResult) -> bool:
    """ "Top 10 / meilleures agences / best X" pages: lists of companies, not a company."""
    return bool(LISTICLE_RE.search(result.title)) or bool(_LISTICLE_PATH_RE.search(urlsplit(result.url).path))


def is_social(url: str | None) -> bool:
    return registrable_domain(url) in SOCIAL_DOMAINS if url else False


@dataclass
class Assessment:
    sufficient: bool
    reason: str
    usable: list[SearchResult] = field(default_factory=list)


Assessor = Callable[[Sequence[SearchResult]], Assessment]


def assess_companies(results: Sequence[SearchResult], *, min_companies: int) -> Assessment:
    """Distinct company domains (first result per domain kept) vs ``min_companies``."""
    from scout.discovery.common import candidate_domain  # discovery owns the non-company host lists

    seen: set[str] = set()
    usable: list[SearchResult] = []
    for r in results:
        if is_listicle(r):
            continue
        dom = candidate_domain(r.url)
        if not dom or dom in seen:
            continue
        seen.add(dom)
        usable.append(r)
    n = len(usable)
    if n >= min_companies:
        return Assessment(True, f"{n} company domain(s) ≥ {min_companies}", usable)
    return Assessment(
        False,
        f"only {n} company domain(s) < {min_companies} (directories, socials, media, listicles excluded)",
        usable,
    )


def _name_keys(names: Sequence[str]) -> list[str]:
    keys: list[str] = []
    for n in names:
        for k in (normalize_company_name(n), normalize_key(n)):
            if len(k) >= 3 and k not in keys:
                keys.append(k)
    return keys


def mentions_subject(result: SearchResult, *, names: Sequence[str], domain: str | None = None) -> bool:
    """True when the result is about the subject: hosted on its domain, or naming it (domain or name)."""
    dom = (domain or "").lower().strip() or None
    if dom and registrable_domain(result.url) == dom:
        return True
    hay = f" {normalize_key(f'{result.title} {result.snippet}')} "
    if dom:
        label = normalize_key(dom.split(".")[0])
        if len(label) >= 4 and f" {label} " in hay:
            return True
        if dom in (result.title + " " + result.snippet).lower():
            return True
    for key in _name_keys(names):
        if f" {key} " in hay:
            return True
        if len(key) >= 8 and fuzz.partial_ratio(key, hay) >= 92:
            return True
    return False


def assess_subject(
    results: Sequence[SearchResult],
    *,
    names: Sequence[str],
    domain: str | None = None,
    min_matching: int = 1,
) -> Assessment:
    """Results mentioning the target (company name or domain) vs ``min_matching``."""
    usable = [r for r in results if mentions_subject(r, names=names, domain=domain)]
    n = len(usable)
    subject = ", ".join([*(x for x in names if x), *([domain] if domain else [])]) or "the subject"
    if n >= min_matching:
        return Assessment(True, f"{n} result(s) mention {subject}", usable)
    return Assessment(False, f"{n} result(s) mention {subject} (< {min_matching})", usable)


def assess_any(results: Sequence[SearchResult], *, min_results: int = 1) -> Assessment:
    n = len(results)
    if n >= min_results:
        return Assessment(True, f"{n} result(s)", list(results))
    return Assessment(False, f"{n} result(s) < {min_results}", list(results))
