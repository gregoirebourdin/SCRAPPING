"""PostgreSQL job queue: idempotent enqueue, SKIP LOCKED claims, leases, retries, dead-letter (spec §9)."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import CAMPAIGN_TERMINAL, ErrorCategory, JobStatus
from scout.db.ids import uuid7
from scout.db.models import Campaign, Job, JobAttempt, JobDependency

JOBS_CHANNEL = "scout_jobs"
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 1800.0
BLOCKED_MAX_ATTEMPTS = 3


@dataclass
class ClaimedJob:
    id: uuid.UUID
    workspace_id: uuid.UUID
    campaign_id: uuid.UUID | None
    type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    priority: int


def backoff_seconds(attempt: int, *, blocked: bool = False) -> float:
    """Exponential backoff with full jitter; blocked pages wait much longer."""
    base = BACKOFF_BASE_S * (6 if blocked else 1)
    raw = min(BACKOFF_CAP_S, base * (2 ** max(0, attempt - 1)))
    return raw * random.uniform(0.5, 1.0)


async def enqueue(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    type: str,
    payload: dict[str, Any] | None = None,
    campaign_id: uuid.UUID | None = None,
    priority: int = 0,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    depends_on: list[uuid.UUID] | None = None,
    parent_job_id: uuid.UUID | None = None,
    status: JobStatus = JobStatus.pending,
) -> uuid.UUID | None:
    """Insert a job in the caller's transaction. Returns None when an identical active job exists."""
    job_id = uuid7()
    deps = [d for d in (depends_on or []) if d]
    values: dict[str, Any] = {
        "id": job_id,
        "workspace_id": workspace_id,
        "campaign_id": campaign_id,
        "type": type,
        "payload": payload or {},
        "priority": priority,
        "dedupe_key": dedupe_key,
        "max_attempts": max_attempts,
        "parent_job_id": parent_job_id,
        "status": status,
        "blocked_by_count": 0,
    }
    if run_after is not None:
        values["run_after"] = run_after
    stmt = pg_insert(Job).values(**values)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["workspace_id", "dedupe_key"],
            index_where=sa.text(
                "dedupe_key IS NOT NULL AND status NOT IN ('completed','failed','cancelled','dead_letter')"
            ),
        )
    res = await session.execute(stmt.returning(Job.id))
    inserted = res.scalar_one_or_none()
    if inserted is None:
        return None
    if deps:
        # Only count dependencies that are not finished yet.
        open_deps = (
            await session.scalars(
                sa.select(Job.id).where(Job.id.in_(deps), Job.status != JobStatus.completed)
            )
        ).all()
        for d in open_deps:
            session.add(JobDependency(job_id=job_id, depends_on_job_id=d))
        if open_deps:
            await session.execute(
                sa.update(Job).where(Job.id == job_id).values(blocked_by_count=len(open_deps))
            )
    await session.execute(sa.text("SELECT pg_notify(:ch, :t)"), {"ch": JOBS_CHANNEL, "t": type})
    return job_id


async def claim(worker_id: str, types: list[str], limit: int, lease_seconds: int) -> list[ClaimedJob]:
    if limit <= 0:
        return []
    sql = sa.text(
        """
        UPDATE jobs SET status = 'claimed', locked_by = :worker, attempts = attempts + 1,
               lease_expires_at = now() + make_interval(secs => :lease), heartbeat_at = now(),
               updated_at = now()
        WHERE id IN (
            SELECT j.id FROM jobs j
            LEFT JOIN campaigns c ON c.id = j.campaign_id
            WHERE j.status IN ('pending', 'retrying')
              AND j.run_after <= now()
              AND j.blocked_by_count = 0
              AND j.type = ANY(:types)
              AND (j.campaign_id IS NULL OR c.status IN ('planning', 'running')
                   OR j.type = ANY(:always_types))
            ORDER BY j.priority DESC, j.run_after
            LIMIT :limit
            FOR UPDATE OF j SKIP LOCKED
        )
        RETURNING id, workspace_id, campaign_id, type, payload, attempts, max_attempts, priority
        """
    )
    async with session_scope() as s:
        rows = (
            await s.execute(
                sql,
                {
                    "worker": worker_id,
                    "lease": lease_seconds,
                    "types": types,
                    "limit": limit,
                    # Jobs that must run even when their campaign is not running (bookkeeping).
                    "always_types": ["campaign.finalize"],
                },
            )
        ).all()
        for r in rows:
            s.add(JobAttempt(job_id=r.id, attempt_no=r.attempts, worker_id=worker_id))
    return [
        ClaimedJob(
            id=r.id,
            workspace_id=r.workspace_id,
            campaign_id=r.campaign_id,
            type=r.type,
            payload=r.payload or {},
            attempts=r.attempts,
            max_attempts=r.max_attempts,
            priority=r.priority,
        )
        for r in rows
    ]


