"""Shared helpers for AI resolvers: passage retrieval, untrusted wrapping, evidence verification."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from scout.ai.prompts import wrap_untrusted
from scout.enrich.chunks import Passage, expand_terms, page_priors_for, passages_for_pages, rank_passages
from scout.enrich.matching import quote_in_text
from scout.enrich.resolvers.base import ResolveContext

INSUFFICIENT = "AI enrichment: insufficient evidence"
UNVERIFIED_CAP = 0.5


def relevant_passages(
    rc: ResolveContext, *, k: int = 6, max_chars: int = 6000, include_unmatched: bool = False
) -> list[Passage]:
    plan = rc.plan
    terms = expand_terms(plan.concept or plan.name, plan.keywords)
    priors = page_priors_for(plan.concept or plan.name, plan.input_sources)
    return rank_passages(
        passages_for_pages(rc.pages),
        terms,
        page_priors=priors,
        k=k,
        max_chars=max_chars,
        include_unmatched=include_unmatched,
    )


def passages_block(passages: Sequence[Passage]) -> str:
    return "\n\n".join(wrap_untrusted(p.text, source=p.page_url) for p in passages)


def subject_line(rc: ResolveContext) -> str:
    c = rc.company
    if c is None:
        return f"Lead: {rc.subject_name}"
    domain = c.domain or c.normalized_domain or c.website_url or "unknown domain"
    return f"Company: {c.name} ({domain})"


def _norm_url(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}"


def verify_quote(
    quote: str | None, source_url: str | None, passages: Sequence[Passage]
) -> tuple[bool, str | None]:
    """(valid, url): the quote must appear verbatim (case/accent/whitespace-insensitive) in a passage —
    the cited one first; when it is found in another passage the citation is corrected."""
    if not quote:
        return False, source_url
    target = _norm_url(source_url)
    cited = [p for p in passages if target and _norm_url(p.page_url) == target]
    for group in (cited, passages):
        for p in group:
            if quote_in_text(quote, p.text):
                return True, p.page_url
    return False, source_url
