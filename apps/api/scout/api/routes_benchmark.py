"""Benchmark harness API (admin only): ground-truth datasets, runs, per-item diffs, run comparison.

Every endpoint requires the workspace admin role. Numbers are measured on the workspace's own ground
truth; the API never emits comparative claims about other products.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from scout.api.deps import Ctx
from scout.benchmark import dataset as ds
from scout.benchmark.metrics import METRIC_DEFS
from scout.db.engine import session_scope
from scout.db.enums import MemberRole
from scout.errors import ValidationFailed
from scout.services import benchmark as svc

router = APIRouter(tags=["benchmark"])

Kind = Literal["leads", "email", "enrichment"]


class DatasetImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    kind: Kind = "leads"
    description: str | None = Field(default=None, max_length=2000)
    csv: str | None = Field(
        default=None, max_length=ds.MAX_CSV_BYTES, description="CSV text (see GET /benchmark/template.csv)"
    )
    items: list[dict[str, Any]] | None = Field(
        default=None, max_length=ds.MAX_ITEMS, description="JSON items: {label?, input?, expected}"
    )
    people_exhaustive: bool = Field(
        default=True,
        description="Ground truth lists every decision maker (unmatched people count as false positives)",
    )
    company_input: Literal["domain", "name"] = Field(
        default="domain", description="What the engine is given: the domain (identity given) or only the name"
    )
    columns: dict[str, ds.ColumnPrompt] | None = Field(
        default=None,
        description="Enrichment prompts for columns missing in the workspace: {key: {instruction, data_type}}",
    )


class DatasetPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind = "leads"
    csv: str | None = Field(default=None, max_length=ds.MAX_CSV_BYTES)
    items: list[dict[str, Any]] | None = Field(default=None, max_length=ds.MAX_ITEMS)
    people_exhaustive: bool = True
    company_input: Literal["domain", "name"] = "domain"


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["registry", "live", "suite"]
    dataset_id: uuid.UUID | None = None
    suite: str | None = Field(default=None, max_length=64)
    strategy: str | None = Field(default=None, max_length=120, description="Label shown in comparisons")
    config: dict[str, Any] = Field(default_factory=dict)


def _admin(ctx: Ctx) -> None:
    ctx.require(MemberRole.admin)


@router.get("/benchmark/suites")
async def suites(ctx: Ctx) -> list[dict[str, Any]]:
    _admin(ctx)
    from scout.benchmark.registry import list_suites, load_suites

    load_suites()
    return [s.public() for s in list_suites()]


@router.get("/benchmark/metrics")
async def metric_definitions(ctx: Ctx) -> list[dict[str, Any]]:
    """Every metric with its definition and unit (rates carry n and a Wilson 90 % interval)."""
    _admin(ctx)
    return [
        {"key": k, "label": label, "group": group, "unit": unit, "definition": definition}
        for k, (label, group, unit, definition) in METRIC_DEFS.items()
    ]


@router.get("/benchmark/template.csv")
async def template_csv(ctx: Ctx) -> Response:
    _admin(ctx)
    return Response(
        ds.TEMPLATE_CSV,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="benchmark-template.csv"'},
    )


@router.get("/benchmark/columns")
async def csv_columns(ctx: Ctx) -> dict[str, Any]:
    """Documented CSV column mapping and import limits."""
    _admin(ctx)
    return {
        "columns": ds.CSV_COLUMNS,
        "limits": {
            "csv_bytes": ds.MAX_CSV_BYTES,
            "rows": ds.MAX_ROWS,
            "items": ds.MAX_ITEMS,
            "people_per_item": ds.MAX_PEOPLE_PER_ITEM,
            "enrichment_columns": ds.MAX_ENRICH_KEYS,
        },
    }


@router.post("/benchmark/datasets/preview")
async def preview_dataset(body: DatasetPreview, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    parsed = svc.parse_import(
        kind=body.kind,
        csv=body.csv,
        items=body.items,
        people_exhaustive=body.people_exhaustive,
        company_input=body.company_input,
    )
    return svc.preview(parsed)


@router.post("/benchmark/datasets", status_code=201)
async def import_dataset(body: DatasetImport, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    parsed = svc.parse_import(
        kind=body.kind,
        csv=body.csv,
        items=body.items,
        people_exhaustive=body.people_exhaustive,
        company_input=body.company_input,
    )
    columns = {
        k.strip()[:80]: v.model_dump(exclude_none=True) for k, v in (body.columns or {}).items() if k.strip()
    }
    if len(columns) > ds.MAX_ENRICH_KEYS:
        raise ValidationFailed(f"At most {ds.MAX_ENRICH_KEYS} column prompts")
    async with session_scope() as s:
        d = await svc.create_dataset(
            s,
            ctx.workspace_id,
            name=body.name,
            kind=body.kind,
            description=body.description,
            parsed=parsed,
            people_exhaustive=body.people_exhaustive,
            company_input=body.company_input,
            columns=columns,
            source="csv" if body.csv else "json",
            user_id=ctx.user_id,
        )
        await s.flush()
        await s.refresh(d)
        return svc.dataset_out(d)


@router.get("/benchmark/datasets")
async def list_datasets(ctx: Ctx) -> list[dict[str, Any]]:
    _admin(ctx)
    async with session_scope() as s:
        return await svc.list_datasets(s, ctx.workspace_id)


@router.get("/benchmark/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: uuid.UUID, ctx: Ctx, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        return await svc.get_dataset(s, ctx.workspace_id, dataset_id, offset=offset, limit=limit)


@router.delete("/benchmark/datasets/{dataset_id}")
async def delete_dataset(dataset_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        await svc.delete_dataset(s, ctx.workspace_id, dataset_id)
    return {"deleted": True}


@router.post("/benchmark/runs", status_code=202)
async def start_run(body: RunCreate, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        run = await svc.start_run(
            s,
            ctx.workspace_id,
            mode=body.mode,
            dataset_id=body.dataset_id,
            suite=body.suite,
            strategy=body.strategy,
            config=body.config,
            user_id=ctx.user_id,
        )
        await s.flush()
        await s.refresh(run)
        return svc.run_out(run, full=True)


@router.get("/benchmark/runs")
async def list_runs(
    ctx: Ctx, dataset_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200)
) -> list[dict[str, Any]]:
    _admin(ctx)
    async with session_scope() as s:
        return await svc.list_runs(s, ctx.workspace_id, dataset_id=dataset_id, limit=limit)


@router.get("/benchmark/compare")
async def compare_runs(
    ctx: Ctx, ids: str = Query(..., description="Comma-separated run ids (max 6)")
) -> dict[str, Any]:
    _admin(ctx)
    try:
        run_ids = [uuid.UUID(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError as exc:
        raise ValidationFailed("ids must be comma-separated run UUIDs") from exc
    async with session_scope() as s:
        return await svc.compare_runs(s, ctx.workspace_id, run_ids)


@router.get("/benchmark/runs/{run_id}")
async def get_run(run_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        return await svc.get_run(s, ctx.workspace_id, run_id)


@router.get("/benchmark/runs/{run_id}/results")
async def run_results(
    run_id: uuid.UUID,
    ctx: Ctx,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    errors_only: bool = False,
    strategy: str | None = None,
) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        return await svc.run_results(
            s,
            ctx.workspace_id,
            run_id,
            offset=offset,
            limit=limit,
            errors_only=errors_only,
            strategy=strategy,
        )


@router.post("/benchmark/runs/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        return await svc.cancel_run(s, ctx.workspace_id, run_id)


@router.delete("/benchmark/runs/{run_id}")
async def delete_run(run_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    _admin(ctx)
    async with session_scope() as s:
        await svc.delete_run(s, ctx.workspace_id, run_id)
    return {"deleted": True}
