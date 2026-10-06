"""Audit log (spec §113) and undo support (spec §114)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import ActorType
from scout.db.models import AuditLog


async def log(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: str | None,
    action: str,
    actor_type: ActorType = ActorType.user,
    entity_type: str | None = None,
    entity_ids: Iterable[Any] = (),
    campaign_id: uuid.UUID | None = None,
    assistant_action_id: uuid.UUID | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
    undo: dict[str, Any] | None = None,
) -> AuditLog:
    ids = [str(i) for i in entity_ids]
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_ids=ids[:500],  # keep rows small; the count stays exact
        entity_count=len(ids),
        campaign_id=campaign_id,
        assistant_action_id=assistant_action_id,
        summary=summary,
        payload=payload or {},
        undo_payload=undo,
    )
    s.add(entry)
    await s.flush()
    return entry


async def mark_undone(s: AsyncSession, audit_id: int) -> None:
    await s.execute(sa.update(AuditLog).where(AuditLog.id == audit_id).values(undone_at=sa.func.now()))
