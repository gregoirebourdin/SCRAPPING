"""Domain pattern learning v2: recency- and source-weighted samples → Bayesian pattern posteriors.

Model ("convention model"). A domain has one convention ``c`` (prior π from
:func:`scout.email.patterns.ranked_priors`, i.e. company size + country). Each named address follows
``c`` with probability ρ ~ Beta(8, 1) (companies mostly enforce one convention; legacy addresses,
founders and homonyms are the exceptions); exceptions follow a Dirichlet over the other patterns
centred on π. Evidence per pattern is a *weighted* count:

    weight = source weight × recency (half-life 18 months) × min(1, sample confidence / 0.9)

plus SMTP confirmations (1.0 each). SMTP rejections from healthy infrastructure (the caller decides)
are "not this pattern" observations against the pattern they were rendered with. The reported
``confidence`` is the posterior predictive probability that a *new* employee follows the pattern
(it sums to 1 over the vocabulary): one website sample gives ≈ 0.84, two ≈ 0.90, eight ≈ 0.94,
8 × ``{first}.{last}`` + 1 × ``{f}{last}`` ≈ 0.89 — never 100 %.

Excluded from learning: role addresses, nameless addresses, two-letter ``{f}{l}`` matches (too weak),
names that do not render the local part. Ambiguous local parts resolve like
:func:`scout.email.patterns.infer_pattern` (first vocabulary match).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import EmailEvidenceSource
from scout.db.models import DomainEmailPattern, DomainEmailSample, DomainProfile
from scout.email.contracts import ObservedEmail, PatternStat
from scout.email.intel import state
from scout.email.lists import is_role_local_part
from scout.email.patterns import PATTERN_SET, PATTERNS, infer_pattern, ranked_priors
from scout.email.syntax import normalize_domain

log = structlog.get_logger(__name__)

SOURCE_WEIGHTS: dict[EmailEvidenceSource, float] = {
    EmailEvidenceSource.website: 1.0,
    EmailEvidenceSource.user: 1.0,
    EmailEvidenceSource.smtp_verified: 1.0,
    EmailEvidenceSource.import_: 0.9,
    EmailEvidenceSource.github: 0.7,  # commit emails can be personal aliases or legacy
    EmailEvidenceSource.search: 0.6,
    EmailEvidenceSource.rdap: 0.5,
}
HALF_LIFE_DAYS = 548.0  # ~18 months
MIN_RECENCY_WEIGHT = 0.05
DEFAULT_SAMPLE_CONFIDENCE = 0.9  # samples at this confidence count fully
WEAK_PATTERNS: frozenset[str] = frozenset({"{f}{l}"})

CONFORMITY_PRIOR: tuple[float, float] = (8.0, 1.0)  # Beta(a, b) on ρ: mean 0.89, worth 9 samples
EXCEPTION_CONCENTRATION = 1.0  # Dirichlet strength of the exception distribution
SUCCESS_WEIGHT = 1.0
FAILURE_WEIGHT = 1.0
LEGACY_WEIGHT = 0.8  # v1 counters carried over without addresses or dates
PATTERNS_FRESHNESS = timedelta(days=30)  # stored posteriors are recomputed (recency decay) after this

_LOCK_SQL = sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


# ---- pure learning --------------------------------------------------------------------------


def recency_weight(observed_at: datetime | None, *, now: datetime) -> float:
    """0.5 ** (age / 18 months), floored at 0.05; unknown or future dates count fully."""
    if observed_at is None:
        return 1.0
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=now.tzinfo)
    age_days = (now - observed_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return max(MIN_RECENCY_WEIGHT, 0.5 ** (age_days / HALF_LIFE_DAYS))


def sample_weight(sample: ObservedEmail, *, now: datetime) -> float:
    """Evidence weight of one observed address (source × recency × confidence)."""
    source = SOURCE_WEIGHTS.get(EmailEvidenceSource(sample.source), 0.5)
    conf = min(1.0, max(0.0, float(sample.confidence)) / DEFAULT_SAMPLE_CONFIDENCE)
    return source * recency_weight(sample.observed_at, now=now) * conf


def sample_pattern(sample: ObservedEmail) -> str | None:
    """Learnable pattern of a sample, or None (role, nameless, weak or non-matching)."""
    local = sample.local_part or sample.address.rpartition("@")[0]
    if sample.is_role or not local or is_role_local_part(local):
        return None
    if sample.first_name and sample.last_name:
        pattern = infer_pattern(sample.first_name, sample.last_name, local)
    else:
        pattern = None
    return pattern if pattern in PATTERN_SET and pattern not in WEAK_PATTERNS else None


def wilson_lower_bound(p: float, n: float, z: float = 1.0) -> float:
    """Wilson score lower bound of a proportion `p` observed over `n` (fractional) trials."""
    if n <= 0:
        return 0.0
    p = min(1.0, max(0.0, p))
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


@dataclass
class _Evidence:
    weight: dict[str, float] = field(default_factory=dict)
    count: dict[str, int] = field(default_factory=dict)
    smtp_count: dict[str, int] = field(default_factory=dict)
    latest: dict[str, datetime] = field(default_factory=dict)


def _on_domain(address: str, domain: str | None) -> bool:
    if not domain:
        return True
    d = address.rpartition("@")[2].lower()
    return d == domain or d.endswith("." + domain)


def _collect(domain: str | None, samples: Iterable[ObservedEmail], now: datetime) -> _Evidence:
    """Per-pattern weighted counts; an address seen from several sources counts once (best weight)."""
    best: dict[str, tuple[float, str, ObservedEmail]] = {}
    for s in samples:
        addr = (s.address or "").strip().lower()
        if not addr or not _on_domain(addr, domain):
            continue
        pattern = sample_pattern(s)
        if pattern is None:
            continue
        w = sample_weight(s, now=now)
        if w <= 0:
            continue
        prev = best.get(addr)
        if prev is None or w > prev[0]:
            best[addr] = (w, pattern, s)
    ev = _Evidence()
    for w, pattern, s in best.values():
        ev.weight[pattern] = ev.weight.get(pattern, 0.0) + w
        ev.count[pattern] = ev.count.get(pattern, 0) + 1
        if s.source == EmailEvidenceSource.smtp_verified:
            ev.smtp_count[pattern] = ev.smtp_count.get(pattern, 0) + 1
        if s.observed_at is not None and (pattern not in ev.latest or s.observed_at > ev.latest[pattern]):
            ev.latest[pattern] = s.observed_at
    return ev


def _lbeta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def posterior(
    evidence: Mapping[str, float], failures: Mapping[str, float], priors: Mapping[str, float]
) -> dict[str, float]:
    """Posterior predictive P(new employee follows p) for every vocabulary pattern (sums to 1)."""
    a0, b0 = CONFORMITY_PRIOR
    beta = EXCEPTION_CONCENTRATION
    n_total = sum(evidence.values())
    log_lik: dict[str, float] = {}
    conformity: dict[str, float] = {}
    for c in PATTERNS:
        pc = priors[c]
        nc = evidence.get(c, 0.0)
        fc = failures.get(c, 0.0)
        others = n_total - nc
        ll = math.log(pc) + _lbeta(a0 + nc, b0 + others + fc) - _lbeta(a0, b0)
        ll += math.lgamma(beta) - math.lgamma(beta + others)
        for q in PATTERNS:
            nq = evidence.get(q, 0.0)
            if q != c and nq > 0:
                bq = beta * priors[q] / (1.0 - pc)
                ll += math.lgamma(bq + nq) - math.lgamma(bq)
        log_lik[c] = ll
        conformity[c] = (a0 + nc) / (a0 + b0 + n_total + fc)
    top = max(log_lik.values())
    weights = {c: math.exp(v - top) for c, v in log_lik.items()}
    z = sum(weights.values())
    post_c = {c: w / z for c, w in weights.items()}
    out: dict[str, float] = {}
    for p in PATTERNS:
        prob = post_c[p] * conformity[p]
        for c in PATTERNS:
            if c == p:
                continue
            others = n_total - evidence.get(c, 0.0)
            share = (beta * priors[p] / (1.0 - priors[c]) + evidence.get(p, 0.0)) / (beta + others)
            prob += post_c[c] * (1.0 - conformity[c]) * share
        out[p] = prob
    return out


def _clean_counts(values: Mapping[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for p, v in (values or {}).items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if p in PATTERN_SET and n > 0:
            out[p] = n
    return out


def learn(
    domain: str | None,
    samples: Iterable[ObservedEmail],
    *,
    successes: Mapping[str, int] | None = None,
    failures: Mapping[str, int] | None = None,
    confirmed_at: Mapping[str, datetime | None] | None = None,
    legacy: Mapping[str, int] | None = None,
    company_size_max: int | None = None,
    country: str | None = None,
    now: datetime | None = None,
    include_unsupported: bool = False,
) -> list[PatternStat]:
    """Learned conventions of a domain, best first (pure function).

    `successes` / `failures` are SMTP counters per pattern (failures only from healthy infrastructure —
    the caller decides). SMTP confirmations already present as ``smtp_verified`` samples are not counted
    twice. `legacy` carries v1 counters (no addresses). Patterns without positive evidence are omitted
    unless `include_unsupported` (then failure-only patterns are returned with share 0).
    """
    now = now or state.utcnow()
    d = normalize_domain(domain) if domain else None
    ev = _collect(d, samples, now)
    succ = _clean_counts(successes)
    fail = _clean_counts(failures)
    legacy_counts = _clean_counts(legacy)

    evidence: dict[str, float] = {}
    counts: dict[str, int] = {}
    for p in PATTERNS:
        w, c = ev.weight.get(p, 0.0), ev.count.get(p, 0)
        if legacy_counts.get(p):
            w, c = max(w, legacy_counts[p] * LEGACY_WEIGHT), max(c, legacy_counts[p])
        extra = max(0, succ.get(p, 0) - ev.smtp_count.get(p, 0))
        total = w + extra * SUCCESS_WEIGHT
        if total > 0:
            evidence[p] = total
        if c:
            counts[p] = c
    fail_w = {p: n * FAILURE_WEIGHT for p, n in fail.items()}
    wanted = set(evidence) | (set(fail_w) if include_unsupported else set())
    if not wanted:
        return []

    priors = dict(ranked_priors(company_size_max=company_size_max, country=country))
    conf = posterior(evidence, fail_w, priors)
    n_total = sum(evidence.values())
    out: list[PatternStat] = []
    for p in PATTERNS:
        if p not in wanted:
            continue
        dates = [x for x in (ev.latest.get(p), (confirmed_at or {}).get(p)) if x is not None]
        out.append(
            PatternStat(
                pattern=p,
                share=_share(evidence.get(p, 0.0), n_total),
                confidence=round(conf[p], 4),
                samples=counts.get(p, 0),
                successes=succ.get(p, 0),
                failures=fail.get(p, 0),
                last_confirmed_at=max(dates) if dates else None,
            )
        )
    return rank(out)


def _share(weight: float, total: float) -> float:
    if weight <= 0 or total <= 0:
        return 0.0
    return max(0.0001, round(weight / total, 4))  # supported patterns never store a zero share


def rank(stats: Iterable[PatternStat]) -> list[PatternStat]:
    """Best first: posterior confidence, then evidence, then vocabulary order.

    All patterns of a domain share one evidence base, so a 1-sample pattern can never outrank a
    7-sample one (the posterior already shrinks thin evidence towards the prior).
    """
    return sorted(stats, key=lambda s: (-s.confidence, -(s.samples + s.successes), PATTERNS.index(s.pattern)))


def dominant_lower_bound(stats: list[PatternStat]) -> float | None:
    """Conservative (Wilson, z=1) lower bound of the dominant pattern's confidence."""
    if not stats:
        return None
    n = sum(s.samples + s.successes for s in stats) + stats[0].failures
    return round(wilson_lower_bound(stats[0].confidence, n), 4)


