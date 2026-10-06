"""Job handlers for the enrichment engine. The worker must import this module to register them."""

from __future__ import annotations

import uuid
from typing import Any

from scout.enrich.engine import enqueue_column, run_batch
from scout.jobs.registry import JobContext, job_handler


def _uuids(values: Any) -> list[uuid.UUID]:
    return [uuid.UUID(str(v)) for v in (values or [])]


@job_handler("enrichment.batch", timeout_s=900)
async def enrichment_batch(ctx: JobContext) -> dict[str, Any]:
    """Compute ≤ 50 cells of one column. Payload: column_id, entity_ids, force?, refresh?, max_page_age_days?"""
    p = ctx.payload
    checkpoint = ctx.checkpoint if ctx.campaign_id else None
    return await run_batch(
        ctx.workspace_id,
        uuid.UUID(str(p["column_id"])),
        _uuids(p.get("entity_ids")),
        force=bool(p.get("force")),
        refresh=bool(p.get("refresh")),
        max_page_age_days=p.get("max_page_age_days"),
        campaign_id=ctx.campaign_id,
        job_id=ctx.job_id,
        checkpoint=checkpoint,
    )


@job_handler("enrichment.column", timeout_s=300)
async def enrichment_column(ctx: JobContext) -> dict[str, Any]:
    """Fan out a whole column (large lists) into batch jobs. Payload: column_id, list_id?, entity_ids?,
    only_missing?, force?"""
    p = ctx.payload
    queued = await enqueue_column(
        ctx.workspace_id,
        uuid.UUID(str(p["column_id"])),
        entity_ids=_uuids(p["entity_ids"]) if p.get("entity_ids") is not None else None,
        list_id=uuid.UUID(str(p["list_id"])) if p.get("list_id") else None,
        only_missing=bool(p.get("only_missing", True)),
        force=bool(p.get("force", False)),
        campaign_id=ctx.campaign_id,
    )
    return {"queued": queued}
