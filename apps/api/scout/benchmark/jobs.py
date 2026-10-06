"""Job handler ``benchmark.run`` — advances a benchmark run in time slices (see ``runner.execute``)."""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from scout.benchmark import runner
from scout.jobs.registry import JobContext, job_handler

log = structlog.get_logger("benchmark.jobs")

JOB_TYPE = "benchmark.run"
MAX_ATTEMPTS = 3


@job_handler(JOB_TYPE, timeout_s=900, max_concurrency=2)
async def benchmark_run(ctx: JobContext) -> dict[str, Any]:
    run_id = uuid.UUID(str(ctx.payload["run_id"]))
    try:
        state = await runner.execute(run_id)
    except Exception as exc:
        if ctx.attempt >= MAX_ATTEMPTS:
            await runner.fail(run_id, f"{type(exc).__name__}: {exc}")
            log.warning("benchmark.run_failed", run_id=str(run_id), error=str(exc))
            return {"run_id": str(run_id), "status": "failed"}
        raise  # retried with backoff; stored results are kept (idempotent per item)
    if state == "continue":
        ctx.later(0.5)  # next slice, without consuming an attempt
    return {"run_id": str(run_id), "status": state}