# ---- persistence --------------------------------------------------------------------------------


def observed_from_row(row: DomainEmailSample) -> ObservedEmail:
    """``domain_email_samples`` row → ObservedEmail (observed_at = latest evidence date)."""
    return ObservedEmail(
        address=row.address,
        local_part=row.local_part,
        source=EmailEvidenceSource(row.source),
        first_name=row.first_name,
        last_name=row.last_name,
        pattern=row.pattern,
        is_role=bool(row.is_role),
        source_url=row.source_url,
        evidence=row.evidence,
        confidence=float(row.confidence),
        observed_at=row.last_seen_at or row.observed_at,
    )


def _row_stat(r: DomainEmailPattern) -> PatternStat:
    return PatternStat(
        pattern=r.pattern,
        share=float(r.share or 0.0),
        confidence=float(r.confidence or 0.0),
        samples=int(r.supporting_samples or 0),
        successes=int(r.successful_checks or 0),
        failures=int(r.failed_checks or 0),
        last_confirmed_at=r.last_confirmed_at,
    )


async def lock_domain(s: AsyncSession, domain: str) -> None:
    """Serialize pattern writes for one domain (transaction-scoped advisory lock, re-entrant)."""
    await s.execute(_LOCK_SQL, {"key": f"scout:domain_patterns:{domain}"})


