"""Benchmark metrics — pure, deterministic functions (no I/O).

Matching rules
--------------
* **Domains** — registrable domain (eTLD+1), lowercase, IDNA; URLs, hosts and e-mail addresses accepted
  (``https://www.Acme.fr/contact`` ≡ ``acme.fr``).
* **Company names** (only when no domain is expected) — legal forms removed, accents folded, punctuation
  collapsed (``Acme SAS`` ≡ ``ACME``).
* **Person names** — tokens of ``first + last`` lowercased, accent-free, punctuation and hyphens as spaces,
  courtesy titles dropped (``Élodie Lefèbvre-Durand`` ≡ ``elodie lefebvre durand``). Two names match when
  their token sets are equal (order-insensitive: ``DUPONT Marie`` ≡ ``Marie Dupont``) or when the smaller
  set has ≥ 2 tokens and is contained in the larger one (middle names). Matching is one-to-one, greedy in
  ground-truth order.
* **Roles** — both titles go through the production title normalizer (``scout.extract.titles``); a role
  is correct when the canonical titles are equal, when both are top decision makers (``Gérant`` ≡ ``CEO``
  ≡ ``Fondatrice``), or when role family *and* seniority band are equal — exec (owner, C-level), leader
  (VP, director, head), manager, individual — (``Directeur commercial`` ≡ ``Head of Sales``;
  ``CTO`` ≠ ``Directeur technique``; ``Head of Sales`` ≠ ``Sales Assistant``).
* **E-mails** — trimmed, ``mailto:`` removed, case-insensitive.
* **Enrichment values** — booleans exact (``true/yes/oui/1`` ↔ ``false/no/non/0``); numbers within a relative
  tolerance (default 5 %); enums normalized-equal; URLs compared without scheme / ``www`` / trailing
  slash; text normalized-equal or token-set similarity ≥ 90/100; lists compared as sets.

Rates are reported with their sample size ``n`` (successes ``k``) and a Wilson score interval at 90 %.
Nothing here compares Research with another product: only measured numbers on the given ground truth.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from scout.util.text import normalize_company_name, normalize_key
from scout.util.urls import registrable_domain

Z90 = 1.6448536269514722  # two-sided 90 % normal quantile

DELIVERABLE = frozenset({"SAFE", "LIKELY_SAFE"})
EMAIL_STATUSES = frozenset(
    {"SAFE", "LIKELY_SAFE", "RISKY", "CATCH_ALL", "UNKNOWN", "TEMPORARY_UNKNOWN", "INVALID"}
)
_STATUS_ALIASES = {
    "valid": "SAFE",
    "ok": "SAFE",
    "deliverable": "SAFE",
    "verified": "SAFE",
    "likely": "LIKELY_SAFE",
    "likely_valid": "LIKELY_SAFE",
    "probable": "LIKELY_SAFE",
    "invalid": "INVALID",
    "bounce": "INVALID",
    "bounced": "INVALID",
    "undeliverable": "INVALID",
    "no_mailbox": "INVALID",
    "none": "INVALID",
    "catch_all": "CATCH_ALL",
    "catchall": "CATCH_ALL",
    "accept_all": "CATCH_ALL",
    "risky": "RISKY",
    "unknown": "UNKNOWN",
}
_HONORIFICS = frozenset(
    {"m", "mr", "mrs", "ms", "mme", "mlle", "dr", "pr", "prof", "sir", "madame", "monsieur"}
)
_TRUE = frozenset({"true", "yes", "y", "oui", "vrai", "1", "si", "ja"})
_FALSE = frozenset({"false", "no", "n", "non", "faux", "0", "nein"})
_PATTERN_ALIASES = {
    "first": "{first}",
    "prenom": "{first}",
    "firstname": "{first}",
    "last": "{last}",
    "nom": "{last}",
    "lastname": "{last}",
    "f": "{f}",
    "l": "{l}",
}

# =============================================================================================
# Statistics
# =============================================================================================


def wilson(k: int, n: int, z: float = Z90) -> tuple[float, float] | None:
    """Wilson score interval for k successes out of n (None when n == 0)."""
    if n <= 0:
        return None
    k = max(0, min(k, n))
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (q in [0, 100]); None for an empty sample."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * max(0.0, min(100.0, q)) / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def rate(k: int, n: int) -> dict[str, Any]:
    """A measured rate: value, successes, sample size and Wilson 90 % interval (helper for suites)."""
    ci = wilson(k, n)
    return {
        "value": round(k / n, 4) if n else None,
        "k": int(k),
        "n": int(n),
        "ci90": list(ci) if ci else None,
    }


def measure(value: float | int | None, n: int | None = None) -> dict[str, Any]:
    """A measured non-rate number with its sample size (helper for suites)."""
    return {"value": value, "n": n}


# =============================================================================================
# Normalization & matching
# =============================================================================================


def norm_domain(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return registrable_domain(value.strip().lower())


def norm_email(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith("mailto:"):
        v = v[7:]
    v = v.split("?", 1)[0].strip()
    if v.count("@") != 1:
        return None
    local, dom = v.split("@", 1)
    if not local or "." not in dom:
        return None
    return v


def norm_status(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.upper() in EMAIL_STATUSES:
        return v.upper()
    return _STATUS_ALIASES.get(normalize_key(v).replace(" ", "_"))


def norm_company(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return normalize_company_name(value) or None


def norm_pattern(value: Any) -> str | None:
    """``first.last`` / ``prenom.nom`` / ``{first}.{last}`` → ``{first}.{last}``."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().lower()
    if "{" in v:
        return v
    out = re.sub(r"[a-z]+", lambda m: _PATTERN_ALIASES.get(m.group(0), m.group(0)), v)
    return out if "{" in out else None


