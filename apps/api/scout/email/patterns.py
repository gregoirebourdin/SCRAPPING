"""Email local-part pattern vocabulary, name normalization, rendering, inference and priors.

Everything here is deterministic: candidates are *derived* from a real person's name and a
pattern; nothing is ever invented.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from scout.email.lists import is_role_local_part
from scout.util.text import ascii_fold

PATTERNS: list[str] = [
    "{first}.{last}",
    "{first}",
    "{f}{last}",
    "{first}{last}",
    "{f}.{last}",
    "{last}.{first}",
    "{first}_{last}",
    "{first}-{last}",
    "{last}",
    "{first}{l}",
    "{last}{f}",
    "{f}{l}",
    "{first}.{l}",
]
PATTERN_SET: frozenset[str] = frozenset(PATTERNS)

# Name particles merged into / stripped from last names ("de la Fontaine" → delafontaine | fontaine).
PARTICLES: frozenset[str] = frozenset(
    {"de", "du", "des", "la", "le", "les", "d", "l", "van", "von", "der", "den", "di", "da", "del", "della",
     "dos", "das", "do", "ten", "ter", "zu", "y", "mac", "st"}
)

_FIELD = re.compile(r"\{(first|last|f|l)\}")
_APOSTROPHES = re.compile(r"[’‘`´ʼ]")
_NON_NAME = re.compile(r"[^a-z0-9' -]+")
_ELISION = re.compile(r"\b([dl])'")
_VALID_LOCAL = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_SEPARATORS = re.compile(r"[._-]+")

# Variant decay: secondary spellings of a compound name are less likely than the primary one.
VARIANT_DECAY = 0.6


@dataclass(frozen=True)
class NameParts:
    """Ascii-folded, lowercase spelling variants of a person's name (primary variant first)."""

    first: tuple[str, ...]
    last: tuple[str, ...]
    f: tuple[str, ...]       # first-name initials ("j", "jp" for Jean-Pierre)
    l: tuple[str, ...]       # noqa: E741 — last-name initials, one per last-name variant

    @property
    def is_empty(self) -> bool:
        return not self.first and not self.last


def _clean(raw: str | None) -> str:
    if not raw:
        return ""
    s = _APOSTROPHES.sub("'", raw)
    s = ascii_fold(s).lower().replace(".", " ")
    return _NON_NAME.sub(" ", s).strip()


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))