async def ensure_profile(s: AsyncSession, domain: str) -> None:
    await s.execute(
        pg_insert(DomainProfile).values(domain=domain).on_conflict_do_nothing(index_elements=["domain"])
    )


async def _relearn(
    s: AsyncSession,
    domain: str,
    *,
    company_size_max: int | None = None,
    country: str | None = None,
    now: datetime | None = None,
) -> list[PatternStat]:
    """Recompute and store the domain's patterns inside `s` (caller commits)."""
    now = now or state.utcnow()
    await lock_domain(s, domain)
    await ensure_profile(s, domain)
    profile = await s.get(DomainProfile, domain, with_for_update=True, populate_existing=True)
    assert profile is not None
    rows = (
        await s.scalars(
            sa.select(DomainEmailPattern)
            .where(DomainEmailPattern.domain == domain)
            .execution_options(populate_existing=True)
        )
    ).all()
    sample_rows = (
        await s.scalars(sa.select(DomainEmailSample).where(DomainEmailSample.domain == domain))
    ).all()

    stats: dict[str, Any] = dict(profile.stats or {})
    if not stats.get("patterns_v2"):
        # First v2 pass: v1 counters had no addresses; keep them as legacy evidence.
        legacy_v1 = {r.pattern: int(r.supporting_samples) for r in rows if r.supporting_samples > 0}
        if legacy_v1:
            stats["legacy_samples"] = legacy_v1
        stats["patterns_v2"] = True
    if company_size_max is not None or country is not None:
        stats["prior_context"] = {"company_size_max": company_size_max, "country": country}
    ctx = stats.get("prior_context") or {}

    observed = [observed_from_row(r) for r in sample_rows]
    learned = learn(
        domain,
        observed,
        successes={r.pattern: r.successful_checks for r in rows},
        failures={r.pattern: r.failed_checks for r in rows},
        confirmed_at={r.pattern: r.last_confirmed_at for r in rows},
        legacy=stats.get("legacy_samples"),
        company_size_max=ctx.get("company_size_max"),
        country=ctx.get("country"),
        now=now,
        include_unsupported=True,
    )
    by_pattern = {st.pattern: st for st in learned}
    existing = {r.pattern: r for r in rows}
    for pattern in sorted(set(by_pattern) | set(existing)):
        if pattern not in PATTERN_SET:
            continue
        st = by_pattern.get(pattern)
        row = existing.get(pattern)
        if row is None:
            row = DomainEmailPattern(domain=domain, pattern=pattern)
            s.add(row)
        row.confidence = st.confidence if st else 0.0
        row.share = st.share if st else 0.0
        row.supporting_samples = st.samples if st else 0
        dates = [x for x in (row.last_confirmed_at, st.last_confirmed_at if st else None) if x is not None]
        row.last_confirmed_at = max(dates) if dates else None
        row.updated_at = now

    supported = [st for st in learned if st.share > 0]
    dominant = supported[0] if supported else None
    meaningful = (
        max(profile.dominant_pattern_confidence or 0.0, dominant.confidence if dominant else 0.0) >= 0.5
    )
    if profile.dominant_pattern and dominant and profile.dominant_pattern != dominant.pattern and meaningful:
        stats = state.add_flag(
            stats,
            state.PATTERN_CHANGED,
            {"from": profile.dominant_pattern, "to": dominant.pattern, "confidence": dominant.confidence},
            now,
        )
        log.info(
            "email.intel.pattern_changed", domain=domain, old=profile.dominant_pattern, new=dominant.pattern
        )
    stats["patterns_learned_at"] = state.iso(now)
    profile.dominant_pattern = dominant.pattern if dominant else None
    profile.dominant_pattern_confidence = dominant.confidence if dominant else None
    profile.named_samples = len({o.address for o in observed if sample_pattern(o) is not None})
    profile.observed_emails = len({o.address for o in observed})
    profile.stats = stats
    await s.flush()
    return supported


