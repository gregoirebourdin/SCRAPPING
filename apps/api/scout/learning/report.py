"""Source reliability report (``GET /v1/learning/sources``, admin "Sources & health" page)."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.models import ResolverStat
from scout.learning import priors as P
from scout.learning import routing
from scout.learning.stats import StatRow, beta_mean, row_from_model, wilson_interval


def _r(v: float | None, digits: int = 4) -> float | None:
    return None if v is None else round(v, digits)


def reliability_row(dimension: str, key: str, row: StatRow | None, updated_at: Any = None) -> dict[str, Any]:
    known = P.known(dimension, key)
    prior = known or P.DEFAULT_PRIOR
    est = routing.estimate(dimension, key, {(dimension, key): row} if row else None, prior=prior)
    r = row or StatRow(0, 0, 0, 0, 0)
    judged = r.correct + r.wrong
    interval = wilson_interval(r.correct, judged)
    return {
        "dimension": dimension,
        "dimension_label": P.DIMENSIONS.get(dimension, dimension),
        "key": key,
        "label": P.label(dimension, key),
        "attempts": r.attempts,
        "successes": r.successes,
        "coverage": _r(r.successes / r.attempts) if r.attempts else None,
        "confirmed_correct": r.correct,
        "confirmed_wrong": r.wrong,
        "inconclusive": r.inconclusive,
        "precision": _r(beta_mean(r.correct, judged, prior.precision)),
        "raw_precision": _r(r.correct / judged) if judged else None,
        "precision_interval": [_r(interval[0]), _r(interval[1])] if interval else None,
        "effective_precision": _r(est.precision),
        "effective_coverage": _r(est.coverage),
        "avg_latency_ms": _r(r.avg_latency_ms, 1),
        "avg_cost_usd": _r(r.avg_cost_usd, 6),
        "prior": known.precision if known else None,
        "prior_coverage": known.coverage if known else None,
        "evidence_level": est.evidence_level,
        "last_outcome_at": r.last_outcome_at,
        "updated_at": updated_at,
    }


async def source_reliability(s: AsyncSession, dimension: str | None = None) -> list[dict[str, Any]]:
    """Every learned row plus every key that only has a prior, grouped by dimension (most attempts first)."""
    q = sa.select(ResolverStat)
    if dimension:
        q = q.where(ResolverStat.dimension == dimension)
    rows = {(m.dimension, m.key): m for m in (await s.scalars(q)).all()}
    dims = [dimension] if dimension else list(P.DIMENSIONS)
    keys: set[tuple[str, str]] = set(rows)
    for d in dims:
        keys |= {(d, k) for k in P.priors_for(d)}
    order = {d: i for i, d in enumerate(P.DIMENSIONS)}
    out = []
    for d, k in keys:
        m = rows.get((d, k))
        out.append(
            reliability_row(d, k, row_from_model(m) if m is not None else None, m.updated_at if m else None)
        )
    out.sort(key=lambda x: (order.get(x["dimension"], len(order)), x["dimension"], -x["attempts"], x["key"]))
    return out