def name_tokens(first: Any = None, last: Any = None, full: Any = None) -> tuple[str, ...]:
    raw = " ".join(str(x) for x in (first, last) if x) or (str(full) if full else "")
    toks = [t for t in normalize_key(raw).split() if t not in _HONORIFICS]
    return tuple(toks)


def names_match(a: Sequence[str], b: Sequence[str]) -> bool:
    if not a or not b:
        return False
    sa, sb = set(a), set(b)
    if sa == sb:
        return True
    small, large = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(small) >= 2 and small <= large


# Seniority bands for role comparison: same-level synonyms (Directeur commercial ≡ Head of Sales ≡ VP Sales).
_SENIORITY_BAND = {
    "owner": "exec",
    "c_level": "exec",
    "vp": "leader",
    "director": "leader",
    "head": "leader",
    "manager": "manager",
    "senior": "individual",
    "entry": "individual",
}


@lru_cache(maxsize=4096)
def role_of(title: str) -> tuple[str, str, str, bool]:
    """(canonical title, role family, seniority, top decision maker) from the production normalizer."""
    from scout.extract.titles import is_top_decision_maker, normalize_title

    info = normalize_title(title)
    return (
        info.normalized_title.lower(),
        str(getattr(info.role_family, "value", info.role_family)),
        str(getattr(info.seniority, "value", info.seniority)),
        is_top_decision_maker(info),
    )


def roles_match(expected: str, actual: str) -> bool:
    e, a = role_of(expected.strip()), role_of(actual.strip())
    if e[0] and e[0] == a[0]:
        return True
    if e[3] and a[3]:  # both head the company: Gérant ≡ CEO ≡ Founder ≡ Président
        return True
    if e[1] == "other" and e[2] == "unknown":  # unrecognized title: fall back to normalized text
        return normalize_key(expected) == normalize_key(actual)
    return e[1] == a[1] and _SENIORITY_BAND.get(e[2], e[2]) == _SENIORITY_BAND.get(a[2], a[2])


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str):
        k = normalize_key(v)
        if k in _TRUE:
            return True
        if k in _FALSE:
            return False
    return None


_NUM_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def _as_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace(" ", "").replace("\xa0", "").replace(" ", "")
    s = s.replace("€", "").replace("$", "").replace("%", "")
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    mult = 1.0
    if s[-1:].lower() in ("k", "m") and _NUM_RE.match(s[:-1] or "x"):
        mult = 1_000.0 if s[-1].lower() == "k" else 1_000_000.0
        s = s[:-1]
    return float(s) * mult if _NUM_RE.match(s) else None


def _looks_boolean(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool):
        return True
    return _as_number(expected) is None and _as_bool(expected) is not None and _as_bool(actual) is not None


def _norm_url(v: str) -> str:
    s = v.strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)
    s = s[4:] if s.startswith("www.") else s
    return s.rstrip("/")


def _text_equal(e: str, a: str) -> bool:
    ne, na = normalize_key(e), normalize_key(a)
    if ne == na:
        return True
    if not ne or not na:
        return False
    from rapidfuzz import fuzz

    return fuzz.token_set_ratio(ne, na) >= 90


def values_equal(expected: Any, actual: Any, data_type: str | None = None, *, rel_tol: float = 0.05) -> bool:
    """Type-aware equality for enrichment values (see module docstring)."""
    if actual is None or actual == "":
        return False
    dt = (data_type or "").lower()
    if dt == "boolean" or (not dt and _looks_boolean(expected, actual)):
        eb, ab = _as_bool(expected), _as_bool(actual)
        return eb is not None and eb == ab
    if dt == "number" or (not dt and _as_number(expected) is not None):
        en, an = _as_number(expected), _as_number(actual)
        if en is None or an is None:
            return False
        return abs(an - en) <= max(1e-9, rel_tol * abs(en))
    if isinstance(expected, list) or isinstance(actual, list):
        el = expected if isinstance(expected, list) else [expected]
        al = actual if isinstance(actual, list) else [actual]
        return {normalize_key(str(x)) for x in el} == {normalize_key(str(x)) for x in al}
    e, a = str(expected), str(actual)
    if dt == "enum":
        return normalize_key(e) == normalize_key(a)
    if dt == "email":
        return norm_email(e) is not None and norm_email(e) == norm_email(a)
    if dt == "url" or (not dt and re.match(r"^(https?://|www\.)", e.strip().lower())):
        return _norm_url(e) == _norm_url(a)
    return _text_equal(e, a)


# =============================================================================================
# Per-item comparison
# =============================================================================================


def _given_company(expected: Mapping[str, Any], given: Mapping[str, Any]) -> bool:
    exp_dom = norm_domain(expected.get("domain"))
    if exp_dom:
        return norm_domain(given.get("domain")) == exp_dom
    return False