async def mark_running(job_id: uuid.UUID) -> None:
    async with session_scope() as s:
        await s.execute(
            sa.update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.claimed)
            .values(status=JobStatus.running, started_at=sa.func.coalesce(Job.started_at, sa.func.now()))
        )


async def heartbeat(job_ids: list[uuid.UUID], worker_id: str, lease_seconds: int) -> None:
    if not job_ids:
        return
    async with session_scope() as s:
        await s.execute(
            sa.update(Job)
            .where(
                Job.id.in_(job_ids),
                Job.locked_by == worker_id,
                Job.status.in_([JobStatus.claimed, JobStatus.running]),
            )
            .values(
                heartbeat_at=sa.func.now(),
                lease_expires_at=sa.func.now() + timedelta(seconds=lease_seconds),
            )
        )


async def _close_attempt(
    s: AsyncSession,
    job_id: uuid.UUID,
    attempt_no: int,
    status: str,
    error: str | None,
    category: ErrorCategory | None,
) -> None:
    await s.execute(
        sa.update(JobAttempt)
        .where(
            JobAttempt.job_id == job_id, JobAttempt.attempt_no == attempt_no, JobAttempt.finished_at.is_(None)
        )
        .values(
            status=status,
            error=error,
            error_category=category,
            finished_at=sa.func.now(),
            duration_ms=sa.cast(
                sa.func.extract("epoch", sa.func.now() - JobAttempt.started_at) * 1000, sa.Integer
            ),
        )
    )


async def complete(job: ClaimedJob, worker_id: str, result: dict[str, Any] | None = None) -> None:
    async with session_scope() as s:
        updated = await s.execute(
            sa.update(Job)
            .where(Job.id == job.id, Job.locked_by == worker_id)
            .values(
                status=JobStatus.completed, result=result, finished_at=sa.func.now(), lease_expires_at=None
            )
            .returning(Job.id)
        )
        if updated.scalar_one_or_none() is None:
            return  # lease lost; another worker owns the job now
        await _close_attempt(s, job.id, job.attempts, "completed", None, None)
        # Unblock dependants.
        dependants = (
            await s.scalars(sa.select(JobDependency.job_id).where(JobDependency.depends_on_job_id == job.id))
        ).all()
        if dependants:
            await s.execute(
                sa.update(Job)
                .where(Job.id.in_(dependants), Job.blocked_by_count > 0)
                .values(blocked_by_count=Job.blocked_by_count - 1)
            )
            await s.execute(sa.text("SELECT pg_notify(:ch, 'deps')"), {"ch": JOBS_CHANNEL})


async def fail(
    job: ClaimedJob,
    worker_id: str,
    *,
    error: str,
    category: ErrorCategory,
    retryable: bool,
    blocked: bool = False,
) -> JobStatus:
    max_attempts = min(job.max_attempts, BLOCKED_MAX_ATTEMPTS) if blocked else job.max_attempts
    if retryable and job.attempts < max_attempts:
        status = JobStatus.retrying
        run_after = datetime.now(UTC) + timedelta(seconds=backoff_seconds(job.attempts, blocked=blocked))
    else:
        status = JobStatus.dead_letter if retryable else JobStatus.failed
        run_after = None
    async with session_scope() as s:
        values: dict[str, Any] = {
            "status": status,
            "last_error": error[:2000],
            "error_category": category,
            "lease_expires_at": None,
            "locked_by": None,
        }
        if run_after:
            values["run_after"] = run_after
        else:
            values["finished_at"] = sa.func.now()
        await s.execute(sa.update(Job).where(Job.id == job.id, Job.locked_by == worker_id).values(**values))
        await _close_attempt(s, job.id, job.attempts, status.value, error[:2000], category)
    return status