async def relearn_domain_patterns(
    domain: str, *, company_size_max: int | None = None, country: str | None = None
) -> list[PatternStat]:
    """Recompute ``domain_email_patterns`` (share, confidence, supporting_samples, last_confirmed_at)
    from ``domain_email_samples`` and the SMTP counters. Returns the supported patterns, best first."""
    d = normalize_domain(domain)
    if d is None:
        return []
    async with session_scope() as s:
        out = await _relearn(s, d, company_size_max=company_size_max, country=country)
    state.invalidate(d)
    return out


async def bump_counters(
    s: AsyncSession, domain: str, pattern: str, *, successes: int = 0, failures: int = 0
) -> None:
    """Add SMTP counters for (domain, pattern) inside `s` (creates the row; caller relearns)."""
    if pattern not in PATTERN_SET or (successes <= 0 and failures <= 0):
        return
    T = DomainEmailPattern
    now = sa.func.now()
    ins = pg_insert(T).values(
        domain=domain,
        pattern=pattern,
        successful_checks=max(0, successes),
        failed_checks=max(0, failures),
        last_verified_at=now,
        last_confirmed_at=now if successes > 0 else None,
        last_failed_at=now if failures > 0 else None,
    )
    await lock_domain(s, domain)
    await s.execute(
        ins.on_conflict_do_update(
            index_elements=[T.domain, T.pattern],
            set_={
                "successful_checks": T.successful_checks + ins.excluded.successful_checks,
                "failed_checks": T.failed_checks + ins.excluded.failed_checks,
                "last_verified_at": now,
                "last_confirmed_at": sa.func.coalesce(ins.excluded.last_confirmed_at, T.last_confirmed_at),
                "last_failed_at": sa.func.coalesce(ins.excluded.last_failed_at, T.last_failed_at),
                "updated_at": now,
            },
        )
    )


