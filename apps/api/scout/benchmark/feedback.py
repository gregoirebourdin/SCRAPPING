"""Feed definitive ground-truth outcomes back to empirical scoring (``scout.learning.stats``).

Only definitive comparisons are fed, keyed by the provenance recorded on the compared value:

* ``people.source`` / <source key of the person's first observation> — matched (True); spurious (False,
  only when the ground truth lists every decision maker of the company);
* ``resolver`` / <e-mail resolver> and ``pattern`` / <pattern> — correct (True); wrong address or a
  known-invalid address graded deliverable (False);
* ``enrich.resolver`` / <resolver of the cell> — correct (True) / wrong (False). Unknown answers,
  user overrides and user-confirmed e-mails are not engine output and are skipped.

The learning module is optional: when it is not installed this is a no-op.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from scout.benchmark.metrics import match_people, norm_email

log = structlog.get_logger("benchmark.feedback")


def outcomes(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    verdicts: Mapping[str, Any],
    given: Mapping[str, Any],
) -> list[tuple[str, str, bool]]:
    """(dimension, key, correct) triples for one compared item (pure)."""
    out: list[tuple[str, str, bool]] = []
    people = list(actual.get("people") or [])
    pv = verdicts.get("people")
    if pv and not given.get("people") and expected.get("people") is not None:
        pairs = match_people(expected.get("people") or [], people)
        matched = {j for _, j in pairs}
        for j, a in enumerate(people):
            src = a.get("source")
            if not src:
                continue
            if j in matched:
                out.append(("people.source", src, True))
            elif pv.get("exhaustive"):
                out.append(("people.source", src, False))
    by_addr = {
        norm_email((a.get("email") or {}).get("address")): a.get("email") or {}
        for a in people
        if (a.get("email") or {}).get("address")
    }
    for r in (verdicts.get("emails") or {}).get("rows", []):
        verdict = r.get("verdict")
        if verdict not in ("correct", "wrong", "invalid_fp") or r.get("company_level"):
            continue
        email = by_addr.get(r.get("actual")) or {}
        if email.get("user_confirmed"):
            continue
        ok = verdict == "correct"
        if email.get("resolver"):
            out.append(("resolver", str(email["resolver"]), ok))
        if email.get("pattern"):
            out.append(("pattern", str(email["pattern"]), ok))
    enrichment = actual.get("enrichment") or {}
    for r in (verdicts.get("enrichment") or {}).get("rows", []):
        if r.get("verdict") not in ("correct", "wrong") or not r.get("resolver"):
            continue
        if (enrichment.get(r.get("key")) or {}).get("user_override"):
            continue
        out.append(("enrich.resolver", str(r["resolver"]), r["verdict"] == "correct"))
    return out


async def feed(triples: list[tuple[str, str, bool]]) -> int:
    """Record outcomes; returns how many were recorded (0 when the learning module is absent)."""
    if not triples:
        return 0
    try:
        from scout.learning.stats import record_outcome  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return 0
    n = 0
    for dimension, key, correct in triples:
        try:
            await record_outcome(dimension, key, correct)
            n += 1
        except Exception as exc:  # learning must never break a benchmark
            log.warning("benchmark.feedback_failed", dimension=dimension, error=str(exc))
            break
    return n