async def requeue_paused(job: ClaimedJob, worker_id: str) -> JobStatus:
    """Cooperative pause: hand the job back without consuming an attempt. When the campaign was stopped
    (completed / cancelled / …) rather than paused, the job is cancelled — a resume can never happen."""
    async with session_scope() as s:
        campaign_status = (
            await s.scalar(sa.select(Campaign.status).where(Campaign.id == job.campaign_id))
            if job.campaign_id
            else None
        )
        terminal = campaign_status is not None and campaign_status in CAMPAIGN_TERMINAL
        status = JobStatus.cancelled if terminal else JobStatus.paused
        values: dict[str, Any] = {
            "status": status,
            "attempts": Job.attempts - 1,
            "locked_by": None,
            "lease_expires_at": None,
        }
        if terminal:
            values["finished_at"] = sa.func.now()
        await s.execute(sa.update(Job).where(Job.id == job.id, Job.locked_by == worker_id).values(**values))
        await _close_attempt(s, job.id, job.attempts, status.value, None, None)
    return status


async def reschedule(
    job: ClaimedJob, worker_id: str, *, delay_s: float, payload: dict[str, Any] | None = None
) -> None:
    """Handler-requested re-run later (e.g. back-pressure) without consuming an attempt."""
    async with session_scope() as s:
        values: dict[str, Any] = {
            "status": JobStatus.pending,
            "attempts": Job.attempts - 1,
            "locked_by": None,
            "lease_expires_at": None,
            "run_after": datetime.now(UTC) + timedelta(seconds=delay_s),
        }
        if payload is not None:
            values["payload"] = payload
        await s.execute(sa.update(Job).where(Job.id == job.id, Job.locked_by == worker_id).values(**values))
        await _close_attempt(s, job.id, job.attempts, "rescheduled", None, None)


async def reap_expired_leases() -> int:
    """Jobs whose worker died (lease expired) go back to retrying — no job is lost."""
    async with session_scope() as s:
        res = await s.execute(
            sa.text(
                """
                UPDATE jobs SET
                  status = CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'retrying' END,
                  finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
                  last_error = 'lease expired (worker lost)', error_category = 'internal',
                  locked_by = NULL, lease_expires_at = NULL, run_after = now(), updated_at = now()
                WHERE status IN ('claimed', 'running') AND lease_expires_at < now()
                RETURNING id
                """
            )
        )
        ids = res.scalars().all()
        if ids:
            await s.execute(
                sa.update(JobAttempt)
                .where(JobAttempt.job_id.in_(ids), JobAttempt.finished_at.is_(None))
                .values(
                    status="lease_expired", finished_at=sa.func.now(), error_category=ErrorCategory.internal
                )
            )
    return len(ids)


async def set_campaign_jobs_paused(s: AsyncSession, campaign_id: uuid.UUID) -> None:
    await s.execute(
        sa.update(Job)
        .where(Job.campaign_id == campaign_id, Job.status.in_([JobStatus.pending, JobStatus.retrying]))
        .values(status=JobStatus.paused)
    )


async def resume_campaign_jobs(s: AsyncSession, campaign_id: uuid.UUID) -> None:
    await s.execute(
        sa.update(Job)
        .where(Job.campaign_id == campaign_id, Job.status == JobStatus.paused)
        .values(status=JobStatus.pending, run_after=sa.func.now())
    )
    await s.execute(sa.text("SELECT pg_notify(:ch, 'resume')"), {"ch": JOBS_CHANNEL})


async def cancel_campaign_jobs(s: AsyncSession, campaign_id: uuid.UUID) -> None:
    await s.execute(
        sa.update(Job)
        .where(
            Job.campaign_id == campaign_id,
            Job.status.in_([JobStatus.pending, JobStatus.retrying, JobStatus.paused]),
            Job.type != "campaign.finalize",
        )
        .values(status=JobStatus.cancelled, finished_at=sa.func.now())
    )


async def job_counts(s: AsyncSession, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await s.execute(
            sa.select(Job.type, Job.status, sa.func.count())
            .where(Job.campaign_id == campaign_id)
            .group_by(Job.type, Job.status)
        )
    ).all()
    out: dict[str, int] = {}
    for t, st, n in rows:
        out[f"{t}:{st}"] = n
    return out
