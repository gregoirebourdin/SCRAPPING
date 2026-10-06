"""Deterministic company-fit helpers (industry match) — no AI."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from scout.util.text import normalize_key


@dataclass
class IndustryMatch:
    score: float
    label: str | None
    evidence: str | None


def _profiles(industries: Iterable[str]) -> list:
    try:
        from scout.discovery.taxonomy import match_industries  # type: ignore[import-not-found]
    except Exception:
        return []
    out = []
    for ind in industries:
        out.extend(match_industries(ind)[:1])
    return out


def _terms_for(industries: list[str]) -> tuple[set[str], set[str]]:
    """(labels, naf_codes) across all requested industries, lowercase/accent-folded."""
    labels: set[str] = {normalize_key(i) for i in industries}
    naf: set[str] = set()
    for p in _profiles(industries):
        for lang_labels in getattr(p, "labels", {}).values():
            labels.update(normalize_key(x) for x in lang_labels)
        for lang_kw in getattr(p, "search_keywords", {}).values():
            labels.update(normalize_key(x) for x in lang_kw)
        naf.update(getattr(p, "naf_codes", []) or [])
    # singular/plural and "agency"/"agence" variants
    extra = set()
    for lab in labels:
        if lab.endswith("ies"):
            extra.add(lab[:-3] + "y")
        if lab.endswith("s"):
            extra.add(lab[:-1])
    return {x for x in labels | extra if len(x) >= 3}, naf


def industry_fit(
    industries: list[str],
    *,
    category: str | None,
    industry: str | None,
    name: str | None,
    description: str | None,
    page_text: str | None,
) -> IndustryMatch | None:
    if not industries:
        return None
    labels, naf_codes = _terms_for(industries)
    cat = (category or "") + " " + (industry or "")
    for code in naf_codes:
        if code and code in cat:
            return IndustryMatch(1.0, industries[0], f"Activity code {code}")
    cat_n = normalize_key(cat)
    for lab in sorted(labels, key=len, reverse=True):
        if lab and lab in cat_n:
            return IndustryMatch(0.9, industries[0], f"Category: {cat.strip()[:120]}")
    name_n = normalize_key(name or "")
    hay = normalize_key(" ".join(x for x in (description or "", (page_text or "")[:20000]) if x))
    hits = [lab for lab in labels if lab in hay or lab in name_n]
    if len(hits) >= 2:
        return IndustryMatch(0.8, industries[0], f"Website mentions: {', '.join(sorted(hits)[:3])}")
    if hits:
        return IndustryMatch(0.6, industries[0], f"Website mentions: {hits[0]}")
    return IndustryMatch(0.2 if (page_text or description or category) else 0.4, None, None)
