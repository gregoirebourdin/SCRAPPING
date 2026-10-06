"""Name-affinity guard (docs/EMAIL_ENGINE.md §Affinity).

An address is only ever attributed to a person when its local part is consistent with that
person's identity. Looking for "John Smith" and finding ``marie@acme.com`` must score ~0 and never
be attributed. The score (0–1) combines:

* name evidence — exact rendering of a vocabulary pattern from the person's name, or full
  first+last tokens, or last name + initial;
* the domain convention — a ``{first}@`` address is only convincing when the domain's dominant
  pattern is ``{first}`` (and no colleague shares the first name);
* conflicts — the local part matches a known colleague, or contains another first name;
* context — the page that published the address also names the person.

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from scout.email.lists import is_role_local_part
from scout.email.patterns import infer_pattern, name_parts
from scout.extract.names import is_known_first_name
from scout.util.text import ascii_fold

# Minimum affinity to attribute an address to a person at all.
ATTRIBUTE_MIN = 0.5
# Affinity at which a published address can be SAFE / an unverified guess LIKELY_SAFE.
STRONG = 0.85

_FULL_BOTH = frozenset(
    {"{first}.{last}", "{first}{last}", "{first}_{last}", "{first}-{last}", "{last}.{first}"}
)
_INITIAL_LAST = frozenset({"{f}{last}", "{f}.{last}", "{last}{f}"})
_FIRST_INITIAL = frozenset({"{first}{l}", "{first}.{l}"})
_SEP = re.compile(r"[._+\-]+")


@dataclass(frozen=True)
class Affinity:
    score: float
    pattern: str | None
    reason: str


def _tokens(local_part: str) -> list[str]:
    lp = ascii_fold(local_part.lower().split("+", 1)[0])
    return [t for t in _SEP.split(lp) if t]


def _colleague_owns(
    local_part: str, colleagues: Sequence[tuple[str | None, str | None]], first: str | None, last: str | None
) -> str | None:
    """Name of a colleague this local part clearly belongs to (and not to the person)."""
    me = name_parts(first, last)
    for cf, cl in colleagues:
        if not (cf and cl):
            continue
        cp = name_parts(cf, cl)
        if cp.first == me.first and cp.last == me.last:
            continue  # same person (homonym record)
        pat = infer_pattern(cf, cl, local_part)
        if pat is not None and pat not in ("{f}{l}", "{first}", "{last}"):
            return f"{cf} {cl}"
        toks = _tokens(local_part)
        if any(v in toks for v in cp.first) and any(v in toks for v in cp.last):
            return f"{cf} {cl}"
    return None


def name_affinity(
    local_part: str,
    first: str | None,
    last: str | None,
    *,
    dominant_pattern: str | None = None,
    dominant_share: float = 0.0,
    colleagues: Sequence[tuple[str | None, str | None]] = (),
    context_mentions_name: bool = False,
) -> Affinity:
    """How plausible it is that ``local_part@domain`` is this person's own mailbox (0–1)."""
    lp = local_part.strip().lower()
    if not lp:
        return Affinity(0.0, None, "Empty local part")
    if is_role_local_part(lp):
        return Affinity(0.05, None, "Role / shared mailbox, not a personal address")
    parts = name_parts(first, last)
    if parts.is_empty:
        return Affinity(0.0, None, "No name to compare with")

    owner = _colleague_owns(lp, colleagues, first, last)
    if owner:
        return Affinity(0.0, None, f"Address belongs to {owner}")

    bonus = 0.05 if context_mentions_name else 0.0
    pattern = infer_pattern(first, last, lp)
    dominant_here = pattern is not None and pattern == dominant_pattern and dominant_share >= 0.6
    if pattern in _FULL_BOTH:
        return Affinity(min(1.0, 0.97 + bonus), pattern, "Full first and last name")
    if pattern in _INITIAL_LAST:
        return Affinity(
            min(1.0, (0.93 if dominant_here else 0.88) + bonus), pattern, "First initial and last name"
        )
    if pattern in _FIRST_INITIAL:
        return Affinity(
            min(1.0, (0.88 if dominant_here else 0.8) + bonus), pattern, "First name and last initial"
        )
    if pattern == "{first}":
        shared = sum(1 for cf, _ in colleagues if cf and name_parts(cf, None).first[:1] == parts.first[:1])
        if shared:
            return Affinity(0.3, pattern, "First name only, shared with a colleague")
        base = 0.88 if dominant_here else 0.62
        return Affinity(
            min(1.0, base + bonus),
            pattern,
            "First name only" + (" (domain convention)" if dominant_here else ""),
        )
    if pattern == "{last}":
        base = 0.85 if dominant_here else 0.58
        return Affinity(
            min(1.0, base + bonus),
            pattern,
            "Last name only" + (" (domain convention)" if dominant_here else ""),
        )
    if pattern == "{f}{l}":
        base = 0.6 if dominant_here else 0.3
        return Affinity(base, pattern, "Initials only")

    toks = _tokens(lp)
    joined = "".join(toks)
    has_first = any(v in toks or (len(v) >= 3 and v.replace("-", "") in joined) for v in parts.first)
    has_last = any(v in toks or (len(v) >= 4 and v.replace("-", "") in joined) for v in parts.last)
    if has_first and has_last:
        return Affinity(min(1.0, 0.85 + bonus), None, "Contains first and last name")
    if has_last and any(t in parts.f for t in toks):
        return Affinity(min(1.0, 0.7 + bonus), None, "Contains last name and first initial")
    # Another person's first name in the address ("marie@" while looking for John) → conflict.
    for t in toks:
        if len(t) >= 3 and t not in parts.first and t not in parts.last and is_known_first_name(t):
            return Affinity(0.0, None, f'Address names someone else ("{t}")')
    if has_last:
        return Affinity(0.45, None, "Contains last name only")
    if has_first:
        return Affinity(0.35, None, "Contains first name only")
    return Affinity(0.05, None, "Unrelated to the person's name")