def _first_variants(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    words = _clean(raw).replace("'", "").split()
    if not words:
        return (), ()
    if len(words) > 2:
        # Registry style "Jean Pierre Marie": the usual first name is the first one.
        words = words[:1]
    tokens = [t for w in words for t in w.split("-") if t]
    if not tokens:
        return (), ()
    if len(tokens) == 1:
        return (tokens[0],), (tokens[0][0],)
    initials = (tokens[0][0], "".join(t[0] for t in tokens))
    if len(words) == 1:  # hyphenated compound: Jean-Pierre
        return _dedupe(["-".join(tokens), "".join(tokens)]), _dedupe(initials)
    # "Jean Pierre": either the first given name only, or a compound written with a space.
    return _dedupe([tokens[0], "-".join(tokens), "".join(tokens)]), _dedupe(initials)


def _core_variants(words: list[str]) -> list[str]:
    tokens = [t for w in words for t in w.split("-") if t]
    if not tokens:
        return []
    if len(tokens) == 1:
        return [tokens[0]]
    hyphenated = any("-" in w for w in words)
    joined, dashed = "".join(tokens), "-".join(tokens)
    return [dashed, joined, tokens[0]] if hyphenated else [joined, dashed, tokens[0]]


def _last_variants(raw: str | None) -> tuple[str, ...]:
    s = _ELISION.sub(r"\1 ", _clean(raw)).replace("'", "")
    words = s.split()
    particles: list[str] = []
    while len(words) > 1 and words[0] in PARTICLES:
        particles.append(words.pop(0))
    core = _core_variants(words)
    if not core:
        return ()
    if not particles:
        return _dedupe(core)
    core_tokens = [t for w in words for t in w.split("-") if t]
    return _dedupe(["".join(particles + core_tokens), *core])


@lru_cache(maxsize=4096)
def name_parts(first: str | None, last: str | None) -> NameParts:
    """Normalized spelling variants for a first/last name pair.

    "Jean-Pierre" → first ("jean-pierre", "jeanpierre"), initials ("j", "jp");
    "de la Fontaine" → last ("delafontaine", "fontaine"); "O'Brien" → ("obrien",).
    """
    fv, fi = _first_variants(first)
    lv = _last_variants(last)
    return NameParts(first=fv, last=lv, f=fi, l=_dedupe(v[0] for v in lv))


def _is_valid_local(local: str) -> bool:
    return 0 < len(local) <= 64 and ".." not in local and _VALID_LOCAL.fullmatch(local) is not None


def render_parts(pattern: str, parts: NameParts) -> list[str]:
    """Local parts for `pattern`, most likely spelling first; [] when a needed name part is missing."""
    fields = set(_FIELD.findall(pattern))
    if not fields or _FIELD.sub("", pattern) not in {"", ".", "_", "-"}:
        return []
    first_axis = parts.first if "first" in fields else parts.f if "f" in fields else ("",)
    last_axis = parts.last if "last" in fields else parts.l if "l" in fields else ("",)
    if not first_axis or not last_axis:
        return []
    combos = sorted(
        itertools.product(enumerate(first_axis), enumerate(last_axis)),
        key=lambda c: (c[0][0] + c[1][0], c[0][0]),
    )
    out: list[str] = []
    for (_, fv), (_, lv) in combos:
        local = (
            pattern.replace("{first}", fv).replace("{f}", fv).replace("{last}", lv).replace("{l}", lv)
        )
        if _is_valid_local(local) and local not in out:
            out.append(local)
    return out


def render(pattern: str, first: str | None, last: str | None) -> list[str]:
    """Local parts for `pattern` and a person's name (variants for compound names)."""
    return render_parts(pattern, name_parts(first, last))


def _bare_local(local_part: str) -> str:
    return local_part.strip().lower().split("+", 1)[0]


def infer_pattern(first: str | None, last: str | None, local_part: str) -> str | None:
    """The vocabulary pattern that produces `local_part` for this person, or None."""
    lp = _bare_local(local_part)
    if not lp:
        return None
    parts = name_parts(first, last)
    for pattern in PATTERNS:
        if lp in render_parts(pattern, parts):
            return pattern
    return None


def infer_patterns(samples: Sequence[tuple[str, str, str]]) -> dict[str, int]:
    """Count patterns over (first, last, local_part) samples; unmatched samples are ignored."""
    counts: Counter[str] = Counter()
    for first, last, local in samples:
        p = infer_pattern(first, last, local)
        if p:
            counts[p] += 1
    return dict(counts.most_common())


def pattern_confidence(samples: int, successes: int = 0, failures: int = 0) -> float:
    """Confidence that a domain uses a pattern, from named samples, SMTP successes and failures.

    1 sample → 0.70, 2 → 0.85, 3 → 0.925, … capped at 0.97; failures dilute the evidence.
    """
    n = max(0, samples) + max(0, successes)
    if n <= 0:
        return 0.0
    base = min(0.97, 1.0 - 0.3 * 0.5 ** (n - 1))
    penalty = n / (n + 2.0 * max(0, failures))
    return round(base * penalty, 4)


# ---- inference from addresses without names ------------------------------------------------

_SHAPE_CAP = 0.75  # without names, {first}.{last} and {last}.{first} are indistinguishable


def _shape_weights(local_part: str) -> dict[str, float]:
    lp = _bare_local(local_part)
    if not lp or is_role_local_part(lp) or any(c.isdigit() for c in lp):
        return {}
    tokens = [t for t in _SEPARATORS.split(lp) if t]
    sep = next((c for c in lp if c in "._-"), "")
    if len(tokens) == 2:
        a, b = tokens
        if sep == ".":
            if len(a) == 1 and len(b) > 1:
                return {"{f}.{last}": 1.0}
            if len(b) == 1 and len(a) > 1:
                return {"{first}.{l}": 1.0}
            if len(a) > 1 and len(b) > 1:
                return {"{first}.{last}": 1.0}
        if sep == "_" and len(a) > 1 and len(b) > 1:
            return {"{first}_{last}": 1.0}
        if sep == "-" and len(a) > 1 and len(b) > 1:
            # Ambiguous with a compound first name ("jean-pierre@").
            return {"{first}-{last}": 0.5, "{first}": 0.3}
        return {}
    if len(tokens) == 1 and len(lp) >= 2:
        if len(lp) == 2:
            return {"{f}{l}": 0.6}
        if len(lp) <= 6:
            return {"{first}": 0.6, "{f}{last}": 0.3}
        if len(lp) >= 11:
            return {"{first}{last}": 0.5, "{f}{last}": 0.3}
        return {"{f}{last}": 0.4, "{first}": 0.35, "{first}{last}": 0.15}
    return {}


def infer_from_local_parts(local_parts: Iterable[str]) -> list[tuple[str, float, int]]:
    """Weak pattern evidence from nameless addresses at a domain → [(pattern, confidence, count)].

    Confidence is capped at 0.75: shapes cannot tell first from last name.
    """
    weights: dict[str, float] = {}
    counts: Counter[str] = Counter()
    for lp in dict.fromkeys(local_parts):
        for pattern, w in _shape_weights(lp).items():
            weights[pattern] = weights.get(pattern, 0.0) + w
            counts[pattern] += 1
    total = sum(weights.values())
    if total <= 0:
        return []
    out = []
    for pattern, w in weights.items():
        share = w / total
        conf = min(_SHAPE_CAP, 0.35 + 0.2 * w) * share
        out.append((pattern, round(conf, 4), counts[pattern]))
    return sorted(out, key=lambda t: -t[1])


# ---- priors ----------------------------------------------------------------------------------

# Share of business domains using each pattern (B2B mailbox surveys; Europe-weighted).
GLOBAL_PRIORS: dict[str, float] = {
    "{first}.{last}": 0.41,
    "{first}": 0.20,
    "{f}{last}": 0.13,
    "{first}{last}": 0.06,
    "{f}.{last}": 0.05,
    "{last}": 0.03,
    "{first}_{last}": 0.02,
    "{first}-{last}": 0.02,
    "{last}.{first}": 0.02,
    "{first}{l}": 0.02,
    "{last}{f}": 0.02,
    "{f}{l}": 0.01,
    "{first}.{l}": 0.01,
}

_EU_DOT = {"FR", "BE", "LU", "CH", "ES", "IT", "PT", "NL", "MC"}
_GERMANIC = {"DE", "AT", "CH"}
_ANGLO = {"US", "GB", "UK", "CA", "AU", "NZ", "IE"}


def _weights(company_size_max: int | None, country: str | None) -> dict[str, float]:
    w = dict(GLOBAL_PRIORS)
    cc = (country or "").upper()
    if company_size_max is not None:
        if company_size_max <= 10:
            # Micro companies / agencies: first-name mailboxes dominate.
            w["{first}"] *= 2.4 if cc in _EU_DOT else 2.0
            w["{first}.{last}"] *= 0.8
            w["{f}{last}"] *= 0.6
        elif company_size_max <= 50:
            w["{first}"] *= 1.4
        elif company_size_max > 1000:
            w["{first}"] *= 0.2
            w["{first}.{last}"] *= 1.25
            w["{f}{last}"] *= 1.15
        elif company_size_max > 250:
            w["{first}"] *= 0.35
            w["{first}.{last}"] *= 1.2
            w["{f}{last}"] *= 1.1
    if cc in _EU_DOT:
        w["{first}.{last}"] *= 1.15
        w["{f}{last}"] *= 0.7
    if cc in _GERMANIC:
        w["{f}.{last}"] *= 1.6
        w["{first}.{last}"] *= 1.2
    if cc in _ANGLO:
        w["{f}{last}"] *= 1.6
        w["{first}{l}"] *= 1.3
    return w


@lru_cache(maxsize=256)
def _normalized_priors(company_size_max: int | None, country: str | None) -> tuple[tuple[str, float], ...]:
    w = _weights(company_size_max, country)
    total = sum(w.values())
    ranked = sorted(((p, v / total) for p, v in w.items()), key=lambda t: (-t[1], PATTERNS.index(t[0])))
    return tuple((p, round(v, 4)) for p, v in ranked)


def ranked_priors(*, company_size_max: int | None = None, country: str | None = None) -> list[tuple[str, float]]:
    """All vocabulary patterns with their context-adjusted prior, most likely first (sums to ~1)."""
    return list(_normalized_priors(company_size_max, (country or "").upper() or None))


def prior_for(pattern: str, *, company_size_max: int | None = None, country: str | None = None) -> float:
    """Prior probability that a company of this size/country uses `pattern` (0 if unknown)."""
    return dict(ranked_priors(company_size_max=company_size_max, country=country)).get(pattern, 0.0)
