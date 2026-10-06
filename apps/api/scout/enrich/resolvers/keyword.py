"""Keyword strategy: case/accent-insensitive, word-boundary-aware matching over all cached pages (no AI)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scout.db.enums import ColumnDataType
from scout.enrich.matching import TermHit, excerpt, find_terms, tokens
from scout.enrich.resolvers.base import NOT_CRAWLED, ResolveContext, ok, ordered_pages, unknown
from scout.enrich.types import CellResult

RESOLVER = "keyword"
ABSENCE_CONFIDENCE = 0.9


@dataclass
class PageHit:
    page: Any
    hit: TermHit
    text: str

    @property
    def snippet(self) -> str:
        return excerpt(self.text, self.hit.start, self.hit.end, 160)


def _texts(page: Any) -> list[str]:
    return [
        t
        for t in (
            getattr(page, "content_text", None),
            getattr(page, "title", None),
            getattr(page, "meta_description", None),
        )
        if t
    ]


def term_groups(terms: Sequence[str]) -> dict[str, list[str]]:
    """Variants of one term ("ManyChat" / "Many Chat") collapse into one group."""
    groups: dict[str, list[str]] = {}
    for t in terms:
        key = "".join(tokens(t))
        if key:
            groups.setdefault(key, []).append(t)
    return groups


def scan_pages(pages: Sequence[Any], terms: Sequence[str]) -> dict[str, PageHit]:
    """First hit per term, scanning pages home → services → … (title/meta included)."""
    found: dict[str, PageHit] = {}
    for page in ordered_pages(pages):
        pending = [t for t in terms if t not in found]
        if not pending:
            break
        for text in _texts(page):
            for h in find_terms(text, pending):
                if h.term not in found:
                    found[h.term] = PageHit(page=page, hit=h, text=text)
    return found


@dataclass
class KeywordOutcome:
    passed: bool | None  # None → site not crawled
    hits: list[PageHit]
    missing: list[str]


def match_keywords(pages: Sequence[Any], terms: Sequence[str], *, match_all: bool = False) -> KeywordOutcome:
    """Shared by the keyword resolver and website conditions."""
    groups = term_groups(terms)
    if not pages:
        return KeywordOutcome(passed=None, hits=[], missing=list(groups))
    found = scan_pages(pages, [t for v in groups.values() for t in v])
    hits: list[PageHit] = []
    missing: list[str] = []
    for variants in groups.values():
        hit = next((found[v] for v in variants if v in found), None)
        if hit is None:
            missing.append(variants[0])
        else:
            hits.append(hit)
    passed = (not missing) if match_all else bool(hits)
    return KeywordOutcome(passed=passed, hits=hits, missing=missing)


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    terms = [t for t in (plan.keywords or ([plan.concept] if plan.concept else [])) if t and t.strip()]
    if not terms:
        return unknown(plan, resolver=RESOLVER, error="No keyword configured")
    if not rc.pages:
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    match_all = plan.field == "all"
    out = match_keywords(rc.pages, terms, match_all=match_all)
    boolean = plan.data_type == ColumnDataType.boolean
    if out.passed:
        first = out.hits[0]
        evidence = " | ".join(h.snippet for h in out.hits[:3]) if match_all else first.snippet
        value: Any = True if boolean else first.snippet
        return ok(
            plan,
            value,
            resolver=RESOLVER,
            confidence=1.0,
            evidence=evidence,
            source_url=getattr(first.page, "url", None),
        )
    names = ", ".join(f"“{t}”" for t in (out.missing or terms)[:4])
    evidence = f"No mention of {names} across {len(rc.pages)} crawled pages"
    if match_all and out.hits:
        evidence = f"Found {', '.join(h.hit.term for h in out.hits)} but not {names}"
    if boolean:
        return ok(plan, False, resolver=RESOLVER, confidence=ABSENCE_CONFIDENCE, evidence=evidence)
    return unknown(plan, resolver=RESOLVER, evidence=evidence, source_id="website")
