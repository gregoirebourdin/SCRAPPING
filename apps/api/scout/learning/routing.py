"""Empirical routing: turn learned counters into decisions (docs/ARCHITECTURE.md §Empirical Source Scoring).

* ``estimate()`` — effective precision / coverage / latency / cost for one key: the prior until the key has
  ``MIN_OUTCOMES`` judged outcomes (precision) or ``MIN_ATTEMPTS`` attempts (coverage, latency, cost), then the
  Beta-smoothed estimate (k = ``stats.PRIOR_STRENGTH`` pseudo-observations at the prior).
* ``rank()`` — order candidate keys by an objective (precision, expected yield = precision × coverage, yield
  per dollar, yield per second). Exploration: once any candidate has learned evidence, a share
  ``EXPLORATION_SHARE`` of decisions ranks on a Thompson draw from each key's Beta posterior instead of its
  mean, so new or unlucky sources still get tried. The RNG is seedable (deterministic in tests).
* ``blend_confidence()`` — shift a hard-coded per-source confidence by what was learned, in log-odds:
  ``logit(c') = logit(c) + logit(p̂) − logit(p₀)`` (no change until the source has ``MIN_OUTCOMES``).
* ``relative_yield()`` — learned expected yield relative to the prior, clipped (router weights).

Every function is pure and never raises on missing data: no snapshot / no row ⇒ priors (deterministic).
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from scout.learning import priors as P
from scout.learning.stats import PRIOR_STRENGTH, Snapshot, StatRow, beta_mean

MIN_OUTCOMES = 10  # judged outcomes before learned precision overrides the prior
MIN_ATTEMPTS = 30  # attempts before learned coverage / latency / cost override the prior
EXPLORATION_SHARE = 0.1  # share of decisions that explore (Thompson draw) once evidence exists
COST_FLOOR_USD = 0.0005  # keeps yield-per-dollar finite for free resolvers
FACTOR_RANGE = (0.5, 1.5)  # relative_yield clip

Objective = Literal["precision", "yield", "yield_per_cost", "latency"]
EvidenceLevel = Literal["prior only", "learning", "learned"]

_RNG = random.Random()


def enabled() -> bool:
    """``Settings.empirical_routing_enabled`` (False if settings cannot be read)."""
    try:
        from scout.config import get_settings

        return bool(get_settings().empirical_routing_enabled)
    except Exception:
        return False


@dataclass(frozen=True)
class Estimate:
    dimension: str
    key: str
    prior: P.Prior
    precision: float
    coverage: float
    latency_ms: float
    cost_usd: float
    attempts: int = 0
    successes: int = 0
    correct: int = 0
    wrong: int = 0
    precision_learned: bool = False
    coverage_learned: bool = False

    @property
    def judged(self) -> int:
        return self.correct + self.wrong

    @property
    def learned(self) -> bool:
        return self.precision_learned or self.coverage_learned

    @property
    def expected_yield(self) -> float:
        return self.precision * self.coverage

    @property
    def evidence_level(self) -> EvidenceLevel:
        if self.precision_learned:
            return "learned"
        if self.attempts or self.judged:
            return "learning"
        return "prior only"


def _as_prior(dimension: str, key: str, prior: P.Prior | float | None) -> P.Prior:
    if prior is None:
        return P.prior(dimension, key)
    if isinstance(prior, P.Prior):
        return prior
    base = P.known(dimension, key) or P.DEFAULT_PRIOR
    return P.Prior(float(prior), base.coverage, base.cost_usd, base.latency_ms, base.label)


def estimate(
    dimension: str, key: str, snap: Snapshot | None = None, *, prior: P.Prior | float | None = None
) -> Estimate:
    """Effective estimate for one key (priors until the minimum evidence is reached)."""
    pr = _as_prior(dimension, key, prior)
    row: StatRow | None
    try:
        row = (snap or {}).get((dimension, key))
    except Exception:
        row = None
    if row is None:
        return Estimate(dimension, key, pr, pr.precision, pr.coverage, pr.latency_ms, pr.cost_usd)
    judged = row.correct + row.wrong
    p_learned = judged >= MIN_OUTCOMES
    c_learned = row.attempts >= MIN_ATTEMPTS
    return Estimate(
        dimension,
        key,
        pr,
        precision=beta_mean(row.correct, judged, pr.precision) if p_learned else pr.precision,
        coverage=beta_mean(row.successes, row.attempts, pr.coverage) if c_learned else pr.coverage,
        latency_ms=row.latency_ms_total / row.attempts if c_learned else pr.latency_ms,
        cost_usd=row.cost_usd_total / row.attempts if c_learned else pr.cost_usd,
        attempts=row.attempts,
        successes=row.successes,
        correct=row.correct,
        wrong=row.wrong,
        precision_learned=p_learned,
        coverage_learned=c_learned,
    )


def objective_value(
    precision: float, coverage: float, cost_usd: float, latency_ms: float, objective: Objective
) -> float:
    if objective == "precision":
        return precision
    y = precision * coverage
    if objective == "yield":
        return y
    if objective == "yield_per_cost":
        return y / (max(0.0, cost_usd) + COST_FLOOR_USD)
    if objective == "latency":
        return y / (1.0 + max(0.0, latency_ms) / 1000.0)
    return y  # unknown objective: expected yield (never raise on a hot path)


def make_rng(seed: int | str | None = None) -> random.Random:
    """Seeded RNG (stable across processes for str seeds); the shared module RNG when ``seed`` is None."""
    if seed is None:
        return _RNG
    if isinstance(seed, str):
        seed = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big")
    return random.Random(seed)


def posterior_sample(est: Estimate, rng: random.Random) -> float:
    """Thompson draw of precision from Beta(correct + k·p₀, wrong + k·(1 − p₀))."""
    p0 = min(1 - 1e-3, max(1e-3, est.prior.precision))
    return rng.betavariate(est.correct + PRIOR_STRENGTH * p0, est.wrong + PRIOR_STRENGTH * (1 - p0))


@dataclass(frozen=True)
class Ranked:
    key: str
    score: float
    estimate: Estimate
    precision_used: float  # posterior mean, or the Thompson draw when this decision explored
    explored: bool = False


def rank(
    dimension: str,
    keys: Sequence[str],
    snap: Snapshot | None = None,
    *,
    objective: Objective = "yield_per_cost",
    priors: Mapping[str, P.Prior | float] | None = None,
    explore: float = EXPLORATION_SHARE,
    seed: int | str | None = None,
    rng: random.Random | None = None,
    keep_order_without_evidence: bool = True,
) -> list[Ranked]:
    """Rank candidate keys, best first (ties keep the input order).

    While no candidate has learned evidence the input order is returned unchanged when
    ``keep_order_without_evidence`` (callers' deterministic order encodes their priors), else keys are ranked
    on priors. Exploration only happens once some candidate has learned evidence.
    """
    keys = list(dict.fromkeys(k for k in keys if k))
    try:
        return _rank(
            dimension, keys, snap, objective, priors, explore, seed, rng, keep_order_without_evidence
        )
    except Exception:  # never break a caller's routing: fall back to its own (prior) order
        ests = [estimate(dimension, k, None) for k in keys]
        return [Ranked(e.key, 0.0, e, e.precision) for e in ests]


def _rank(
    dimension: str,
    keys: list[str],
    snap: Snapshot | None,
    objective: Objective,
    priors: Mapping[str, P.Prior | float] | None,
    explore: float,
    seed: int | str | None,
    rng: random.Random | None,
    keep_order_without_evidence: bool,
) -> list[Ranked]:
    ests = [estimate(dimension, k, snap, prior=(priors or {}).get(k)) for k in keys]
    any_learned = any(e.learned for e in ests)
    if not any_learned and keep_order_without_evidence:
        return [
            Ranked(
                e.key,
                objective_value(e.precision, e.coverage, e.cost_usd, e.latency_ms, objective),
                e,
                e.precision,
            )
            for e in ests
        ]
    r = rng or make_rng(seed)
    exploring = any_learned and explore > 0 and r.random() < explore
    scored: list[tuple[float, int, Ranked]] = []
    for i, e in enumerate(ests):
        p = posterior_sample(e, r) if exploring else e.precision
        score = objective_value(p, e.coverage, e.cost_usd, e.latency_ms, objective)
        scored.append((score, i, Ranked(e.key, score, e, p, exploring)))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [x for _, _, x in scored]


def relative_yield(
    est: Estimate, precision: float | None = None, *, bounds: tuple[float, float] = FACTOR_RANGE
) -> float:
    """(p̂ · ĉ) / (p₀ · c₀), clipped — 1.0 exactly while nothing is learned (and no exploration draw)."""
    p = est.precision if precision is None else precision
    base = est.prior.precision * est.prior.coverage
    if base <= 0:
        return 1.0
    lo, hi = bounds
    return max(lo, min(hi, (p * est.coverage) / base))


def _logit(p: float) -> float:
    p = min(0.99, max(0.01, p))
    return math.log(p / (1 - p))


def blend_confidence(confidence: float, est: Estimate) -> float:
    """Shift a hard-coded confidence by the learned precision of its source, in log-odds space.

    ``logit(c') = logit(c) + logit(p̂) − logit(p₀)``. Unchanged until the source has ``MIN_OUTCOMES`` judged
    outcomes; a good track record never lowers a confidence and a bad one never raises it.
    """
    if not est.precision_learned or not isinstance(confidence, int | float):
        return confidence
    shift = _logit(est.precision) - _logit(est.prior.precision)
    if abs(shift) < 1e-9:
        return confidence
    blended = 1.0 / (1.0 + math.exp(-(_logit(confidence) + shift)))
    out = max(confidence, blended) if shift > 0 else min(confidence, blended)
    return round(max(0.0, min(1.0, out)), 3)


def learned_confidence(
    dimension: str,
    key: str,
    confidence: float,
    snap: Snapshot | None,
    *,
    prior: P.Prior | float | None = None,
) -> float:
    return blend_confidence(confidence, estimate(dimension, key, snap, prior=prior))