def compare_company(
    expected: Mapping[str, Any] | None, actual: Mapping[str, Any] | None, given: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Company identity. TP: same domain (or same normalized name when no domain is expected); a different
    identity is FP + FN; nothing found is FN. ``given`` when the input already carried the expected domain:
    such items are excluded from company precision / recall (identity was not discovered)."""
    if not expected:
        return None
    exp_dom = norm_domain(expected.get("domain"))
    exp_name = norm_company(expected.get("name"))
    if not exp_dom and not exp_name:
        return None
    out: dict[str, Any] = {
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "given": _given_company(expected, given or {}),
        "expected": exp_dom or expected.get("name"),
        "actual": None,
        "found": bool(actual and actual.get("found")),
    }
    if not actual or not actual.get("found"):
        out["fn"] = 1
        return out
    act_dom = norm_domain(actual.get("domain"))
    out["actual"] = act_dom or actual.get("name")
    if exp_dom:
        if act_dom is None:
            out["fn"] = 1
        elif act_dom == exp_dom:
            out["tp"] = 1
        else:
            out["fp"] = out["fn"] = 1
    elif norm_company(actual.get("name")) == exp_name:
        out["tp"] = 1
    else:
        out["fp"] = out["fn"] = 1
    return out


def _person_label(p: Mapping[str, Any]) -> str:
    return " ".join(str(x) for x in (p.get("first"), p.get("last")) if x) or str(p.get("full_name") or "")


def _tokens_of(p: Mapping[str, Any]) -> tuple[str, ...]:
    return name_tokens(p.get("first"), p.get("last"), p.get("full_name"))


def match_people(
    expected: Sequence[Mapping[str, Any]], actual: Sequence[Mapping[str, Any]]
) -> list[tuple[int, int]]:
    """One-to-one greedy matching in ground-truth order → [(expected index, actual index)]."""
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    act_tokens = [_tokens_of(a) for a in actual]
    for i, e in enumerate(expected):
        et = _tokens_of(e)
        exact = [j for j, at in enumerate(act_tokens) if j not in used and at and set(at) == set(et)]
        loose = [j for j, at in enumerate(act_tokens) if j not in used and names_match(et, at)]
        candidates = exact or loose
        if candidates:
            pick = candidates[0]
            used.add(pick)
            pairs.append((i, pick))
    return pairs


def _duplicates(token_lists: Sequence[tuple[str, ...]]) -> int:
    dup = 0
    seen: list[tuple[str, ...]] = []
    for t in token_lists:
        if t and any(names_match(t, s) for s in seen):
            dup += 1
        else:
            seen.append(t)
    return dup


def compare_people(
    expected: Sequence[Mapping[str, Any]] | None,
    actual: Sequence[Mapping[str, Any]],
    *,
    exhaustive: bool = True,
    given: bool = False,
) -> tuple[dict[str, Any] | None, list[tuple[int, int]]]:
    """People identity + role. FP (unmatched actual people) only counts when the ground truth lists every
    expected person of the company (``exhaustive``); otherwise they are reported as ``unscored``."""
    if expected is None:
        return None, []
    pairs = match_people(expected, actual)
    matched_actual = {j for _, j in pairs}
    unmatched_actual = [a for j, a in enumerate(actual) if j not in matched_actual]
    roles: list[dict[str, Any]] = []
    out_pairs: list[dict[str, Any]] = []
    for i, j in pairs:
        e, a = expected[i], actual[j]
        role = None
        et, at = (e.get("title") or "").strip(), (a.get("title") or "").strip()
        if et and at and not given:
            role = {"expected": et, "actual": at, "correct": roles_match(et, at)}
            roles.append(role)
        elif et and not at and not given:
            role = {"expected": et, "actual": None, "correct": None}
        out_pairs.append({"expected": _person_label(e), "actual": _person_label(a), "role": role})
    matched_expected = {i for i, _ in pairs}
    tp = len(pairs)
    fn = len(expected) - tp
    fp = len(unmatched_actual) if exhaustive else 0
    return (
        {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "given": given,
            "exhaustive": exhaustive,
            "pairs": out_pairs,
            "missing": [_person_label(e) for i, e in enumerate(expected) if i not in matched_expected],
            "spurious": [_person_label(a) for a in unmatched_actual] if exhaustive else [],
            "unscored": 0 if exhaustive else len(unmatched_actual),
            "roles_evaluated": len(roles),
            "roles_correct": sum(1 for r in roles if r["correct"]),
            "actual_count": len(actual),
            "duplicates": _duplicates([_tokens_of(a) for a in actual]),
        },
        pairs,
    )


def infer_expected_pattern(company: Mapping[str, Any], emails: Sequence[Mapping[str, Any]]) -> str | None:
    """Expected domain pattern: explicit ``email_pattern``, else the majority pattern of expected named
    addresses (via the production pattern vocabulary)."""
    explicit = norm_pattern(company.get("email_pattern"))
    if explicit:
        return explicit
    from scout.email.patterns import infer_pattern

    counts: Counter[str] = Counter()
    for e in emails:
        addr = norm_email(e.get("address"))
        if not addr or norm_status(e.get("status")) == "INVALID" or not (e.get("first") and e.get("last")):
            continue
        p = infer_pattern(e.get("first"), e.get("last"), addr.split("@", 1)[0])
        if p:
            counts[p] += 1
    if not counts:
        return None
    (best, n), *rest = counts.most_common()
    return best if not rest or rest[0][1] < n else None  # ties → unknown truth


def compare_emails(
    expected_emails: Sequence[Mapping[str, Any]] | None,
    expected_people: Sequence[Mapping[str, Any]] | None,
    actual_people: Sequence[Mapping[str, Any]],
    company_emails: Sequence[Mapping[str, Any]],
    pairs: Sequence[tuple[int, int]],
) -> dict[str, Any] | None:
    """Per expected address: ``correct`` (TP) · ``wrong`` (another address claimed: FP + FN) · ``missing``
    (FN) · ``invalid_fp`` (an address known not to exist was graded SAFE / LIKELY_SAFE) · ``invalid_ok``.
    A claim is any returned address whose status is not INVALID; a claim on a known-invalid mailbox is a
    wrong claim (FP, and counted in the precision denominators)."""
    if not expected_emails:
        return None
    exp_people = list(expected_people or [])
    by_expected = dict(pairs)
    rows: list[dict[str, Any]] = []
    tp = fp = fn = 0
    company_set = {
        norm_email(x.get("address")) for x in company_emails if norm_status(x.get("status")) != "INVALID"
    }
    for e in expected_emails:
        exp_addr = norm_email(e.get("address"))
        exp_status = norm_status(e.get("status"))
        person_tokens = name_tokens(e.get("first"), e.get("last"))
        act: Mapping[str, Any] | None = None
        linked = bool(person_tokens)
        if linked:
            idx = next(
                (i for i, p in enumerate(exp_people) if names_match(_tokens_of(p), person_tokens)), None
            )
            j = by_expected.get(idx) if idx is not None else None
            if j is None:  # person given as an email row only: match it directly among actual people
                j = next(
                    (k for k, a in enumerate(actual_people) if names_match(_tokens_of(a), person_tokens)),
                    None,
                )
            act = (actual_people[j].get("email") or None) if j is not None else None
        act_addr = norm_email(act.get("address")) if act else None
        act_status = norm_status(act.get("status")) if act else None
        row: dict[str, Any] = {
            "person": " ".join(str(x) for x in (e.get("first"), e.get("last")) if x) or None,
            "expected": exp_addr,
            "expected_status": exp_status,
            "actual": act_addr,
            "actual_status": act_status,
            "resolver": act.get("resolver") if act else None,
        }
        if exp_status == "INVALID":
            # Evaluable claim: an address for a person known to have no mailbox, or the known-invalid address
            # itself (another address for that person has no truth and is not scored).
            wrong_claim = bool(
                act_addr and act_status != "INVALID" and (exp_addr is None or act_addr == exp_addr)
            )
            claimed = wrong_claim and act_status in DELIVERABLE
            row["verdict"] = "invalid_fp" if claimed else "invalid_ok"
            row["wrong_claim"] = wrong_claim
            fp += int(wrong_claim)
            rows.append(row)
            continue
        if not exp_addr:
            continue
        if not linked:
            row["verdict"] = "correct" if exp_addr in company_set else "missing"
            row["company_level"] = True
            tp += row["verdict"] == "correct"
            fn += row["verdict"] == "missing"
            rows.append(row)
            continue
        if act_addr is None or act_status == "INVALID":
            row["verdict"] = "missing"
            fn += 1
        elif act_addr == exp_addr:
            row["verdict"] = "correct"
            tp += 1
        else:
            row["verdict"] = "wrong"
            fp += 1
            fn += 1
        rows.append(row)
    addrs = [norm_email((a.get("email") or {}).get("address")) for a in actual_people]
    present = [a for a in addrs if a]
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "rows": rows,
        "duplicates": len(present) - len(set(present)),
        "actual_count": len(present),
    }


def compare_enrichment(
    expected: Mapping[str, Any] | None,
    actual: Mapping[str, Mapping[str, Any]] | None,
    *,
    rel_tol: float = 0.05,
) -> dict[str, Any] | None:
    """Per expected column value: ``correct`` · ``wrong`` (answered, different: FP + FN) · ``unknown``
    (no answer / failed / missing column: FN)."""
    if not expected:
        return None
    actual = actual or {}
    rows: list[dict[str, Any]] = []
    for key, exp_value in expected.items():
        if exp_value is None or exp_value == "":
            continue
        a = actual.get(key) or {"status": "missing"}
        answered = a.get("status") == "success" and a.get("value") not in (None, "")
        correct = answered and values_equal(exp_value, a.get("value"), a.get("data_type"), rel_tol=rel_tol)
        rows.append(
            {
                "key": key,
                "expected": exp_value,
                "actual": a.get("value"),
                "status": a.get("status"),
                "resolver": a.get("resolver"),
                "verdict": "correct" if correct else ("wrong" if answered else "unknown"),
            }
        )
    if not rows:
        return None
    correct_n = sum(r["verdict"] == "correct" for r in rows)
    wrong = sum(r["verdict"] == "wrong" for r in rows)
    return {
        "evaluated": len(rows),
        "answered": sum(r["verdict"] != "unknown" for r in rows),
        "correct": correct_n,
        "fp": wrong,
        "fn": len(rows) - correct_n,
        "rows": rows,
    }


def compare_item(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    given: Mapping[str, Any] | None = None,
    *,
    rel_tol: float = 0.05,
) -> dict[str, Any]:
    """All verdicts for one ground-truth item. ``given`` is the item's input (identity handed to the engine)."""
    given = given or {}
    exp_company = expected.get("company") or {}
    company = compare_company(exp_company, actual.get("company"), given.get("company") or {})
    actual_people = list(actual.get("people") or [])
    people, pairs = compare_people(
        expected.get("people"),
        actual_people,
        exhaustive=bool(expected.get("people_exhaustive", True)),
        given=bool(given.get("people")),
    )
    emails = compare_emails(
        expected.get("emails"),
        expected.get("people"),
        actual_people,
        list(actual.get("company_emails") or []),
        pairs,
    )
    enrichment = compare_enrichment(expected.get("enrichment"), actual.get("enrichment"), rel_tol=rel_tol)
    domain = actual.get("domain") or {}
    catch_truth = exp_company.get("catch_all")
    if not isinstance(catch_truth, bool):
        catch_truth = (
            True
            if any(norm_status(e.get("status")) == "CATCH_ALL" for e in expected.get("emails") or [])
            else None
        )
    catch_all = None
    if catch_truth is not None:
        act_catch = domain.get("catch_all")
        if act_catch is None and any(
            norm_status((p.get("email") or {}).get("status")) == "CATCH_ALL" for p in actual_people
        ):
            act_catch = True
        catch_all = {
            "expected": catch_truth,
            "actual": act_catch,
            "correct": isinstance(act_catch, bool) and act_catch == catch_truth,
        }
    pattern = None
    exp_pattern = infer_expected_pattern(exp_company, expected.get("emails") or [])
    if exp_pattern:
        act_pattern = norm_pattern(domain.get("pattern"))
        pattern = {"expected": exp_pattern, "actual": act_pattern, "correct": act_pattern == exp_pattern}
    qualified = sum(
        1
        for r in (emails or {}).get("rows", [])
        if r.get("verdict") == "correct"
        and not r.get("company_level")
        and r.get("actual_status") in DELIVERABLE
    )
    groups = [g for g in (company, people, emails, enrichment) if g]
    return {
        "company": company,
        "people": people,
        "emails": emails,
        "enrichment": enrichment,
        "catch_all": catch_all,
        "pattern": pattern,
        "email_resolutions": list(actual.get("email_resolutions") or []),
        "crawl": actual.get("crawl"),
        "qualified_leads": qualified,
        "fp": sum(int(g.get("fp", 0)) for g in groups),
        "fn": sum(int(g.get("fn", 0)) for g in groups),
    }


# =============================================================================================
# Aggregation
# =============================================================================================

# key → (label, group, unit, definition)
METRIC_DEFS: dict[str, tuple[str, str, str, str]] = {
    "company_precision": (
        "Company precision",
        "company",
        "rate",
        "Correct company identities / identities returned (same registrable domain; same normalized name when no domain is expected). Items whose input already carried the expected domain are excluded.",
    ),
    "company_recall": (
        "Company recall",
        "company",
        "rate",
        "Correct company identities / expected companies (identity not given as input).",
    ),
    "company_identity_given": (
        "Identities given as input",
        "company",
        "count",
        "Items whose input already carried the expected domain (excluded from company precision / recall).",
    ),
    "company_coverage": (
        "Registry coverage",
        "company",
        "rate",
        "Expected companies already present in the workspace registry (registry mode).",
    ),
    "crawl_success_rate": (
        "Crawl success",
        "company",
        "rate",
        "Companies whose website returned ≥ 1 page / companies crawled (live mode).",
    ),
    "person_precision": (
        "Person precision",
        "people",
        "rate",
        "Matched people / people returned, on companies whose ground truth lists every decision maker. Name match: accent/case/order-insensitive token sets (middle names tolerated).",
    ),
    "person_recall": ("Person recall", "people", "rate", "Matched people / expected people."),
    "role_precision": (
        "Role precision",
        "people",
        "rate",
        "Matched people whose title maps to the expected role (same canonical title, or same role family and seniority) / matched people with both titles.",
    ),
    "email_precision": (
        "Email precision",
        "email",
        "rate",
        "Correct addresses / addresses returned (any status except INVALID) for expected people.",
    ),
    "email_recall": (
        "Email recall (deliverable)",
        "email",
        "rate",
        "Expected addresses found and graded SAFE or LIKELY_SAFE / expected addresses.",
    ),
    "email_discovery_recall": (
        "Email discovery recall",
        "email",
        "rate",
        "Expected addresses found with any status except INVALID / expected addresses.",
    ),
    "safe_email_precision": ("SAFE precision", "email", "rate", "Correct addresses / addresses graded SAFE."),
    "deliverable_precision": (
        "SAFE + likely precision",
        "email",
        "rate",
        "Correct addresses / addresses graded SAFE or LIKELY_SAFE.",
    ),
    "invalid_false_positive_rate": (
        "Invalid false-positive rate",
        "email",
        "rate",
        "Addresses known not to exist that were graded SAFE or LIKELY_SAFE / known-invalid addresses. Lower is better.",
    ),
    "catch_all_accuracy": (
        "Catch-all accuracy",
        "email",
        "rate",
        "Domains whose catch-all verdict equals the truth / domains with a known truth (no verdict counts as wrong).",
    ),
    "domain_pattern_accuracy": (
        "Domain pattern accuracy",
        "email",
        "rate",
        "Domains whose dominant pattern equals the truth (explicit, or majority of expected named addresses) / domains with a known pattern.",
    ),
    "avg_email_resolution_ms": (
        "Avg email resolution",
        "email",
        "ms",
        "Mean resolution time per person (fast path + deep path when it ran).",
    ),
    "p50_email_resolution_ms": ("P50 email resolution", "email", "ms", "Median resolution time per person."),
    "p95_email_resolution_ms": (
        "P95 email resolution",
        "email",
        "ms",
        "95th percentile resolution time per person (linear interpolation).",
    ),
    "smtp_fallback_rate": (
        "SMTP fallback rate",
        "email",
        "rate",
        "Resolutions that needed the deep (SMTP) path / resolutions. Lower is cheaper.",
    ),
    "cache_hit_rate": (
        "Cache hit rate",
        "email",
        "rate",
        "Resolutions served at least partly from cache (address or domain profile) / resolutions.",
    ),
    "cost_per_email": ("Cost per email", "email", "usd", "Email-stage cost / addresses returned."),
    "emails_resolved_per_minute": (
        "Emails resolved / min",
        "email",
        "per_minute",
        "Addresses graded SAFE or LIKELY_SAFE per minute of run wall-clock time (live mode).",
    ),
    "enrichment_accuracy": (
        "Enrichment accuracy",
        "enrichment",
        "rate",
        "Correct column values / expected values (no answer counts as wrong). Booleans exact, numbers ±5 %, text normalized.",
    ),
    "enrichment_precision": (
        "Enrichment precision",
        "enrichment",
        "rate",
        "Correct column values / values answered.",
    ),
    "enrichment_coverage": (
        "Enrichment coverage",
        "enrichment",
        "rate",
        "Values answered (not unknown / failed) / expected values.",
    ),
    "false_positives": (
        "False positives",
        "quality",
        "count",
        "Wrong company identities + spurious people + wrong or falsely-safe emails + wrong enrichment values.",
    ),
    "false_negatives": (
        "False negatives",
        "quality",
        "count",
        "Missed companies + missed people + missed or wrong emails + unanswered or wrong enrichment values.",
    ),
    "duplicate_rate": (
        "Duplicate rate",
        "quality",
        "rate",
        "Duplicate people (same name tokens at one company) and shared addresses / people + addresses returned.",
    ),
    "items": ("Items", "operations", "count", "Ground-truth items processed."),
    "items_failed": (
        "Items with errors",
        "operations",
        "count",
        "Items whose processing raised an error (scored as misses).",
    ),
    "avg_processing_ms": ("Avg processing time", "operations", "ms", "Mean wall-clock time per item."),
    "p50_processing_ms": ("P50 processing time", "operations", "ms", "Median wall-clock time per item."),
    "p95_processing_ms": (
        "P95 processing time",
        "operations",
        "ms",
        "95th percentile wall-clock time per item.",
    ),
    "cost_usd": (
        "Total cost",
        "operations",
        "usd",
        "Usage recorded during the run (AI, search, verification…). Registry mode incurs none.",
    ),
    "qualified_leads": (
        "Qualified leads",
        "operations",
        "count",
        "Expected people found with their correct address graded SAFE or LIKELY_SAFE.",
    ),
    "cost_per_qualified_lead": (
        "Cost per qualified lead",
        "operations",
        "usd",
        "Total cost / qualified leads.",
    ),
}


def _def(key: str) -> dict[str, Any]:
    label, group, unit, definition = METRIC_DEFS.get(
        key, (key.replace("_", " ").capitalize(), "other", "number", "")
    )
    return {"label": label, "group": group, "unit": unit, "definition": definition}


def _rate_metric(key: str, k: int, n: int) -> dict[str, Any]:
    return {**_def(key), **rate(k, n)}


def _value_metric(key: str, value: float | int | None, n: int | None = None) -> dict[str, Any]:
    if isinstance(value, float):
        value = round(value, 6 if _def(key)["unit"] == "usd" else 2)
    return {**_def(key), "value": value, "n": n, "k": None, "ci90": None}


def aggregate(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    total_cost_usd: float = 0.0,
    duration_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Run-level metrics from per-item outcomes ``{verdicts, latency_ms, cost_usd, email_cost_usd, error}``.

    Every rate carries ``n`` (sample size), ``k`` and ``ci90``; a metric with no sample has value None.
    """
    c = Counter[str]()
    latencies: list[float] = []
    email_ms: list[float] = []
    email_cost = 0.0
    items = 0
    for o in outcomes:
        items += 1
        v = o.get("verdicts") or {}
        if o.get("error"):
            c["items_failed"] += 1
        if o.get("latency_ms") is not None:
            latencies.append(float(o["latency_ms"]))
        email_cost += float(o.get("email_cost_usd") or 0.0)
        comp = v.get("company")
        if comp:
            if comp.get("given"):
                c["company_given"] += 1
            else:
                c["company_tp"] += comp["tp"]
                c["company_fp"] += comp["fp"]
                c["company_fn"] += comp["fn"]
                c["fp"] += comp["fp"]
                c["fn"] += comp["fn"]
            c["company_expected"] += 1
            c["company_found"] += int(bool(comp.get("found")))
        crawl = v.get("crawl")
        if crawl and crawl.get("attempted"):
            c["crawl_attempted"] += 1
            c["crawl_ok"] += int((crawl.get("pages") or 0) > 0)
        ppl = v.get("people")
        if ppl:
            if not ppl.get("given"):
                c["person_tp"] += ppl["tp"]
                c["person_fn"] += ppl["fn"]
                if ppl.get("exhaustive"):
                    c["person_tp_exh"] += ppl["tp"]
                    c["person_fp"] += ppl["fp"]
                c["fp"] += ppl["fp"]
                c["fn"] += ppl["fn"]
            c["roles_n"] += ppl.get("roles_evaluated", 0)
            c["roles_k"] += ppl.get("roles_correct", 0)
            c["dup"] += ppl.get("duplicates", 0)
            c["dup_n"] += ppl.get("actual_count", 0)
        em = v.get("emails")
        if em:
            c["fp"] += em["fp"]
            c["fn"] += em["fn"]
            c["dup"] += em.get("duplicates", 0)
            c["dup_n"] += em.get("actual_count", 0)
            for r in em.get("rows", []):
                verdict = r.get("verdict")
                status = r.get("actual_status")
                if verdict in ("invalid_fp", "invalid_ok"):
                    c["invalid_n"] += 1
                    c["invalid_k"] += verdict == "invalid_fp"
                    if r.get("wrong_claim"):  # an address returned for a mailbox that does not exist
                        c["claims"] += 1
                        c["safe_n"] += status == "SAFE"
                        c["deliv_n"] += status in DELIVERABLE
                    continue
                c["email_expected"] += 1
                if verdict == "correct":
                    c["email_found_any"] += 1
                    if r.get("company_level") or status in DELIVERABLE:
                        c["email_found_deliverable"] += 1
                if r.get("company_level"):
                    continue
                if verdict in ("correct", "wrong"):
                    c["claims"] += 1
                    c["claims_ok"] += verdict == "correct"
                    if status == "SAFE":
                        c["safe_n"] += 1
                        c["safe_k"] += verdict == "correct"
                    if status in DELIVERABLE:
                        c["deliv_n"] += 1
                        c["deliv_k"] += verdict == "correct"
                        c["resolved"] += 1
        enr = v.get("enrichment")
        if enr:
            c["enr_n"] += enr["evaluated"]
            c["enr_k"] += enr["correct"]
            c["enr_answered"] += enr["answered"]
            c["fp"] += enr["fp"]
            c["fn"] += enr["fn"]
        ca = v.get("catch_all")
        if ca:
            c["catch_n"] += 1
            c["catch_k"] += int(bool(ca.get("correct")))
        pat = v.get("pattern")
        if pat:
            c["pattern_n"] += 1
            c["pattern_k"] += int(bool(pat.get("correct")))
        for res in v.get("email_resolutions") or []:
            c["res_n"] += 1
            c["res_deep"] += int(bool(res.get("deep")))
            c["res_cache"] += int(bool(res.get("cache_hit")))
            if res.get("ms") is not None:
                email_ms.append(float(res["ms"]))
        c["qualified"] += int(v.get("qualified_leads") or 0)

    m: dict[str, dict[str, Any]] = {}

    def put_rate(key: str, k: int, n: int, *, when: bool = True) -> None:
        if when:
            m[key] = _rate_metric(key, k, n)

    has_company = c["company_expected"] > 0
    put_rate("company_precision", c["company_tp"], c["company_tp"] + c["company_fp"], when=has_company)
    put_rate("company_recall", c["company_tp"], c["company_tp"] + c["company_fn"], when=has_company)
    put_rate(
        "company_coverage", c["company_found"], c["company_expected"], when=has_company and mode == "registry"
    )
    put_rate("crawl_success_rate", c["crawl_ok"], c["crawl_attempted"], when=c["crawl_attempted"] > 0)
    has_people = c["person_tp"] + c["person_fn"] + c["person_fp"] + c["roles_n"] > 0
    put_rate("person_precision", c["person_tp_exh"], c["person_tp_exh"] + c["person_fp"], when=has_people)
    put_rate("person_recall", c["person_tp"], c["person_tp"] + c["person_fn"], when=has_people)
    put_rate("role_precision", c["roles_k"], c["roles_n"], when=has_people)
    has_email = c["email_expected"] + c["invalid_n"] > 0
    put_rate("email_precision", c["claims_ok"], c["claims"], when=has_email)
    put_rate("email_recall", c["email_found_deliverable"], c["email_expected"], when=has_email)
    put_rate("email_discovery_recall", c["email_found_any"], c["email_expected"], when=has_email)
    put_rate("safe_email_precision", c["safe_k"], c["safe_n"], when=has_email)
    put_rate("deliverable_precision", c["deliv_k"], c["deliv_n"], when=has_email)
    put_rate("invalid_false_positive_rate", c["invalid_k"], c["invalid_n"], when=has_email)
    put_rate("catch_all_accuracy", c["catch_k"], c["catch_n"], when=c["catch_n"] > 0)
    put_rate("domain_pattern_accuracy", c["pattern_k"], c["pattern_n"], when=c["pattern_n"] > 0)
    if c["res_n"]:
        m["avg_email_resolution_ms"] = _value_metric(
            "avg_email_resolution_ms", sum(email_ms) / len(email_ms) if email_ms else None, len(email_ms)
        )
        m["p50_email_resolution_ms"] = _value_metric(
            "p50_email_resolution_ms", percentile(email_ms, 50), len(email_ms)
        )
        m["p95_email_resolution_ms"] = _value_metric(
            "p95_email_resolution_ms", percentile(email_ms, 95), len(email_ms)
        )
        put_rate("smtp_fallback_rate", c["res_deep"], c["res_n"])
        put_rate("cache_hit_rate", c["res_cache"], c["res_n"])
    if has_email and mode != "registry":
        m["cost_per_email"] = _value_metric(
            "cost_per_email", email_cost / c["claims"] if c["claims"] else None, c["claims"]
        )
        minutes = (duration_ms or 0) / 60_000.0
        m["emails_resolved_per_minute"] = _value_metric(
            "emails_resolved_per_minute", c["resolved"] / minutes if minutes > 0 else None, c["resolved"]
        )
    put_rate("enrichment_accuracy", c["enr_k"], c["enr_n"], when=c["enr_n"] > 0)
    put_rate("enrichment_precision", c["enr_k"], c["enr_answered"], when=c["enr_n"] > 0)
    put_rate("enrichment_coverage", c["enr_answered"], c["enr_n"], when=c["enr_n"] > 0)
    m["false_positives"] = _value_metric("false_positives", c["fp"], items)
    m["false_negatives"] = _value_metric("false_negatives", c["fn"], items)
    put_rate("duplicate_rate", c["dup"], c["dup_n"], when=c["dup_n"] > 0)
    m["items"] = _value_metric("items", items, items)
    m["items_failed"] = _value_metric("items_failed", c["items_failed"], items)
    m["avg_processing_ms"] = _value_metric(
        "avg_processing_ms", sum(latencies) / len(latencies) if latencies else None, len(latencies)
    )
    m["p50_processing_ms"] = _value_metric("p50_processing_ms", percentile(latencies, 50), len(latencies))
    m["p95_processing_ms"] = _value_metric("p95_processing_ms", percentile(latencies, 95), len(latencies))
    m["cost_usd"] = _value_metric("cost_usd", round(total_cost_usd, 6), items)
    m["qualified_leads"] = _value_metric("qualified_leads", c["qualified"], items)
    m["cost_per_qualified_lead"] = _value_metric(
        "cost_per_qualified_lead", total_cost_usd / c["qualified"] if c["qualified"] else None, c["qualified"]
    )
    if c["company_given"]:
        m["company_identity_given"] = _value_metric("company_identity_given", c["company_given"], items)
    rank = {k: i for i, k in enumerate(METRIC_DEFS)}
    for key, metric in m.items():  # JSONB does not keep key order: the display order travels with the metric
        metric["order"] = rank.get(key, len(rank))
    return m


# =============================================================================================
# Suite metrics → stored shape
# =============================================================================================

_RATE_WORDS = ("precision", "recall", "accuracy", "coverage")
SUITE_DEFINITION = "Defined by the suite (synthetic, labelled scenarios)."


def _infer_unit(key: str, value: Any) -> str:
    if key.endswith("_ms"):
        return "ms"
    if "cost" in key or key.endswith("_usd"):
        return "usd"
    if key.endswith(("per_minute", "_per_min")):
        return "per_minute"
    if key in METRIC_DEFS:
        return METRIC_DEFS[key][2]
    looks_rate = key.endswith("_rate") or any(w in key for w in _RATE_WORDS)
    if looks_rate and isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1:
        return "rate"
    return "number"


def normalize_metrics(
    raw: Mapping[str, Any],
    samples: Mapping[str, int] | None = None,
    *,
    labels: Mapping[str, str] | None = None,
    definitions: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Suite metrics → the stored metric shape.

    Values are plain numbers or ``rate(k, n)`` / ``measure(value, n)`` dicts (an optional ``"unit"`` key is
    honoured: rate | ms | usd | per_minute | count | number). Plain rates get a Wilson 90 % interval when
    their sample size is known (``samples[key]``). Labels default to the harness's names; definitions
    come from the suite (``definitions``) — the harness's own dataset definitions are never assumed.
    Non-numeric values (breakdowns, nested dicts) are skipped.
    """
    out: dict[str, dict[str, Any]] = {}
    for position, (key, v) in enumerate(raw.items()):
        if isinstance(v, Mapping):
            if "value" not in v:
                continue  # a breakdown (e.g. counts per status), not a metric
            value = v.get("value")
            n = v.get("n")
            k = v.get("k")
        else:
            value, n, k = v, (samples or {}).get(key), None
        if value is not None and not isinstance(value, int | float):
            continue  # only numbers are metrics
        if isinstance(value, bool):
            value = int(value)
        unit = str(v.get("unit")) if isinstance(v, Mapping) and v.get("unit") else _infer_unit(key, value)
        ci = v.get("ci90") if isinstance(v, Mapping) else None
        if unit == "rate" and n and value is not None:
            if k is None:
                k = round(float(value) * int(n))
            ci = ci or wilson(int(k), int(n))
        if isinstance(value, float):
            value = round(value, 6)
        meta = _def(key)
        out[key] = {
            "label": (labels or {}).get(key) or meta["label"],
            "group": meta["group"] if key in METRIC_DEFS else "suite",
            "unit": unit,
            "definition": (definitions or {}).get(key) or SUITE_DEFINITION,
            "value": value,
            "n": int(n) if isinstance(n, int | float) else None,
            "k": int(k) if isinstance(k, int | float) else None,
            "ci90": list(ci) if ci else None,
            "order": position,
        }
    return out