async def relearn_in_session(s: AsyncSession, domain: str) -> list[PatternStat]:
    """Relearn inside an open transaction (for callers that batch writes); invalidates the cache."""
    out = await _relearn(s, domain)
    state.invalidate(domain)
    return out


async def record_pattern_outcome(domain: str, pattern: str, success: bool) -> None:
    """Count an SMTP outcome for a guessed address rendered with `pattern`, then relearn.

    Successes create/strengthen the pattern; failures (healthy infrastructure only — the caller
    decides) are stored even for patterns without samples, so later evidence starts from them.
    """
    d = normalize_domain(domain)
    if d is None or pattern not in PATTERN_SET:
        return
    async with session_scope() as s:
        await bump_counters(s, d, pattern, successes=1 if success else 0, failures=0 if success else 1)
        await _relearn(s, d)
    state.invalidate(d)


async def add_legacy_samples(s: AsyncSession, domain: str, pattern: str, n: int) -> None:
    """Add address-less sample counts for a pattern (v1 compatibility); caller relearns."""
    if n <= 0 or pattern not in PATTERN_SET:
        return
    await lock_domain(s, domain)
    await ensure_profile(s, domain)
    profile = await s.get(DomainProfile, domain, with_for_update=True, populate_existing=True)
    assert profile is not None
    stats = dict(profile.stats or {})
    if not stats.get("patterns_v2"):
        await _relearn(s, domain)  # snapshot v1 counters before adding to them
        stats = dict(profile.stats or {})
    legacy = dict(stats.get("legacy_samples") or {})
    legacy[pattern] = int(legacy.get(pattern, 0)) + n
    stats["legacy_samples"] = legacy
    profile.stats = stats
    await s.flush()


async def load_pattern_stats(domain: str | None) -> list[PatternStat]:
    """Stored patterns with positive evidence, best first (relearned when stale or still v1)."""
    d = normalize_domain(domain)
    if d is None:
        return []
    async with session_scope() as s:
        rows = (await s.scalars(sa.select(DomainEmailPattern).where(DomainEmailPattern.domain == d))).all()
    if not rows:
        return []
    cutoff = state.utcnow() - PATTERNS_FRESHNESS
    v1 = any(r.share == 0 and (r.supporting_samples + r.successful_checks) > 0 for r in rows)
    if v1 or any(r.updated_at < cutoff for r in rows):
        return await relearn_domain_patterns(d)
    return rank(_row_stat(r) for r in rows if r.pattern in PATTERN_SET and r.share > 0)
