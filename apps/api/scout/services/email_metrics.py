"""Production metrics for the Email Intelligence Engine (Sources & health page, benchmark comparisons)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import VerificationRequestStatus
from scout.db.models import EmailResolution, EmailResolverStat, EmailVerificationRequest, SmtpHealth
from scout.email.confidence import RESOLVER_PRIORS
from scout.email.stats import PRIOR_STRENGTH


async def email_metrics(s: AsyncSession, workspace_id: uuid.UUID, *, hours: int = 24) -> dict[str, Any]:
    window = sa.text(f"now() - interval '{int(hours)} hours'")
    R = EmailResolution
    base = sa.select(R).where(R.workspace_id == workspace_id, R.created_at >= window).subquery()
    totals = (
        await s.execute(
            sa.select(
                sa.func.count(),
                sa.func.count().filter(base.c.path == "cache"),
                sa.func.count().filter(base.c.path == "fast"),
                sa.func.count().filter(base.c.path == "deep"),
                sa.func.avg(base.c.duration_ms),
                sa.func.percentile_cont(0.5).within_group(base.c.duration_ms),
                sa.func.percentile_cont(0.95).within_group(base.c.duration_ms),
                sa.func.coalesce(sa.func.sum(base.c.smtp_probes), 0),
                sa.func.avg(base.c.candidates_considered),
                sa.func.count().filter(base.c.cache_hits["profile"].astext == "true"),
                sa.func.coalesce(sa.func.sum(base.c.cost_usd), 0),
                sa.func.min(base.c.created_at),
                sa.func.max(base.c.created_at),
            )
        )
    ).one()
    (n, n_cache, n_fast, n_deep, avg_ms, p50, p95, probes, avg_cands, profile_hits, cost, t_min, t_max) = (
        totals
    )
    by_status: dict[Any, int] = dict(
        (await s.execute(sa.select(base.c.status, sa.func.count()).group_by(base.c.status))).all()  # type: ignore[arg-type]
    )
    minutes = max(1.0, ((t_max - t_min).total_seconds() / 60.0) if t_min and t_max else float(hours) * 60.0)
    resolved = sum(v for k, v in by_status.items() if str(k) in ("SAFE", "LIKELY_SAFE"))
    non_cache = (n_fast or 0) + (n_deep or 0)

    pending: dict[Any, int] = dict(
        (
            await s.execute(
                sa.select(EmailVerificationRequest.status, sa.func.count())
                .where(
                    EmailVerificationRequest.workspace_id == workspace_id,
                    EmailVerificationRequest.status.in_(
                        [
                            VerificationRequestStatus.pending,
                            VerificationRequestStatus.processing,
                            VerificationRequestStatus.retry,
                        ]
                    ),
                )
                .group_by(EmailVerificationRequest.status)
            )
        ).all()  # type: ignore[arg-type]
    )
    health = (await s.scalars(sa.select(SmtpHealth).order_by(SmtpHealth.scope))).all()
    resolvers = (
        await s.scalars(
            sa.select(EmailResolverStat)
            .where(EmailResolverStat.dimension == "resolver")
            .order_by(EmailResolverStat.attempts.desc())
        )
    ).all()

    def prec(r: EmailResolverStat) -> float | None:
        judged = r.confirmed_correct + r.confirmed_wrong
        if judged == 0:
            return None
        prior = RESOLVER_PRIORS.get(r.key, 0.5)
        return round((r.confirmed_correct + PRIOR_STRENGTH * prior) / (judged + PRIOR_STRENGTH), 3)

    return {
        "window_hours": hours,
        "resolutions": int(n or 0),
        "by_path": {"cache": int(n_cache or 0), "fast": int(n_fast or 0), "deep": int(n_deep or 0)},
        "by_status": {str(k): int(v) for k, v in by_status.items()},
        "avg_ms": round(float(avg_ms), 1) if avg_ms is not None else None,
        "p50_ms": round(float(p50), 1) if p50 is not None else None,
        "p95_ms": round(float(p95), 1) if p95 is not None else None,
        "smtp_probes": int(probes or 0),
        "smtp_fallback_rate": round((n_deep or 0) / non_cache, 3) if non_cache else None,
        "cache_hit_rate": round(((n_cache or 0) + (profile_hits or 0)) / n, 3) if n else None,
        "avg_candidates": round(float(avg_cands), 2) if avg_cands is not None else None,
        "resolved_per_minute": round(resolved / minutes, 2) if n else None,
        "cost_usd": float(cost or 0),
        "pending_deep": {str(k): int(v) for k, v in pending.items()},
        "smtp_health": [
            {
                "scope": h.scope,
                "state": h.state.value,
                "reason": h.reason,
                "blocked_until": h.blocked_until,
                "updated_at": h.updated_at,
            }
            for h in health
        ],
        "resolvers": [
            {
                "resolver": r.key,
                "attempts": r.attempts,
                "confirmed_correct": r.confirmed_correct,
                "confirmed_wrong": r.confirmed_wrong,
                "precision": prec(r),
                "avg_ms": round(r.latency_ms_total / r.attempts, 1) if r.attempts else None,
            }
            for r in resolvers
        ],
    }
