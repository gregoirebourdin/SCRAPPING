"""Benchmark harness service: datasets (import / list / delete), runs (start / list / results / compare).

Workspace-scoped: every lookup filters by workspace, so foreign ids read as "not found".
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.benchmark import dataset as ds
from scout.benchmark.metrics import METRIC_DEFS
from scout.db.benchmark_models import (
    BENCHMARK_RUN_TERMINAL,
    BenchmarkDataset,
    BenchmarkItem,
    BenchmarkKind,
    BenchmarkMode,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkRunStatus,
)
from scout.db.enums import JobStatus
from scout.db.models import Job
from scout.errors import NotFound, ValidationFailed

LIVE_DEFAULT_MAX_COST = 1.0
LIVE_HARD_MAX_COST = 25.0
LIVE_DEFAULT_MAX_ITEMS = 200
LIVE_HARD_MAX_ITEMS = 500
MAX_COMPARE = 6

_LIVE_KEYS = {
    "crawl": bool,
    "crawl_max_age_days": int,
    "resolve_website": bool,
    "people_ai": bool,
    "max_people": int,
    "email": str,
    "deep_wait_s": float,
    "enrichment": bool,
    "enrichment_ai": bool,
    "rel_tol": float,
    "max_cost_usd": float,
    "concurrency": int,
    "max_items": int,
    "feed_learning": bool,
}
_REGISTRY_KEYS = {"rel_tol": float, "feed_learning": bool, "max_items": int}


def _dataset_error(exc: ds.DatasetError) -> ValidationFailed:
    return ValidationFailed(str(exc), hint="Fix the listed rows and import again", errors=exc.errors)


# =============================================================================================
# Datasets
# =============================================================================================


def parse_import(
    *,
    kind: str,
    csv: str | None,
    items: Sequence[dict[str, Any]] | None,
    people_exhaustive: bool,
    company_input: str,
) -> ds.ParsedDataset:
    if kind not in BenchmarkKind.__members__:
        raise ValidationFailed(f"Unknown dataset kind {kind!r}")
    if company_input not in ("domain", "name"):
        raise ValidationFailed("company_input must be 'domain' or 'name'")
    k: Literal["leads", "email", "enrichment"] = kind  # type: ignore[assignment]
    ci: Literal["domain", "name"] = company_input  # type: ignore[assignment]
    if bool(csv) == bool(items):
        raise ValidationFailed("Send either `csv` (text) or `items` (JSON), not both")
    try:
        if csv:
            return ds.parse_csv(csv, kind=k, people_exhaustive=people_exhaustive, company_input=ci)
        raw = [dict(it) for it in items or []]
        for it in raw:
            exp = it.get("expected")
            if isinstance(exp, dict) and "people_exhaustive" not in exp:
                exp["people_exhaustive"] = people_exhaustive
        return ds.ParsedDataset(items=ds.validate_items(raw, kind=k, company_input=ci), rows=len(raw))
    except ds.DatasetError as exc:
        raise _dataset_error(exc) from exc


def preview(parsed: ds.ParsedDataset) -> dict[str, Any]:
    return {
        "summary": ds.summarize(parsed.items),
        "mapping": parsed.mapping,
        "warnings": parsed.warnings,
        "rows": parsed.rows,
        "sample": parsed.items[:5],
    }


async def create_dataset(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str,
    kind: str,
    description: str | None,
    parsed: ds.ParsedDataset,
    people_exhaustive: bool,
    company_input: str,
    columns: dict[str, Any] | None,
    source: str,
    user_id: str | None,
) -> BenchmarkDataset:
    name = (name or "").strip()[:120]
    if not name:
        raise ValidationFailed("A dataset name is required")
    dset = BenchmarkDataset(
        workspace_id=workspace_id,
        name=name,
        kind=BenchmarkKind(kind),
        description=(description or "").strip()[:2000] or None,
        item_count=len(parsed.items),
        meta={
            "source": source,
            "mapping": parsed.mapping,
            "warnings": parsed.warnings[:50],
            "rows": parsed.rows,
            "people_exhaustive": people_exhaustive,
            "company_input": company_input,
            "columns": columns or {},
            "summary": ds.summarize(parsed.items),
        },
        created_by=user_id,
    )
    s.add(dset)
    await s.flush()
    for start in range(0, len(parsed.items), 500):
        await s.execute(
            sa.insert(BenchmarkItem),
            [
                {
                    "id": uuid.uuid4(),
                    "dataset_id": dset.id,
                    "ordinal": start + i + 1,
                    "label": (it.get("label") or "")[:300] or None,
                    "input": it["input"],
                    "expected": it["expected"],
                }
                for i, it in enumerate(parsed.items[start : start + 500])
            ],
        )
    return dset


def dataset_out(d: BenchmarkDataset, *, runs: int = 0, last_run_at: Any = None) -> dict[str, Any]:
    meta = d.meta or {}
    return {
        "id": d.id,
        "name": d.name,
        "kind": d.kind.value,
        "description": d.description,
        "item_count": d.item_count,
        "summary": meta.get("summary") or {},
        "people_exhaustive": meta.get("people_exhaustive", True),
        "company_input": meta.get("company_input", "domain"),
        "columns": meta.get("columns") or {},
        "warnings": meta.get("warnings") or [],
        "source": meta.get("source"),
        "runs": runs,
        "last_run_at": last_run_at,
        "created_by": d.created_by,
        "created_at": d.created_at,
    }


async def _dataset(s: AsyncSession, workspace_id: uuid.UUID, dataset_id: uuid.UUID) -> BenchmarkDataset:
    d = await s.get(BenchmarkDataset, dataset_id)
    if d is None or d.workspace_id != workspace_id:
        raise NotFound("Dataset not found")
    return d


async def list_datasets(s: AsyncSession, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    stats = (
        sa.select(
            BenchmarkRun.dataset_id,
            sa.func.count().label("runs"),
            sa.func.max(BenchmarkRun.created_at).label("last_run_at"),
        )
        .where(BenchmarkRun.workspace_id == workspace_id, BenchmarkRun.dataset_id.is_not(None))
        .group_by(BenchmarkRun.dataset_id)
        .subquery()
    )
    rows = (
        await s.execute(
            sa.select(BenchmarkDataset, stats.c.runs, stats.c.last_run_at)
            .outerjoin(stats, stats.c.dataset_id == BenchmarkDataset.id)
            .where(BenchmarkDataset.workspace_id == workspace_id)
            .order_by(BenchmarkDataset.created_at.desc())
            .limit(500)
        )
    ).all()
    return [dataset_out(d, runs=int(n or 0), last_run_at=last) for d, n, last in rows]


async def get_dataset(
    s: AsyncSession, workspace_id: uuid.UUID, dataset_id: uuid.UUID, *, offset: int = 0, limit: int = 50
) -> dict[str, Any]:
    d = await _dataset(s, workspace_id, dataset_id)
    items = (
        await s.scalars(
            sa.select(BenchmarkItem)
            .where(BenchmarkItem.dataset_id == d.id)
            .order_by(BenchmarkItem.ordinal)
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
        )
    ).all()
    return {
        **dataset_out(d),
        "items": [
            {"id": i.id, "ordinal": i.ordinal, "label": i.label, "input": i.input, "expected": i.expected}
            for i in items
        ],
        "offset": offset,
    }


async def delete_dataset(s: AsyncSession, workspace_id: uuid.UUID, dataset_id: uuid.UUID) -> None:
    """Deletes the dataset and its items; past runs keep their results (expected values are snapshotted)."""
    d = await _dataset(s, workspace_id, dataset_id)
    active = await s.scalar(
        sa.select(sa.func.count())
        .select_from(BenchmarkRun)
        .where(
            BenchmarkRun.dataset_id == d.id,
            BenchmarkRun.status.in_((BenchmarkRunStatus.queued, BenchmarkRunStatus.running)),
        )
    )
    if active:
        raise ValidationFailed("A run on this dataset is in progress — cancel it first")
    await s.delete(d)


# =============================================================================================
# Runs
# =============================================================================================


def _coerce(config: dict[str, Any], allowed: dict[str, type]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (config or {}).items():
        if k not in allowed:
            raise ValidationFailed(f"Unknown option {k!r}", hint=f"Allowed: {', '.join(sorted(allowed))}")
        t = allowed[k]
        try:
            if t is bool:
                if not isinstance(v, bool):
                    raise TypeError
                out[k] = v
            else:
                out[k] = t(v)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(f"Invalid value for {k!r}") from exc
    return out


def validate_config(
    mode: str, config: dict[str, Any] | None, *, item_count: int
) -> tuple[dict[str, Any], str]:
    """Returns (config, default strategy label)."""
    if mode == BenchmarkMode.live:
        cfg = _coerce(config or {}, _LIVE_KEYS)
        cfg["max_cost_usd"] = float(cfg.get("max_cost_usd", LIVE_DEFAULT_MAX_COST))
        if not 0 < cfg["max_cost_usd"] <= LIVE_HARD_MAX_COST:
            raise ValidationFailed(f"max_cost_usd must be in (0, {LIVE_HARD_MAX_COST}]")
        cfg["max_items"] = min(
            int(cfg.get("max_items", LIVE_DEFAULT_MAX_ITEMS)), LIVE_HARD_MAX_ITEMS, item_count
        )
        if cfg["max_items"] < 1:
            raise ValidationFailed("max_items must be ≥ 1")
        cfg["concurrency"] = max(1, min(8, int(cfg.get("concurrency", 3))))
        if cfg.get("email", "fast") not in ("off", "fast", "fast_deep"):
            raise ValidationFailed("email must be off, fast or fast_deep")
        cfg["max_people"] = max(1, min(25, int(cfg.get("max_people", 10))))
        cfg["deep_wait_s"] = max(0.0, min(600.0, float(cfg.get("deep_wait_s", 90.0))))
        cfg["crawl_max_age_days"] = max(0, min(365, int(cfg.get("crawl_max_age_days", 30))))
        parts = [
            f"email:{cfg.get('email', 'fast')}",
            *(["people:ai"] if cfg.get("people_ai") else []),
            *(["enrich:ai"] if cfg.get("enrichment_ai") else []),
            *(["no-crawl"] if cfg.get("crawl") is False else []),
        ]
        return cfg, " · ".join(parts)
    if mode == BenchmarkMode.registry:
        cfg = _coerce(config or {}, _REGISTRY_KEYS)
        cfg["max_items"] = min(int(cfg.get("max_items", item_count)), item_count)
        return cfg, "registry snapshot"
    raise ValidationFailed(f"Unknown mode {mode!r}")


async def start_run(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    mode: str,
    dataset_id: uuid.UUID | None,
    suite: str | None,
    strategy: str | None,
    config: dict[str, Any] | None,
    user_id: str | None,
    enqueue: bool = True,
) -> BenchmarkRun:
    """Create a run and queue its job (``enqueue=False``: the caller executes it in-process, e.g. the CLI)."""
    from scout.benchmark.jobs import JOB_TYPE, MAX_ATTEMPTS
    from scout.jobs import queue

    if mode == BenchmarkMode.suite:
        from scout.benchmark.registry import get_suite, load_suites

        load_suites()
        spec = get_suite(suite or "")
        if spec is None:
            raise ValidationFailed(f"Unknown suite {suite!r}")
        cfg = dict(config or {})
        wanted = cfg.get("strategies")
        if wanted is not None:
            if not isinstance(wanted, list) or not all(isinstance(x, str) for x in wanted):
                raise ValidationFailed("strategies must be a list of names")
            unknown = [x for x in wanted if spec.strategies and x not in spec.strategies]
            if unknown:
                raise ValidationFailed(
                    f"Unknown strategies: {unknown}", hint=f"Available: {list(spec.strategies)}"
                )
        run = BenchmarkRun(
            workspace_id=workspace_id,
            suite_key=spec.key,
            mode=BenchmarkMode.suite,
            strategy=(strategy or ", ".join(wanted or spec.strategies) or "default")[:120],
            config=cfg,
            created_by=user_id,
        )
    else:
        if dataset_id is None:
            raise ValidationFailed("dataset_id is required for registry and live runs")
        d = await _dataset(s, workspace_id, dataset_id)
        cfg, label = validate_config(mode, config, item_count=d.item_count)
        run = BenchmarkRun(
            workspace_id=workspace_id,
            dataset_id=d.id,
            mode=BenchmarkMode(mode),
            strategy=(strategy or label)[:120],
            config=cfg,
            items_total=int(cfg.get("max_items") or d.item_count),
            created_by=user_id,
        )
    s.add(run)
    await s.flush()
    if not enqueue:
        return run
    run.job_id = await queue.enqueue(
        s,
        workspace_id=workspace_id,
        type=JOB_TYPE,
        payload={"run_id": str(run.id)},
        priority=1,
        dedupe_key=f"benchmark:{run.id}",
        max_attempts=MAX_ATTEMPTS,
    )
    return run


async def _reconcile(s: AsyncSession, run: BenchmarkRun) -> None:
    """A run whose job died (timeout kill, dead letter) is marked failed instead of spinning forever."""
    if run.status in BENCHMARK_RUN_TERMINAL or run.job_id is None:
        return
    job = await s.scalar(sa.select(Job.status).where(Job.id == run.job_id))
    if job in (JobStatus.failed, JobStatus.dead_letter, JobStatus.cancelled):
        last = await s.scalar(sa.select(Job.last_error).where(Job.id == run.job_id))
        run.status = BenchmarkRunStatus.failed
        run.error = run.error or f"Job stopped: {last or job.value}"[:1000]
        run.finished_at = sa.func.now()  # type: ignore[assignment]
        await s.flush()
        await s.refresh(run)


def run_out(r: BenchmarkRun, *, dataset_name: str | None = None, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": r.id,
        "dataset_id": r.dataset_id,
        "dataset_name": dataset_name,
        "suite_key": r.suite_key,
        "mode": r.mode.value,
        "strategy": r.strategy,
        "status": r.status.value,
        "items_total": r.items_total,
        "items_done": r.items_done,
        "progress": round(r.items_done / r.items_total, 4)
        if r.items_total
        else (1.0 if r.status == "completed" else 0.0),
        "cost_usd": float(r.cost_usd or 0),
        "duration_ms": r.duration_ms,
        "error": r.error,
        "created_by": r.created_by,
        "created_at": r.created_at,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "headline": {
            k: (r.metrics or {}).get(k)
            for k in (
                "person_precision",
                "person_recall",
                "email_precision",
                "email_recall",
                "enrichment_accuracy",
            )
            if (r.metrics or {}).get(k)
        },
    }
    if full:
        out["config"] = r.config
        out["metrics"] = r.metrics or {}
        out["strategies"] = r.strategies
        out["notes"] = r.notes or []
    return out


async def _run(s: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> BenchmarkRun:
    r = await s.get(BenchmarkRun, run_id)
    if r is None or r.workspace_id != workspace_id:
        raise NotFound("Run not found")
    return r


async def _dataset_names(s: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await s.execute(
            sa.select(BenchmarkDataset.id, BenchmarkDataset.name).where(BenchmarkDataset.id.in_(ids))
        )
    ).all()
    return {i: n for i, n in rows}


async def list_runs(
    s: AsyncSession, workspace_id: uuid.UUID, *, dataset_id: uuid.UUID | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    q = sa.select(BenchmarkRun).where(BenchmarkRun.workspace_id == workspace_id)
    if dataset_id:
        q = q.where(BenchmarkRun.dataset_id == dataset_id)
    rows = (await s.scalars(q.order_by(BenchmarkRun.created_at.desc()).limit(max(1, min(limit, 200))))).all()
    for r in rows:
        await _reconcile(s, r)
    names = await _dataset_names(s, {r.dataset_id for r in rows if r.dataset_id})
    return [run_out(r, dataset_name=names.get(r.dataset_id) if r.dataset_id else None) for r in rows]


async def get_run(s: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    r = await _run(s, workspace_id, run_id)
    await _reconcile(s, r)
    names = await _dataset_names(s, {r.dataset_id} if r.dataset_id else set())
    return run_out(r, dataset_name=names.get(r.dataset_id) if r.dataset_id else None, full=True)


async def run_results(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    offset: int = 0,
    limit: int = 50,
    errors_only: bool = False,
    strategy: str | None = None,
) -> dict[str, Any]:
    r = await _run(s, workspace_id, run_id)
    q = sa.select(BenchmarkResult).where(BenchmarkResult.run_id == r.id)
    if errors_only:
        q = q.where(
            sa.or_(BenchmarkResult.fp > 0, BenchmarkResult.fn > 0, BenchmarkResult.error.is_not(None))
        )
    if strategy:
        q = q.where(BenchmarkResult.strategy == strategy)
    total = int(await s.scalar(sa.select(sa.func.count()).select_from(q.subquery())) or 0)
    limit = max(1, min(limit, 200))
    rows = (
        await s.scalars(
            q.order_by(BenchmarkResult.ordinal, BenchmarkResult.strategy).offset(max(0, offset)).limit(limit)
        )
    ).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": x.id,
                "item_id": x.item_id,
                "ordinal": x.ordinal,
                "label": x.label,
                "strategy": x.strategy,
                "expected": x.expected,
                "actual": x.actual,
                "verdicts": x.verdicts,
                "fp": x.fp,
                "fn": x.fn,
                "latency_ms": x.latency_ms,
                "cost_usd": float(x.cost_usd or 0),
                "error": x.error,
            }
            for x in rows
        ],
    }


async def cancel_run(s: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    r = await _run(s, workspace_id, run_id)
    if r.status in (BenchmarkRunStatus.queued, BenchmarkRunStatus.running):
        if r.status == BenchmarkRunStatus.queued and r.job_id is not None:
            await s.execute(
                sa.update(Job)
                .where(Job.id == r.job_id, Job.status.in_((JobStatus.pending, JobStatus.retrying)))
                .values(status=JobStatus.cancelled, finished_at=sa.func.now())
            )
        r.status = BenchmarkRunStatus.cancelled
        r.finished_at = sa.func.now()  # type: ignore[assignment]
        await s.flush()
        await s.refresh(r)
    return run_out(r)


async def delete_run(s: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    r = await _run(s, workspace_id, run_id)
    if r.status in (BenchmarkRunStatus.queued, BenchmarkRunStatus.running):
        raise ValidationFailed("Cancel the run before deleting it")
    await s.delete(r)


async def compare_runs(
    s: AsyncSession, workspace_id: uuid.UUID, run_ids: Sequence[uuid.UUID]
) -> dict[str, Any]:
    """Measured metrics side by side (no verdict on which is better: read n and the intervals)."""
    ids = list(dict.fromkeys(run_ids))[:MAX_COMPARE]
    if len(ids) < 1:
        raise ValidationFailed("Pick at least one run")
    rows = (
        await s.scalars(
            sa.select(BenchmarkRun).where(BenchmarkRun.workspace_id == workspace_id, BenchmarkRun.id.in_(ids))
        )
    ).all()
    by_id = {r.id: r for r in rows}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        raise NotFound("Run not found", ids=missing)
    runs = [by_id[i] for i in ids]
    names = await _dataset_names(s, {r.dataset_id for r in runs if r.dataset_id})
    columns: list[dict[str, Any]] = []
    metric_sets: list[dict[str, Any]] = []
    for r in runs:
        base = run_out(r, dataset_name=names.get(r.dataset_id) if r.dataset_id else None)
        if r.strategies:  # a suite run contributes one column per strategy
            for name, m in r.strategies.items():
                columns.append({**base, "column": f"{r.id}:{name}", "strategy": name})
                metric_sets.append(m)
        else:
            columns.append({**base, "column": str(r.id)})
            metric_sets.append(r.metrics or {})
    order = list(METRIC_DEFS)

    def rank(k: str) -> tuple[int, int, str]:
        own = min((int(m[k].get("order", 10_000)) for m in metric_sets if k in m), default=10_000)
        return (order.index(k) if k in order else len(order), own, k)

    keys = sorted({k for m in metric_sets for k in m}, key=rank)
    metrics: list[dict[str, Any]] = []
    for k in keys:
        sample: dict[str, Any] = next((m[k] for m in metric_sets if k in m), {})
        metrics.append(
            {
                "key": k,
                "label": sample.get("label", k),
                "group": sample.get("group", "other"),
                "unit": sample.get("unit", "number"),
                "definition": sample.get("definition", ""),
                "order": len(metrics),
                "values": [
                    {kk: m[k].get(kk) for kk in ("value", "n", "k", "ci90")} if k in m else None
                    for m in metric_sets
                ],
            }
        )
    return {
        "columns": columns,
        "metrics": metrics,
        "note": "Measured values only. Differences within overlapping 90 % intervals are not conclusive.",
    }
