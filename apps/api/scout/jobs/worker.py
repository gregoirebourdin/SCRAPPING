"""Async job worker: claims with SKIP LOCKED, heartbeats leases, reaps dead workers, LISTENs for wakeups."""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from contextlib import suppress
from typing import Any

import asyncpg
import structlog

from scout.config import get_settings
from scout.db.engine import normalize_async_url
from scout.db.enums import ErrorCategory
from scout.errors import BlockedError, CampaignPaused, JobError
from scout.jobs import queue
from scout.jobs.registry import JobContext, get_handler, handler_types
from scout.services.usage import UsageContext, usage_scope

log = structlog.get_logger("worker")


class Worker:
    def __init__(self, slots: int | None = None, types: list[str] | None = None) -> None:
        s = get_settings()
        self.slots = slots or s.worker_slots
        self.lease = s.worker_lease_seconds
        self.poll = s.worker_poll_interval
        self.worker_id = s.worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.types = types
        self._running: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._per_type: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._listen_conn: asyncpg.Connection | None = None

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._claim_loop(), name="worker-claim"),
            asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat"),
            asyncio.create_task(self._reaper_loop(), name="worker-reaper"),
            asyncio.create_task(self._listen_loop(), name="worker-listen"),
        ]
        log.info("worker.started", worker_id=self.worker_id, slots=self.slots)

    async def stop(self, grace_s: float = 20.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._tasks:
            t.cancel()
        if self._running:
            _done, pending = await asyncio.wait(list(self._running.values()), timeout=grace_s)
            for t in pending:
                t.cancel()
        if self._listen_conn is not None:
            with suppress(Exception):
                await self._listen_conn.close()
        log.info("worker.stopped", worker_id=self.worker_id)

    async def run_until_idle(self, timeout_s: float = 120.0, idle_rounds: int = 3) -> None:
        """Tests/CLI: process jobs until the queue stays empty for a few polls."""
        deadline = time.monotonic() + timeout_s
        idle = 0
        while time.monotonic() < deadline:
            claimed = await self._claim_once()
            if self._running:
                await asyncio.wait(list(self._running.values()), timeout=0.5)
            if not claimed and not self._running:
                idle += 1
                if idle >= idle_rounds:
                    return
                await asyncio.sleep(0.2)
            else:
                idle = 0
        raise TimeoutError("worker did not become idle in time")

    # ---------------------------------------------------------------- loops
    async def _claim_loop(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = await self._claim_once()
            except Exception as exc:  # DB hiccup: back off, never crash the loop
                log.warning("worker.claim_error", error=str(exc))
                claimed = 0
                await asyncio.sleep(2)
            if claimed == 0:
                self._wake.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll)

    async def _claim_once(self) -> int:
        free = self.slots - len(self._running)
        if free <= 0:
            return 0
        types = self.types or handler_types()
        # Respect per-handler concurrency caps.
        eligible = []
        for t in types:
            spec = get_handler(t)
            if spec and spec.max_concurrency is not None and self._per_type.get(t, 0) >= spec.max_concurrency:
                continue
            eligible.append(t)
        if not eligible:
            return 0
        jobs = await queue.claim(self.worker_id, eligible, free, self.lease)
        for job in jobs:
            self._per_type[job.type] = self._per_type.get(job.type, 0) + 1
            task = asyncio.create_task(self._execute(job), name=f"job-{job.type}-{job.id}")
            self._running[job.id] = task
        return len(jobs)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(max(5, self.lease / 3))
            with suppress(Exception):
                await queue.heartbeat(list(self._running.keys()), self.worker_id, self.lease)

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(15)
            try:
                n = await queue.reap_expired_leases()
                if n:
                    log.warning("worker.reaped_leases", count=n)
                    self._wake.set()
            except Exception as exc:
                log.warning("worker.reaper_error", error=str(exc))

    async def _listen_loop(self) -> None:
        """LISTEN on the direct URL (NOTIFY doesn't traverse PgBouncer); polling remains the fallback."""
        url = normalize_async_url(get_settings().direct_url).replace("postgresql+asyncpg://", "postgresql://")
        while not self._stop.is_set():
            try:
                self._listen_conn = await asyncpg.connect(url)
                await self._listen_conn.add_listener(queue.JOBS_CHANNEL, lambda *_: self._wake.set())
                while not self._stop.is_set() and not self._listen_conn.is_closed():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("worker.listen_unavailable", error=str(exc))
                await asyncio.sleep(10)

    # ---------------------------------------------------------------- execution
    async def _execute(self, job: queue.ClaimedJob) -> None:
        spec = get_handler(job.type)
        structlog.contextvars.bind_contextvars(
            job_id=str(job.id),
            job_type=job.type,
            workspace_id=str(job.workspace_id),
            campaign_id=str(job.campaign_id) if job.campaign_id else None,
        )
        try:
            if spec is None:
                await queue.fail(
                    job,
                    self.worker_id,
                    error=f"no handler for {job.type}",
                    category=ErrorCategory.internal,
                    retryable=False,
                )
                return
            await queue.mark_running(job.id)
            ctx = JobContext(
                job_id=job.id,
                workspace_id=job.workspace_id,
                campaign_id=job.campaign_id,
                type=job.type,
                payload=job.payload,
                attempt=job.attempts,
                worker_id=self.worker_id,
            )
            started = time.monotonic()
            try:
                with usage_scope(UsageContext(job.workspace_id, job.campaign_id, job.id)):
                    result = await asyncio.wait_for(spec.fn(ctx), timeout=spec.timeout_s)
            except CampaignPaused:
                await queue.requeue_paused(job, self.worker_id)
                return
            except TimeoutError:
                await queue.fail(
                    job,
                    self.worker_id,
                    error=f"timed out after {spec.timeout_s}s",
                    category=ErrorCategory.timeout,
                    retryable=True,
                )
                log.warning("job.timeout")
                return
            except JobError as exc:
                status = await queue.fail(
                    job,
                    self.worker_id,
                    error=str(exc),
                    category=exc.category,
                    retryable=exc.retryable,
                    blocked=isinstance(exc, BlockedError),
                )
                log.warning("job.failed", error=str(exc), category=exc.category, status=status)
                return
            except Exception as exc:  # unexpected: retry with backoff, then dead-letter
                status = await queue.fail(
                    job,
                    self.worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                    category=ErrorCategory.internal,
                    retryable=True,
                )
                log.exception("job.crashed", status=status)
                return
            if ctx.reschedule_after is not None:
                await queue.reschedule(
                    job, self.worker_id, delay_s=ctx.reschedule_after, payload=ctx.reschedule_payload
                )
            else:
                await queue.complete(job, self.worker_id, result=result)
            log.debug("job.done", ms=int((time.monotonic() - started) * 1000))
        finally:
            self._running.pop(job.id, None)
            self._per_type[job.type] = max(0, self._per_type.get(job.type, 1) - 1)
            structlog.contextvars.clear_contextvars()
            self._wake.set()
