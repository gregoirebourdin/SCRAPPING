"""Activity (audit log + undo), usage/cost, sources health, diagnostics, webhooks, live events (SSE)."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
import sqlalchemy as sa
from fastapi import APIRouter, Header, Request
from sse_starlette.sse import EventSourceResponse

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import MemberRole
from scout.db.models import AuditLog, JobEvent, Source, Webhook
from scout.errors import NotFound, ValidationFailed
from scout.schemas.api import WebhookCreate
from scout.services import analytics, undo

router = APIRouter(tags=["activity"])


@router.get("/activity")
async def activity(ctx: Ctx, before: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    async with session_scope() as s:
        q = sa.select(AuditLog).where(AuditLog.workspace_id == ctx.workspace_id)
        if before:
            q = q.where(AuditLog.id < before)
        rows = (await s.scalars(q.order_by(AuditLog.id.desc()).limit(min(limit, 500)))).all()
        return [
            {
                "id": r.id,
                "actor_type": r.actor_type.value,
                "actor_id": r.actor_id,
                "action": r.action,
                "summary": r.summary,
                "entity_type": r.entity_type,
                "entity_count": r.entity_count,
                "campaign_id": r.campaign_id,
                "can_undo": bool(r.undo_payload) and r.undone_at is None,
                "undone_at": r.undone_at,
                "created_at": r.created_at,
            }
            for r in rows
        ]


@router.post("/activity/{audit_id}/undo")
async def undo_action(audit_id: int, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        return await undo.undo(s, ctx.workspace_id, audit_id, user_id=ctx.user_id)


@router.get("/usage")
async def usage(ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        return await analytics.usage_summary(s, ctx.workspace_id)


@router.get("/email/metrics")
async def email_engine_metrics(ctx: Ctx, hours: int = 24) -> dict[str, Any]:
    """Email Intelligence Engine throughput, latency (P50/P95), fast/deep mix, cache hits, SMTP health."""
    from scout.services.email_metrics import email_metrics

    async with session_scope() as s:
        return await email_metrics(s, ctx.workspace_id, hours=max(1, min(hours, 24 * 30)))


@router.get("/diagnostics")
async def diagnostics(ctx: Ctx, hours: int = 24) -> dict[str, Any]:
    async with session_scope() as s:
        return await analytics.diagnostics(s, ctx.workspace_id, hours=max(1, min(hours, 24 * 30)))


@router.get("/sources")
async def sources(ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (await s.scalars(sa.select(Source).order_by(Source.kind, Source.quality_score.desc()))).all()
    out = []
    for r in rows:
        req = max(1, r.requests)
        out.append(
            {
                "key": r.key,
                "name": r.name,
                "kind": r.kind,
                "description": r.description,
                "quality": r.quality_score,
                "enabled": r.enabled,
                "requests": r.requests,
                "success_rate": round(r.successes / req, 3) if r.requests else None,
                "block_rate": round(r.blocks / req, 3) if r.requests else None,
                "avg_latency_ms": round(r.total_latency_ms / req) if r.requests else None,
                "results_per_query": round(r.results / req, 1) if r.requests else None,
                "duplicate_rate": round(r.duplicates / max(1, r.results), 3) if r.results else None,
                "qualification_rate": round(r.qualified / max(1, r.results), 3) if r.results else None,
                "unhealthy_until": r.unhealthy_until,
                "last_error": r.last_error,
                "last_success_at": r.last_success_at,
            }
        )
    return out


@router.get("/webhooks")
async def list_webhooks(ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (await s.scalars(sa.select(Webhook).where(Webhook.workspace_id == ctx.workspace_id))).all()
        return [
            {
                "id": w.id,
                "url": w.url,
                "events": w.events,
                "is_active": w.is_active,
                "last_status": w.last_status,
                "last_delivery_at": w.last_delivery_at,
            }
            for w in rows
        ]


@router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookCreate, ctx: Ctx) -> dict[str, Any]:
    ctx.require(MemberRole.admin)
    from scout.crawl.ssrf import SSRFBlocked, validate_url

    try:
        validate_url(body.url)
    except SSRFBlocked as exc:
        raise ValidationFailed(
            f"Webhook URL not allowed: {exc}", hint="Use a public http(s) URL on port 80/443/8080/8443"
        ) from exc
    secret = secrets.token_urlsafe(24)
    async with session_scope() as s:
        w = Webhook(workspace_id=ctx.workspace_id, url=body.url, secret=secret, events=body.events)
        s.add(w)
        await s.flush()
        return {"id": w.id, "url": w.url, "events": w.events, "secret": secret}


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    ctx.require(MemberRole.admin)
    async with session_scope() as s:
        w = await s.get(Webhook, webhook_id)
        if w is None or w.workspace_id != ctx.workspace_id:
            raise NotFound("Webhook not found")
        await s.delete(w)
    return {"deleted": True}


@router.get("/events/stream")
async def stream(
    request: Request, ctx: Ctx, last_event_id: str | None = Header(default=None)
) -> EventSourceResponse:
    """SSE of job_events for the workspace. Resumable with Last-Event-ID; 15 s heartbeats."""
    start_id = 0
    if last_event_id and last_event_id.isdigit():
        start_id = int(last_event_id)
    else:
        q = request.query_params.get("since")
        if q and q.isdigit():
            start_id = int(q)
        else:
            async with session_scope() as s:
                start_id = int(
                    await s.scalar(
                        sa.select(sa.func.coalesce(sa.func.max(JobEvent.id), 0)).where(
                            JobEvent.workspace_id == ctx.workspace_id
                        )
                    )
                    or 0
                )

    async def gen() -> AsyncIterator[dict[str, Any]]:
        cursor = start_id
        idle = 0.0
        yield {"event": "ready", "data": orjson.dumps({"cursor": cursor}).decode(), "id": str(cursor)}
        while True:
            if await request.is_disconnected():
                return
            async with session_scope() as s:
                rows = (
                    await s.execute(
                        sa.select(
                            JobEvent.id,
                            JobEvent.type,
                            JobEvent.payload,
                            JobEvent.campaign_id,
                            JobEvent.created_at,
                        )
                        .where(JobEvent.workspace_id == ctx.workspace_id, JobEvent.id > cursor)
                        .order_by(JobEvent.id)
                        .limit(500)
                    )
                ).all()
            for r in rows:
                cursor = r.id
                yield {
                    "event": r.type,
                    "id": str(r.id),
                    "data": orjson.dumps(
                        {
                            "type": r.type,
                            "payload": r.payload,
                            "campaign_id": r.campaign_id,
                            "at": r.created_at,
                        },
                        option=orjson.OPT_SERIALIZE_UUID,
                    ).decode(),
                }
            if rows:
                idle = 0.0
                continue
            await asyncio.sleep(1.0)
            idle += 1.0
            if idle >= 15:
                idle = 0.0
                yield {"event": "ping", "data": "{}"}

    return EventSourceResponse(
        gen(), ping=None, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    )
