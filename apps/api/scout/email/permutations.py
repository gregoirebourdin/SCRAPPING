"""Ranked email candidates for one person at one domain (known → inferred → prior permutations)."""

from __future__ import annotations

from collections.abc import Sequence

from scout.db.enums import EmailDiscoveryMethod
from scout.email.patterns import (
    PATTERN_SET,
    VARIANT_DECAY,
    infer_from_local_parts,
    infer_patterns,
    name_parts,
    pattern_confidence,
    ranked_priors,
    render_parts,
)
from scout.email.syntax import normalize_domain
from scout.email.types import EmailCandidate

# Known patterns below this confidence (e.g. repeatedly rejected) compete with priors instead.
MIN_STRONG_KNOWN = 0.5
MIN_INFERRED = 0.3
MAX_INFERRED_PATTERNS = 2

_TIER_KNOWN, _TIER_INFERRED, _TIER_PRIOR = 0, 1, 2


def rank_candidates(
    first: str | None,
    last: str | None,
    domain: str,
    *,
    known_patterns: Sequence[tuple[str, float, int]] = (),
    observed_local_parts: Sequence[str] = (),
    observed_samples: Sequence[tuple[str, str, str]] = (),
    company_size_max: int | None = None,
    country: str | None = None,
    max_candidates: int = 6,
) -> list[EmailCandidate]:
    """Ordered, de-duplicated candidates (never more than `max_candidates`).

    1. `known_patterns` for the domain — (pattern, confidence, supporting_samples) from
       `domain_email_patterns` — method `known_pattern`, pattern_confidence = stored confidence.
    2. Patterns inferred from addresses observed at the domain: named samples
       (first, last, local_part) of other people, then nameless local parts — `inferred_pattern`.
    3. Context-adjusted global priors — `permutation`.
    Secondary spellings of compound names are decayed so they interleave with other patterns.
    """
    dom = normalize_domain(domain)
    parts = name_parts(first, last)
    if dom is None or parts.is_empty or max_candidates <= 0:
        return []

    scored: list[tuple[int, float, int, EmailCandidate]] = []

    def add(tier: int, method: EmailDiscoveryMethod, pattern: str, conf: float, samples: int) -> None:
        for idx, local in enumerate(render_parts(pattern, parts)):
            c = round(conf * VARIANT_DECAY**idx, 4)
            cand = EmailCandidate(
                address=f"{local}@{dom}",
                method=method,
                pattern=pattern,
                pattern_confidence=c,
                supporting_samples=samples,
            )
            scored.append((tier, -c, len(scored), cand))

    for pattern, conf, samples in known_patterns:
        if pattern in PATTERN_SET and conf > 0:
            tier = _TIER_KNOWN if conf >= MIN_STRONG_KNOWN else _TIER_PRIOR
            add(tier, EmailDiscoveryMethod.known_pattern, pattern, float(conf), int(samples))

    inferred: dict[str, tuple[float, int]] = {}
    for pattern, n in infer_patterns(observed_samples).items():
        inferred[pattern] = (pattern_confidence(n), n)
    for pattern, conf, _count in infer_from_local_parts(observed_local_parts):
        if pattern not in inferred:
            inferred[pattern] = (conf, 0)  # nameless shapes are not "supporting samples"
    best_inferred = sorted(inferred.items(), key=lambda kv: -kv[1][0])[:MAX_INFERRED_PATTERNS]
    for pattern, (conf, samples) in best_inferred:
        tier = _TIER_INFERRED if conf >= MIN_INFERRED else _TIER_PRIOR
        add(tier, EmailDiscoveryMethod.inferred_pattern, pattern, conf, samples)

    for pattern, prior in ranked_priors(company_size_max=company_size_max, country=country):
        add(_TIER_PRIOR, EmailDiscoveryMethod.permutation, pattern, prior, 0)

    out: list[EmailCandidate] = []
    seen: set[str] = set()
    for _, _, _, cand in sorted(scored, key=lambda t: (t[0], t[1], t[2])):
        if cand.address in seen:
            continue
        seen.add(cand.address)
        out.append(cand)
        if len(out) >= max_candidates:
            break
    return out
