"""Deterministic text primitives for enrichment: accent/case folding, word-boundary term matching,
evidence excerpts and verbatim-quote validation. No AI here."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from unidecode import unidecode

_ALNUM_RUN = re.compile(r"[a-z0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")
# Separators tolerated *between* the tokens of a multi-word term ("e-commerce" ~ "ecommerce" ~ "e commerce").
_SEP = r"[\s\-_./'’]*"
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\.\.\.\]")
MIN_QUOTE_CHARS = 8


def fold(text: str) -> str:
    """Lowercase ASCII fold: "Réseaux Sociaux" → "reseaux sociaux"."""
    if text.isascii():
        return text.lower()
    return unidecode(text).lower()


def fold_with_map(text: str) -> tuple[str, list[int] | None]:
    """Fold `text` and return a map folded-index → original-index (None when the fold is 1:1)."""
    if text.isascii():
        return text.lower(), None
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        f = ch.lower() if ord(ch) < 128 else unidecode(ch).lower()
        if f:
            out.append(f)
            idx.extend([i] * len(f))
    return "".join(out), idx


def tokens(text: str) -> list[str]:
    """Folded alphanumeric tokens."""
    return _ALNUM_RUN.findall(fold(text))


def normalize_for_compare(text: str) -> str:
    """Folded text with every non-alphanumeric run collapsed to one space (quote comparison)."""
    return _NON_ALNUM.sub(" ", fold(text)).strip()


@lru_cache(maxsize=8192)
def term_regex(term: str, *, plural: bool = True) -> re.Pattern[str] | None:
    """Word-boundary-aware regex over *folded* text; tolerant to separators between tokens and to
    a plural suffix on the last token. None for terms without alphanumerics."""
    toks = _ALNUM_RUN.findall(fold(term))
    if not toks:
        return None
    body = _SEP.join(re.escape(t) for t in toks)
    last = toks[-1]
    suffix = "(?:s|es|x)?" if plural and last.isalpha() and len(last) >= 3 else ""
    return re.compile(rf"(?<![a-z0-9]){body}{suffix}(?![a-z0-9])")


@dataclass(frozen=True)
class TermHit:
    term: str
    start: int  # offsets in the ORIGINAL text
    end: int


def find_terms(text: str, terms: Iterable[str], *, first_only: bool = False) -> list[TermHit]:
    """All occurrences of `terms` in `text` (case/accent-insensitive, word-boundary-aware), by position."""
    if not text:
        return []
    folded, idx = fold_with_map(text)
    hits: list[TermHit] = []
    for term in terms:
        rx = term_regex(term)
        if rx is None:
            continue
        for m in rx.finditer(folded):
            s, e = m.start(), m.end()
            if idx is not None:
                s, e = idx[s], idx[e - 1] + 1
            hits.append(TermHit(term=term, start=s, end=e))
            if first_only:
                break
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    return hits


def contains_any(text: str, terms: Iterable[str]) -> bool:
    folded = fold(text)
    for term in terms:
        rx = term_regex(term)
        if rx is not None and rx.search(folded):
            return True
    return False


def excerpt(text: str, start: int, end: int, width: int = 160) -> str:
    """≈`width` chars of context around [start, end), cut at word boundaries, whitespace collapsed."""
    span = end - start
    pad = max(20, (width - span) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    if a > 0:
        sp = text.find(" ", a, start)
        a = sp + 1 if sp != -1 else a
    if b < len(text):
        sp = text.rfind(" ", end, b)
        b = sp if sp != -1 else b
    core = _WS.sub(" ", text[a:b]).strip()
    return ("…" if a > 0 else "") + core + ("…" if b < len(text) else "")


def line_at(text: str, pos: int) -> str:
    """The line of `text` containing offset `pos`."""
    a = text.rfind("\n", 0, pos) + 1
    b = text.find("\n", pos)
    return text[a : b if b != -1 else len(text)]


def quote_in_text(quote: str | None, text: str) -> bool:
    """True when `quote` appears verbatim in `text`, ignoring case, accents, punctuation and whitespace.

    Ellipses in the quote split it into segments that must all appear, in order. Quotes shorter than
    MIN_QUOTE_CHARS (after normalization) never validate — they are not evidence.
    """
    if not quote or not text:
        return False
    hay = normalize_for_compare(text)
    segments = [normalize_for_compare(s) for s in _ELLIPSIS.split(quote)]
    segments = [s for s in segments if s]
    if not segments or sum(len(s) for s in segments) < MIN_QUOTE_CHARS:
        return False
    pos = 0
    for seg in segments:
        found = hay.find(seg, pos)
        if found == -1:
            return False
        pos = found + len(seg)
    return True


def dedupe(items: Sequence[str]) -> list[str]:
    """Order-preserving dedupe by folded form; drops empty strings."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = normalize_for_compare(it)
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out
