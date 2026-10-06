"""Benchmark run execution (job ``benchmark.run`` and CLI).

Dataset runs (registry / live) process items with bounded concurrency, store one result per item
(idempotent: a resumed or retried run never stores an item twice) and work in time slices so a long live
run re-schedules itself instead of hitting the job timeout. Live runs stop launching items when the run's
cost cap or the workspace budget is reached; unprocessed items are reported, not counted. Suite runs call
the registered suite once. Metrics are computed from all stored results when the run completes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.benchmark import feedback
from scout.benchmark.collect import ItemContext, LiveConfig, live_actual, registry_actual
from scout.benchmark.metrics import aggregate, compare_item, normalize_metrics
from scout.benchmark.registry import load_suites, run_suite
from scout.db.benchmark_models import (
    BENCHMARK_RUN_TERMINAL,
    BenchmarkDataset,
    BenchmarkItem,
    BenchmarkMode,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkRunStatus,
)
from scout.db.engine import session_scope
from scout.db.enums import UsageCategory
from scout.db.ids import uuid7
from scout.db.models import UsageEvent

log = structlog.get_logger("benchmark.runner")

SLICE_S = 240.0  # work per job invocation before re-scheduling (job timeout is larger)
REGISTRY_CONCURRENCY = 8
RESULT_BATCH = 500


def _now() -> datetime:
    return datetime.now(UTC)


async def _set(run_id: uuid.UUID, **values: Any) -> None:
    async with session_scope() as s:
        await s.execute(sa.update(BenchmarkRun).where(BenchmarkRun.id == run_id).values(**values))


async def _status(run_id: uuid.UUID) -> BenchmarkRunStatus | None:
    async with session_scope() as s:
        return await s.scalar(sa.select(BenchmarkRun.status).where(BenchmarkRun.id == run_id))


async def _item_cost(scope_id: uuid.UUID) -> tuple[float, float]:
    """(total, e-mail verification) usage recorded under this item's usage scope."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                sa.select(
                    UsageEvent.category, sa.func.coalesce(sa.func.sum(UsageEvent.estimated_cost_usd), 0)
                )
                .where(UsageEvent.job_id == scope_id)
                .group_by(UsageEvent.category)
            )
        ).all()
    total = sum(float(c) for _, c in rows)
    email = sum(float(c) for cat, c in rows if cat == UsageCategory.verification_request)
    return total, email


async def _process_item(
    run: BenchmarkRun,
    item: BenchmarkItem,
    *,
    kind: str,
    columns: dict[str, Any],
    cfg: LiveConfig,
    budget_ok: Any,
) -> dict[str, Any]:
    ctx = ItemContext(
        workspace_id=run.workspace_id,
        kind=kind,
        item_input=item.input or {},
        expected=item.expected or {},
        columns=columns,
        label=item.label or f"item {item.ordinal}",
        budget_ok=budget_ok,
    )
    scope_id = uuid7()
    t0 = time.monotonic()
    error: str | None = None
    actual: dict[str, Any]
    try:
        if run.mode == BenchmarkMode.live:
            from scout.services.usage import UsageContext, usage_scope

            with usage_scope(UsageContext(run.workspace_id, None, scope_id)):
                actual = await live_actual(ctx, cfg)
        else:
            actual = await registry_actual(ctx)
    except Exception as exc:  # an engine failure is a miss for this item, never a crashed run
        log.warning("benchmark.item_failed", run_id=str(run.id), item=item.ordinal, error=str(exc))
        error = f"{type(exc).__name__}: {exc}"[:500]
        actual = {"company": {"found": False}, "people": [], "company_emails": [], "enrichment": {}}
    latency_ms = int((time.monotonic() - t0) * 1000)
    cost, email_cost = (await _item_cost(scope_id)) if run.mode == BenchmarkMode.live else (0.0, 0.0)
    if not email_cost:
        email_cost = sum(float(r.get("cost_usd") or 0) for r in actual.get("email_resolutions") or [])
    verdicts = compare_item(item.expected or {}, actual, item.input or {}, rel_tol=cfg.rel_tol)
    verdicts["email_cost_usd"] = round(email_cost, 6)
    async with session_scope() as s:
        inserted = await s.scalar(
            pg_insert(BenchmarkResult)
            .values(
                {  # dict form: a column named `fn` would clash with values()'s own keyword
                    "id": uuid7(),
                    "run_id": run.id,
                    "item_id": item.id,
                    "ordinal": item.ordinal,
                    "label": item.label,
                    "expected": item.expected or {},
                    "actual": actual,
                    "verdicts": verdicts,
                    "fp": verdicts["fp"],
                    "fn": verdicts["fn"],
                    "latency_ms": latency_ms,
                    "cost_usd": Decimal(str(round(cost, 6))),
                    "error": error,
                }
            )
            .on_conflict_do_nothing(
                index_elements=["run_id", "item_id"], index_where=sa.text("item_id IS NOT NULL")
            )
            .returning(BenchmarkResult.id)
        )
        if inserted is not None:
            await s.execute(
                sa.update(BenchmarkRun)
                .where(BenchmarkRun.id == run.id)
                .values(
                    items_done=BenchmarkRun.items_done + 1,
                    cost_usd=BenchmarkRun.cost_usd + Decimal(str(round(cost, 6))),
                    updated_at=sa.func.now(),
                )
            )
    if inserted is not None and run.config.get("feed_learning", True) and not error:
        await feedback.feed(feedback.outcomes(item.expected or {}, actual, verdicts, item.input or {}))
    return {"cost": cost, "inserted": inserted is not None}


