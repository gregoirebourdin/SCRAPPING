"""Job queue: claim/complete, retry with backoff, dead-letter, lease reaping, dependencies, idempotency."""

from __future__ import annotations

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import ErrorCategory, JobStatus
from scout.db.models import Job, JobAttempt
from scout.jobs import queue
from scout.jobs.registry import JobContext, job_handler
from scout.jobs.worker import Worker

pytestmark = __import__("pytest").mark.integration


async def _status(job_id):
    async with session_scope() as s:
        return await s.scalar(sa.select(Job.status).where(Job.id == job_id))


async def test_enqueue_is_idempotent_with_dedupe_key(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        a = await queue.enqueue(s, workspace_id=ws, type="t.noop", dedupe_key="k1")
        b = await queue.enqueue(s, workspace_id=ws, type="t.noop", dedupe_key="k1")
    assert a is not None and b is None


async def test_claim_complete_and_attempt_rows(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        jid = await queue.enqueue(s, workspace_id=ws, type="t.noop")
    jobs = await queue.claim("w1", ["t.noop"], 10, 60)
    assert [j.id for j in jobs] == [jid]
    # A second claim must not return the same job (SKIP LOCKED + status change).
    assert await queue.claim("w2", ["t.noop"], 10, 60) == []
    await queue.complete(jobs[0], "w1", {"ok": True})
    assert await _status(jid) == JobStatus.completed
    async with session_scope() as s:
        att = (await s.scalars(sa.select(JobAttempt).where(JobAttempt.job_id == jid))).all()
    assert len(att) == 1 and att[0].status == "completed"


async def test_retry_then_dead_letter(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        jid = await queue.enqueue(s, workspace_id=ws, type="t.flaky", max_attempts=2)
    job = (await queue.claim("w1", ["t.flaky"], 1, 60))[0]
    st = await queue.fail(job, "w1", error="boom", category=ErrorCategory.network, retryable=True)
    assert st == JobStatus.retrying
    async with session_scope() as s:
        await s.execute(sa.update(Job).where(Job.id == jid).values(run_after=sa.func.now()))
    job = (await queue.claim("w1", ["t.flaky"], 1, 60))[0]
    assert job.attempts == 2
    st = await queue.fail(job, "w1", error="boom", category=ErrorCategory.network, retryable=True)
    assert st == JobStatus.dead_letter


async def test_permanent_error_fails_immediately(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        await queue.enqueue(s, workspace_id=ws, type="t.bad")
    job = (await queue.claim("w1", ["t.bad"], 1, 60))[0]
    st = await queue.fail(job, "w1", error="invalid", category=ErrorCategory.validation, retryable=False)
    assert st == JobStatus.failed


async def test_crashed_worker_lease_is_reaped(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        jid = await queue.enqueue(s, workspace_id=ws, type="t.long")
    await queue.claim("dead-worker", ["t.long"], 1, 60)
    async with session_scope() as s:
        await s.execute(
            sa.update(Job)
            .where(Job.id == jid)
            .values(lease_expires_at=sa.func.now() - sa.text("interval '1 minute'"))
        )
    assert await queue.reap_expired_leases() == 1
    assert await _status(jid) == JobStatus.retrying
    again = await queue.claim("w2", ["t.long"], 1, 60)
    assert again and again[0].id == jid


async def test_dependencies_block_until_parent_completes(workspace):
    ws, _ = workspace
    async with session_scope() as s:
        parent = await queue.enqueue(s, workspace_id=ws, type="t.parent")
        child = await queue.enqueue(s, workspace_id=ws, type="t.child", depends_on=[parent])
    assert await queue.claim("w", ["t.child"], 5, 60) == []
    pj = (await queue.claim("w", ["t.parent"], 5, 60))[0]
    await queue.complete(pj, "w")
    cj = await queue.claim("w", ["t.child"], 5, 60)
    assert cj and cj[0].id == child


async def test_worker_runs_handlers_and_retries(workspace):
    ws, _ = workspace
    calls = {"n": 0}

    @job_handler("t.worker_flaky")
    async def flaky(ctx: JobContext):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"done": True}

    async with session_scope() as s:
        jid = await queue.enqueue(s, workspace_id=ws, type="t.worker_flaky")
    w = Worker(slots=2, types=["t.worker_flaky"])
    await w.run_until_idle(timeout_s=10)
    assert await _status(jid) == JobStatus.retrying
    async with session_scope() as s:
        await s.execute(sa.update(Job).where(Job.id == jid).values(run_after=sa.func.now()))
    await w.run_until_idle(timeout_s=10)
    assert await _status(jid) == JobStatus.completed and calls["n"] == 2


async def test_stopping_worker_hands_unfinished_jobs_back_at_once(workspace):
    import asyncio

    ws, _ = workspace
    started = asyncio.Event()

    @job_handler("t.worker_slow")
    async def slow(ctx: JobContext):
        started.set()
        await asyncio.sleep(60)  # a deploy lands in the middle of this job
        return {"done": True}

    async with session_scope() as s:
        jid = await queue.enqueue(s, workspace_id=ws, type="t.worker_slow")
    w = Worker(slots=1, types=["t.worker_slow"])
    assert await w._claim_once() == 1
    await asyncio.wait_for(started.wait(), timeout=5)
    await w.stop(grace_s=0.2)
    async with session_scope() as s:
        job = await s.get(Job, jid)
        assert job is not None
        assert job.status == JobStatus.pending and job.locked_by is None and job.attempts == 0
    # due now: the next worker takes it immediately, no lease to wait for
    again = await queue.claim("w2", ["t.worker_slow"], 1, 60)
    assert again and again[0].id == jid and again[0].attempts == 1
    # releasing never touches a job another worker holds
    assert await queue.release([jid], "someone-else") == 0
