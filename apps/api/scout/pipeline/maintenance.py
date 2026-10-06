"""Maintenance jobs: company refresh, reservation expiry, stale enrichment cells, webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any

import httpx
import orjson
import sqlalchemy as sa
import structlog

from scout.db.engine import session_scope
from scout.db.models import Company, Webhook
from scout.jobs.registry import JobContext, job_handler

log = structlog.get_logger("maintenance")


@job_handler("company.refresh", timeout_s=300)
async def refresh_company(ctx: JobContext) -> dict[str, Any]:
    """Row-level 'Refresh company': force a recrawl and re-extract company facts."""
    from scout.crawl.cache import ensure_crawled  # type: ignore[import-not-found]

    company_id = uuid.UUID(ctx.payload["company_id"])
    pages = await ensure_crawled(ctx.workspace_id, company_id, force=True)
    async with session_scope() as s:
        comp = await s.get(Company, company_id)
        if comp is not None:
            comp.last_enriched_at = None  # next pipeline pass re-extracts facts from the fresh crawl
    if pages:
        from scout.extract.company_info import extract_company_facts  # type: ignore[import-not-found]
        from scout.db.enums import SourceType
        from scout.services import registry

        facts = extract_company_facts(pages)
        async with session_scope() as s:
            comp = await s.get(Company, company_id)
            assert comp is not None
            for f in facts:
                name = "registry_id" if f.field_name in ("siren", "registry_id") else f.field_name
                if name in registry.COMPANY_FIELDS:
                    await registry.observe_company(
                        s, ctx.workspace_id, comp, {name: f.value},
                        registry.Evidence(source_type=SourceType.website, confidence=f.confidence, source_key="website",
                                          source_url=f.source_url, evidence=f.evidence),
                    )
            comp.last_enriched_at = sa.func.now()
    await ctx.emit("company.refreshed", {"company_id": str(company_id), "pages": len(pages)})
    return {"pages": len(pages)}


@job_handler("system.maintenance", timeout_s=120)
async def maintenance(ctx: JobContext) -> dict[str, Any]:
    async with session_scope() as s:
        res = await s.execute(sa.text(
            "UPDATE campaign_reservations SET status = 'expired' WHERE status = 'reserved' AND expires_at < now()"))
        expired = res.rowcount or 0
    stale = 0
    try:
        from scout.enrich.engine import mark_stale_cells  # type: ignore[import-not-found]

        stale = await mark_stale_cells(ctx.workspace_id)
    except Exception as exc:  # optional
        log.info("maintenance.stale_skip", error=str(exc))
    ctx.later(600.0)
    return {"expired_reservations": expired, "stale_cells": stale}


@job_handler("webhook.deliver", timeout_s=60)
async def deliver_webhook(ctx: JobContext) -> dict[str, Any]:
    """CRM-ready outbound events (spec §161), HMAC-signed."""
    from scout.crawl.ssrf import validate_url  # type: ignore[import-not-found]

    async with session_scope() as s:
        hooks = (await s.scalars(sa.select(Webhook).where(Webhook.workspace_id == ctx.workspace_id, Webhook.is_active.is_(True)))).all()
        targets = [(h.id, h.url, h.secret) for h in hooks if ctx.payload.get("event") in (h.events or [])]
    sent = 0
    body = orjson.dumps({"event": ctx.payload.get("event"), "data": ctx.payload.get("data"), "sent_at": int(time.time())})
    async with httpx.AsyncClient(timeout=10) as client:
        for hid, url, secret in targets:
            validate_url(url)
            sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            try:
                r = await client.post(url, content=body, headers={"Content-Type": "application/json", "X-Scout-Signature": f"sha256={sig}"})
                status = r.status_code
                sent += 1
            except httpx.HTTPError:
                status = 0
            async with session_scope() as s:
                await s.execute(sa.update(Webhook).where(Webhook.id == hid).values(last_status=status, last_delivery_at=sa.func.now()))
    return {"sent": sent}