async def _pending_items(run: BenchmarkRun, limit: int) -> list[BenchmarkItem]:
    async with session_scope() as s:
        done = sa.select(BenchmarkResult.item_id).where(
            BenchmarkResult.run_id == run.id, BenchmarkResult.item_id.is_not(None)
        )
        rows = (
            await s.scalars(
                sa.select(BenchmarkItem)
                .where(BenchmarkItem.dataset_id == run.dataset_id, BenchmarkItem.id.not_in(done))
                .order_by(BenchmarkItem.ordinal)
                .limit(limit)
            )
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


async def _finalize(run_id: uuid.UUID, *, notes: list[str] | None = None) -> None:
    async with session_scope() as s:
        run = await s.get(BenchmarkRun, run_id)
        if run is None:
            return
        rows = (
            await s.execute(
                sa.select(
                    BenchmarkResult.verdicts,
                    BenchmarkResult.latency_ms,
                    BenchmarkResult.cost_usd,
                    BenchmarkResult.error,
                ).where(BenchmarkResult.run_id == run_id)
            )
        ).all()
        finished = _now()
        duration_ms = int((finished - (run.started_at or finished)).total_seconds() * 1000)
        outcomes = [
            {
                "verdicts": v or {},
                "latency_ms": lat,
                "cost_usd": float(c or 0),
                "email_cost_usd": float((v or {}).get("email_cost_usd") or 0),
                "error": err,
            }
            for v, lat, c, err in rows
        ]
        total_cost = sum(o["cost_usd"] for o in outcomes)
        run.metrics = aggregate(
            outcomes, mode=run.mode.value, total_cost_usd=total_cost, duration_ms=duration_ms
        )
        run.cost_usd = Decimal(str(round(total_cost, 6)))
        run.items_done = len(rows)
        run.duration_ms = duration_ms
        run.finished_at = finished
        run.notes = [*(run.notes or []), *(notes or [])]
        if run.status != BenchmarkRunStatus.cancelled:
            run.status = BenchmarkRunStatus.completed


async def _run_suite(run: BenchmarkRun) -> None:
    load_suites()
    assert run.suite_key
    result = await run_suite(run.suite_key, run.config or {})
    labels, definitions = result.labels, result.definitions
    metrics = normalize_metrics(result.metrics, result.samples, labels=labels, definitions=definitions)
    strategies = (
        {
            name: normalize_metrics(m, result.samples, labels=labels, definitions=definitions)
            for name, m in result.strategies.items()
        }
        if result.strategies
        else None
    )
    async with session_scope() as s:
        for start in range(0, len(result.items), RESULT_BATCH):
            batch = result.items[start : start + RESULT_BATCH]
            await s.execute(
                pg_insert(BenchmarkResult),
                [
                    {
                        "id": uuid7(),
                        "run_id": run.id,
                        "item_id": None,
                        "ordinal": start + i + 1,
                        "label": str(it.get("label") or f"#{start + i + 1}")[:300],
                        "strategy": it.get("strategy"),
                        "expected": it.get("expected")
                        if isinstance(it.get("expected"), dict)
                        else {"value": it.get("expected")},
                        "actual": it.get("actual")
                        if isinstance(it.get("actual"), dict)
                        else {"value": it.get("actual")},
                        "verdicts": {
                            k: v
                            for k, v in it.items()
                            if k not in ("label", "strategy", "expected", "actual", "latency_ms", "cost_usd")
                        },
                        "fp": int(it.get("fp") or 0),
                        "fn": int(it.get("fn") or 0),
                        "latency_ms": int(it["latency_ms"]) if it.get("latency_ms") is not None else None,
                        "cost_usd": Decimal(str(round(float(it.get("cost_usd") or 0), 6))),
                        "error": it.get("error"),
                    }
                    for i, it in enumerate(batch)
                ],
            )
        db_run = await s.get(BenchmarkRun, run.id)
        assert db_run is not None
        finished = _now()
        cost = metrics.get("cost_usd", {}).get("value") or 0
        db_run.metrics = metrics
        db_run.strategies = strategies
        db_run.notes = [*(db_run.notes or []), *result.notes]
        db_run.items_total = db_run.items_done = len(result.items)
        db_run.cost_usd = Decimal(str(round(float(cost), 6)))
        db_run.finished_at = finished
        db_run.duration_ms = int((finished - (db_run.started_at or finished)).total_seconds() * 1000)
        db_run.status = BenchmarkRunStatus.completed


async def execute(run_id: uuid.UUID, *, slice_s: float | None = SLICE_S) -> str:
    """Advance a run. Returns ``"continue"`` when items remain for another slice, else the final status."""
    async with session_scope() as s:
        run = await s.get(BenchmarkRun, run_id)
        if run is None:
            return "missing"
        cancelled_unscored = (
            run.status == BenchmarkRunStatus.cancelled and not run.metrics and run.mode != BenchmarkMode.suite
        )
        if run.status in BENCHMARK_RUN_TERMINAL and not cancelled_unscored:
            return run.status.value
        if run.status == BenchmarkRunStatus.queued:
            run.status = BenchmarkRunStatus.running
            run.started_at = _now()
        dataset = await s.get(BenchmarkDataset, run.dataset_id) if run.dataset_id else None
        await s.flush()
        s.expunge(run)
        if dataset is not None:
            s.expunge(dataset)
    if cancelled_unscored:  # cancelled between two slices: score what was processed
        await _finalize(run_id, notes=["Cancelled: remaining items not processed."])
        return BenchmarkRunStatus.cancelled.value
    if run.mode == BenchmarkMode.suite:
        await _run_suite(run)
        return "completed"
    if dataset is None:
        await _set(run_id, status=BenchmarkRunStatus.failed, error="Dataset deleted", finished_at=_now())
        return "failed"
    cfg = LiveConfig.from_dict(run.config or {})
    max_items = int(run.config.get("max_items") or run.items_total or dataset.item_count)
    max_cost = float(run.config.get("max_cost_usd") or 0)
    concurrency = (
        max(1, min(8, int(run.config.get("concurrency") or 3)))
        if run.mode == BenchmarkMode.live
        else REGISTRY_CONCURRENCY
    )
    columns = dict((dataset.meta or {}).get("columns") or {})
    spent = float(run.cost_usd or 0)
    stop_reason: str | None = None

    async def budget_ok() -> bool:
        if run.mode != BenchmarkMode.live:
            return True
        if max_cost and spent >= max_cost:
            return False
        from scout.services.usage import workspace_budget

        return not (await workspace_budget(run.workspace_id)).exceeded

    deadline = time.monotonic() + slice_s if slice_s else None
    async with session_scope() as s:
        done = int(
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(BenchmarkResult)
                .where(BenchmarkResult.run_id == run_id)
            )
            or 0
        )
    remaining = max(0, max_items - done)
    pending = await _pending_items(run, remaining) if remaining else []
    sem = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task[dict[str, Any]]] = []

    async def one(item: BenchmarkItem) -> dict[str, Any]:
        nonlocal spent
        async with sem:
            r = await _process_item(
                run, item, kind=dataset.kind.value, columns=columns, cfg=cfg, budget_ok=budget_ok
            )
            spent += r["cost"]
            return r

    launched = 0
    for item in pending:
        # at most `concurrency` items in flight: the slice deadline, cancellation and the cost cap are
        # checked once a slot is free, i.e. with the cost of the items that just finished
        while sum(1 for t in tasks if not t.done()) >= concurrency:
            await asyncio.wait([t for t in tasks if not t.done()], return_when=asyncio.FIRST_COMPLETED)
        if deadline is not None and launched and time.monotonic() >= deadline:
            break  # every slice makes progress (≥ 1 item), then yields
        if launched % 5 == 0 and await _status(run_id) == BenchmarkRunStatus.cancelled:
            stop_reason = "cancelled"
            break
        if not await budget_ok():
            stop_reason = "budget"
            break
        tasks.append(asyncio.create_task(one(item)))
        launched += 1
    if tasks:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        if failures:  # infrastructure errors (engine errors are scored as misses): retry the slice
            raise failures[0]
    left = len(pending) - launched
    if stop_reason is None and left > 0:
        return "continue"
    notes: list[str] = []
    if stop_reason == "budget":
        notes.append(f"Cost cap reached: {left} item(s) not processed (not counted in the metrics).")
    elif stop_reason == "cancelled":
        notes.append(f"Cancelled: {left} item(s) not processed.")
    if run.mode == BenchmarkMode.registry:
        notes.append(
            "Registry mode: times are lookup times and no cost is measured (the leads were produced earlier)."
        )
    if run.mode == BenchmarkMode.live and cfg.email == "fast_deep":
        notes.append("Deep (SMTP) verification runs in separate jobs: its cost is not attributed to items.")
    await _finalize(run_id, notes=notes)
    return (await _status(run_id) or BenchmarkRunStatus.completed).value


async def fail(run_id: uuid.UUID, error: str) -> None:
    await _set(run_id, status=BenchmarkRunStatus.failed, error=error[:1000], finished_at=_now())
