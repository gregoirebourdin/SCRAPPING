"""Durable event log (job_events) + NOTIFY. Feeds the SSE stream (spec §78)."""

from __future__ import annotations

import uuid
from typing import Any

import orjson
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.models import JobEvent

NOTIFY_CHANNEL = "scout_events"


async def emit(
    workspace_id: uuid.UUID,
    type_: str,
    payload: dict[str, Any] | None = None,
    *,
    campaign_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Append an event. When `session` is given the event commits atomically with the caller's work."""
    payload = payload or {}

    async def _do(s: AsyncSession) -> None:
        s.add(
            JobEvent(
                workspace_id=workspace_id, type=type_, payload=payload, campaign_id=campaign_id, job_id=job_id
            )
        )
        await s.flush()
        await s.execute(
            sa.text("SELECT pg_notify(:ch, :msg)"),
            {"ch": NOTIFY_CHANNEL, "msg": orjson.dumps({"ws": str(workspace_id)}).decode()},
        )

    if session is not None:
        await _do(session)
    else:
        async with session_scope() as s:
            await _do(s)


# ---------------------------------------------------------------------------------------------
# Live-run helpers (additive): per-candidate stage events feed the skeleton rows and the "what is happening
# now" sentence of the run header / chat run card. Payloads stay tiny; they ride in the caller's transaction.
# ---------------------------------------------------------------------------------------------

CANDIDATE_STAGE = "candidate.stage"
CANDIDATE_DONE = "candidate.done"
CAMPAIGN_AMENDED = "campaign.amended"


async def emit_candidate_stage(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    event_id: uuid.UUID,
    stage: str,
    *,
    name: str | None = None,
    domain: str | None = None,
) -> None:
    await emit(
        workspace_id,
        CANDIDATE_STAGE,
        {
            "campaign_id": str(campaign_id),
            "event_id": str(event_id),
            "stage": stage,
            "name": (name or "")[:120] or None,
            "domain": domain,
        },
        campaign_id=campaign_id,
        session=session,
    )


async def emit_candidate_done(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    event_id: uuid.UUID,
    outcome: str,
    *,
    reason: str | None = None,
    stage: str | None = None,
) -> None:
    await emit(
        workspace_id,
        CANDIDATE_DONE,
        {
            "campaign_id": str(campaign_id),
            "event_id": str(event_id),
            "outcome": outcome,
            "reason": (reason or "")[:200] or None,
            "stage": stage,
        },
        campaign_id=campaign_id,
        session=session,
    )
