"""Lead detail drawer, provenance, history, human edits, review queue, refresh actions, GDPR erasure."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import EntityType, ExposureType, MemberRole
from scout.db.models import Email, LeadExposure, Person
from scout.jobs import queue
from scout.schemas.api import FieldEdit, RefreshRequest, ReviewApprove
from scout.services import audit, leads, registry
from scout.services import lists as lists_svc
from scout.services.suppression import gdpr_erase_person

router = APIRouter(tags=["leads"])


async def _mark_shown(s: Any, ctx: Ctx, entity_type: EntityType, entity_id: uuid.UUID) -> None:
    """SHOWN exposure at most once per day per entity."""
    recent = await s.scalar(
        sa.select(LeadExposure.id).where(
            LeadExposure.workspace_id == ctx.workspace_id, LeadExposure.entity_type == entity_type,
            LeadExposure.entity_id == entity_id, LeadExposure.exposure_type == ExposureType.SHOWN,
            LeadExposure.occurred_at >= sa.func.now() - sa.text("interval '1 day'"),
        ).limit(1)
    )
    if recent is None:
        s.add(LeadExposure(workspace_id=ctx.workspace_id, entity_type=entity_type, entity_id=entity_id,
                           exposure_type=ExposureType.SHOWN))


@router.get("/people/{person_id}")
async def person(person_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        data = await leads.person_detail(s, ctx.workspace_id, person_id)
        data["lists"] = await leads.lead_lists(s, ctx.workspace_id, EntityType.person, person_id)
        data["campaigns"] = await leads.lead_campaigns(s, ctx.workspace_id, EntityType.person, person_id)
        await _mark_shown(s, ctx, EntityType.person, person_id)
        return data


@router.get("/companies/{company_id}")
async def company(company_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        data = await leads.company_detail(s, ctx.workspace_id, company_id)
        data["lists"] = await leads.lead_lists(s, ctx.workspace_id, EntityType.company, company_id)
        data["campaigns"] = await leads.lead_campaigns(s, ctx.workspace_id, EntityType.company, company_id)
        return data


@router.get("/people/{entity_id}/history")
async def person_history(entity_id: uuid.UUID, ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return await leads.lead_history(s, ctx.workspace_id, EntityType.person, entity_id)


@router.get("/companies/{entity_id}/history")
async def company_history(entity_id: uuid.UUID, ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return await leads.lead_history(s, ctx.workspace_id, EntityType.company, entity_id)


@router.patch("/people/{person_id}")
async def edit_person(person_id: uuid.UUID, body: FieldEdit, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        old = await leads.edit_field(s, ctx.workspace_id, EntityType.person, person_id, body.field, body.value, user_id=ctx.user_id)
        entry = await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="person.edit", entity_type="person",
                                entity_ids=[person_id], summary=f"Edited {body.field}",
                                undo={"op": "restore_field", "entity_type": "person", "entity_id": str(person_id),
                                      "field": body.field, "previous": old} if old is not None and body.field != "email" else None)
        return {"ok": True, "audit_id": entry.id}


@router.patch("/companies/{company_id}")
async def edit_company(company_id: uuid.UUID, body: FieldEdit, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        old = await leads.edit_field(s, ctx.workspace_id, EntityType.company, company_id, body.field, body.value, user_id=ctx.user_id)
        entry = await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="company.edit", entity_type="company",
                                entity_ids=[company_id], summary=f"Edited {body.field}",
                                undo={"op": "restore_field", "entity_type": "company", "entity_id": str(company_id),
                                      "field": body.field, "previous": old} if old is not None else None)
        return {"ok": True, "audit_id": entry.id}


@router.post("/review/approve")
async def approve(body: ReviewApprove, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        n = await leads.approve_review(s, ctx.workspace_id, body.entity_type, body.ids, user_id=ctx.user_id)
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="review.approve",
                        entity_type=body.entity_type.value, entity_ids=body.ids, summary=f"Approved {n} records")
    return {"approved": n}


@router.post("/refresh")
async def refresh(body: RefreshRequest, ctx: Ctx) -> dict[str, Any]:
    """Row-level refresh (spec §138): company (recrawl), person (re-resolve), email (re-verify), column."""
    async with session_scope() as s:
        rows = await lists_svc.resolve_rows(s, ctx.workspace_id, body.rows)
        ids = rows.ids
        if body.what == "email":
            person_ids = ids if rows.entity_type == EntityType.person else []
            email_ids = [e for e in (await s.scalars(sa.select(Email.id).where(Email.person_id.in_(person_ids)))).all()]
            for i in range(0, len(email_ids), 50):
                await queue.enqueue(s, workspace_id=ctx.workspace_id, type="email.verify", priority=8,
                                    payload={"email_ids": [str(x) for x in email_ids[i:i + 50]]})
            queued = len(email_ids)
        elif body.what == "column" and body.column_id:
            from scout.enrich.engine import enqueue_column  # type: ignore[import-not-found]

            queued = await enqueue_column(ctx.workspace_id, body.column_id, entity_ids=ids, only_missing=False, force=True)
        elif body.what == "person":
            for i in range(0, len(ids), 50):
                await queue.enqueue(s, workspace_id=ctx.workspace_id, type="email.find", priority=8,
                                    payload={"person_ids": [str(x) for x in ids[i:i + 50]]})
            queued = len(ids)
        else:
            company_ids = ids
            if rows.entity_type == EntityType.person:
                company_ids = list({c for c in (await s.scalars(sa.select(Person.company_id).where(Person.id.in_(ids)))).all() if c})
            for cid in company_ids:
                await queue.enqueue(s, workspace_id=ctx.workspace_id, type="company.refresh", priority=8,
                                    payload={"company_id": str(cid)}, dedupe_key=f"refresh:{cid}")
            queued = len(company_ids)
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action=f"refresh.{body.what}",
                        entity_type=rows.entity_type.value, entity_ids=ids, summary=f"Refresh {body.what} for {len(ids)} rows")
    return {"queued": queued}


@router.post("/people/{person_id}/erase")
async def erase(person_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    ctx.require(MemberRole.admin)
    async with session_scope() as s:
        await gdpr_erase_person(s, ctx.workspace_id, person_id, user_id=ctx.user_id)
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="person.gdpr_erase",
                        entity_type="person", entity_ids=[person_id], summary="GDPR erasure (suppression key kept)")
    return {"erased": True}


@router.post("/people/{person_id}/contacted")
async def mark_contacted(person_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        await registry.record_exposures(s, ctx.workspace_id, ExposureType.CONTACTED, person_ids=[person_id])
    return {"ok": True}
