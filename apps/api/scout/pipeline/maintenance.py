"""Maintenance jobs: company refresh, reservation expiry, stale enrichment cells, webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import orjson
import sqlalchemy as sa
import structlog

from scout.db.engine import session_scope
from scout.db.enums import ReservationStatus
from scout.db.models import CampaignReservation, Company, Webhook
from scout.jobs.registry import JobContext, job_handler

log = structlog.get_logger("maintenance")


@job_handler("company.refresh", timeout_s=300)
async def refresh_company(ctx: JobContext) -> dict[str, Any]:
    """Row-level 'Refresh company': force a recrawl and re-extract company facts (with provenance)."""
    from scout.crawl.cache import ensure_crawled
    from scout.extract.company_info import extract_company_facts
    from scout.pipeline.processor import record_company_facts

    company_id = uuid.UUID(ctx.payload["company_id"])
    pages = await ensure_crawled(ctx.workspace_id, company_id, force=True)
    facts = extract_company_facts(pages) if pages else []
    async with session_scope() as s:
        comp = await s.get(Company, company_id)
        if comp is not None and comp.workspace_id == ctx.workspace_id and facts:
            await record_company_facts(s, ctx.workspace_id, comp, facts, pages)
            comp.last_enriched_at = datetime.now(UTC)
    await ctx.emit("company.refreshed", {"company_id": str(company_id), "pages": len(pages)})
    return {"pages": len(pages), "facts": len(facts)}


@job_handler("system.maintenance", timeout_s=120)
async def maintenance(ctx: JobContext) -> dict[str, Any]:
    async with session_scope() as s:
        res = await s.execute(
            sa.update(CampaignReservation)
            .where(
                CampaignReservation.status == ReservationStatus.reserved,
                CampaignReservation.expires_at < sa.func.now(),
            )
            .values(status=ReservationStatus.expired)
            .returning(CampaignReservation.id)
        )
        expired = len(res.scalars().all())
    stale = 0
    try:
        from scout.enrich.engine import mark_stale_cells

        stale = await mark_stale_cells(ctx.workspace_id)
    except Exception as exc:  # optional
        log.info("maintenance.stale_skip", error=str(exc))
    ctx.later(600.0)
    return {"expired_reservations": expired, "stale_cells": stale}


@job_handler("webhook.deliver", timeout_s=60)
async def deliver_webhook(ctx: JobContext) -> dict[str, Any]:
    """CRM-ready outbound events (spec §161), HMAC-signed.

    Webhook URLs are user supplied: they are re-validated before every delivery and sent through the
    SSRF-safe transport (resolved IPs checked at connect time — DNS rebinding cannot reach internal hosts).
    Redirects are never followed. A blocked or failing hook never prevents delivery to the others.
    """
    from scout.crawl.ssrf import SafeAsyncTransport, SSRFBlocked, validate_url

    async with session_scope() as s:
        hooks = (
            await s.scalars(
                sa.select(Webhook).where(
                    Webhook.workspace_id == ctx.workspace_id, Webhook.is_active.is_(True)
                )
            )
        ).all()
        targets = [(h.id, h.url, h.secret) for h in hooks if ctx.payload.get("event") in (h.events or [])]
    sent = 0
    body = orjson.dumps(
        {"event": ctx.payload.get("event"), "data": ctx.payload.get("data"), "sent_at": int(time.time())}
    )
    async with httpx.AsyncClient(
        timeout=10, transport=SafeAsyncTransport(), follow_redirects=False
    ) as client:
        for hid, url, secret in targets:
            sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            try:
                validate_url(url)
                r = await client.post(
                    url,
                    content=body,
                    headers={"Content-Type": "application/json", "X-Scout-Signature": f"sha256={sig}"},
                )
                status = r.status_code
                sent += 1
            except SSRFBlocked as exc:
                log.warning("webhook.blocked", webhook_id=str(hid), error=str(exc))
                status = 0
            except httpx.HTTPError:
                status = 0
            async with session_scope() as s:
                await s.execute(
                    sa.update(Webhook)
                    .where(Webhook.id == hid)
                    .values(last_status=status, last_delivery_at=sa.func.now())
                )
    return {"sent": sent}
