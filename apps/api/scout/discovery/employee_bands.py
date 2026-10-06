"""INSEE headcount bands ("tranches d'effectif salarié") ↔ numeric employee ranges.

The registry reports *salaried* headcount bands; we treat them as the company's employee range.
"""

from __future__ import annotations

import re

# code → (min, max) salaried employees; None max = open-ended.
TRANCHES: dict[str, tuple[int, int | None]] = {
    "NN": (0, 0),  # non-employer unit (no salaried staff during the reference year)
    "00": (0, 0),
    "01": (1, 2),
    "02": (3, 5),
    "03": (6, 9),
    "11": (10, 19),
    "12": (20, 49),
    "21": (50, 99),
    "22": (100, 199),
    "31": (200, 249),
    "32": (250, 499),
    "41": (500, 999),
    "42": (1000, 1999),
    "51": (2000, 4999),
    "52": (5000, 9999),
    "53": (10000, None),
}

EMPLOYER_TRANCHES: tuple[str, ...] = tuple(c for c in TRANCHES if c not in ("NN", "00"))


def range_for_tranche(code: str | None) -> tuple[int | None, int | None]:
    """INSEE code → (min, max). Unknown / missing codes → (None, None)."""
    if not code:
        return (None, None)
    rng = TRANCHES.get(str(code).strip().upper())
    if rng is None:
        return (None, None)
    return rng


def band_label(code: str | None) -> str | None:
    lo, hi = range_for_tranche(code)
    if lo is None:
        return None
    if hi is None:
        return f"{lo:,}+"
    return str(lo) if lo == hi else f"{lo:,}–{hi:,}"


def tranches_for_range(min_: int | None, max_: int | None) -> list[str]:
    """INSEE codes whose band overlaps [min_, max_]. Empty list = no filter.

    NN/00 (no salaried staff) are excluded as soon as ``min_ >= 1``.
    """
    if min_ is None and max_ is None:
        return []
    lo = 0 if min_ is None else max(0, min_)
    hi = max_
    out: list[str] = []
    for code, (b_lo, b_hi) in TRANCHES.items():
        if code in ("NN", "00") and lo >= 1:
            continue
        if hi is not None and b_lo > hi:
            continue
        if b_hi is not None and b_hi < lo:
            continue
        out.append(code)
    return out


def overlaps(rng: tuple[int | None, int | None], min_: int | None, max_: int | None) -> bool:
    """True when the (possibly partial) range ``rng`` can satisfy [min_, max_]. Unknown ranges pass."""
    lo, hi = rng
    if lo is None and hi is None:
        return True
    if max_ is not None and lo is not None and lo > max_:
        return False
    return not (min_ is not None and hi is not None and hi < min_)


_NUM = r"(\d[\d\s.,]*\s*[kK]?)"
_RANGE_RE = re.compile(rf"^{_NUM}\s*(?:-|–|—|to|à|a|bis|al|~)\s*{_NUM}$")
_BETWEEN_RE = re.compile(rf"^(?:between|entre|zwischen|tra|entre)\s+{_NUM}\s+(?:and|et|und|e|y)\s+{_NUM}$")
_PLUS_RE = re.compile(
    rf"^(>=|≥|>|over|more than|plus de|mehr als|más de|piu di|più di)?\s*{_NUM}\s*(\+|or more|et plus|ou plus|and more|und mehr)?$"
)
_STRICT_GT = {">", "over", "more than", "plus de", "mehr als", "más de", "piu di", "più di"}
_LESS_RE = re.compile(
    rf"^(<=|≤|<|under|less than|fewer than|moins de|up to|jusqu'à|jusqu a|bis zu|weniger als|menos de|meno di)\s*{_NUM}$"
)


def _num(s: str) -> int:
    s = s.strip().lower().replace(" ", "").replace(" ", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
        s = s.replace(",", ".")
        return int(float(s) * mult)
    return int(s.replace(",", "").replace(".", ""))


def parse_range(text: str | int | None) -> tuple[int | None, int | None]:
    """Parse '2-30', '2 à 30', '10+', '<50', '≤ 50', 'between 5 and 20', '1k+', 12 → (min, max).

    '<50' means up to 49; '≤50' / 'up to 50' means up to 50. Unparseable → (None, None).
    """
    if text is None:
        return (None, None)
    if isinstance(text, int):
        return (text, text)
    s = " ".join(
        str(text)
        .strip()
        .lower()
        .replace("employees", "")
        .replace("employés", "")
        .replace("salariés", "")
        .split()
    )
    s = s.strip()
    if not s:
        return (None, None)
    if m := _RANGE_RE.match(s) or _BETWEEN_RE.match(s):
        a, b = _num(m.group(1)), _num(m.group(2))
        return (min(a, b), max(a, b))
    if m := _LESS_RE.match(s):
        n = _num(m.group(2))
        strict = m.group(1) in (
            "<",
            "under",
            "less than",
            "fewer than",
            "moins de",
            "weniger als",
            "menos de",
            "meno di",
        )
        return (None, n - 1 if strict else n)
    if m := _PLUS_RE.match(s):
        prefix, n, suffix = m.group(1), _num(m.group(2)), m.group(3)
        if prefix or suffix:
            return (n + 1 if prefix in _STRICT_GT else n, None)
        return (n, n)
    return (None, None)
