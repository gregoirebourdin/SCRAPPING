"""Campaigns: parse, create/start, lifecycle, live status, rejections, templates."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import EntityType
from scout.db.models import CampaignTemplate, List
from scout.errors import ValidationFailed
from scout.pipeline import campaigns as svc
from scout.pipeline.icp import ParseContext, parse_prompt
from scout.schemas.api import CampaignCreate, ParseOut, ParseRequest, TemplateCreate, TemplateRun
from scout.schemas.campaign import CampaignAmendment, interpret
from scout.services import audit

router = APIRouter(tags=["campaigns"])


async def build_parse_context(
    ctx: Any, list_id: uuid.UUID | None, selected_ids: list[uuid.UUID]
) -> ParseContext:
    async with session_scope() as s:
        rows = (
            await s.execute(
                sa.select(List.id, List.name, List.entity_type).where(
                    List.workspace_id == ctx.workspace_id, List.is_archived.is_(False)
                )
            )
        ).all()
        pc = ParseContext(
            lists={n.lower(): i for i, n, _ in rows},
            latest_import_id=await svc.latest_import_id(s, ctx.workspace_id),
        )
        if list_id:
            match = next(((n, et) for i, n, et in rows if i == list_id), None)
            pc.current_list_id = list_id
            pc.current_list_name = match[0] if match else None
            if selected_ids:
                if match and match[1] == EntityType.company:
                    pc.selected_company_ids = selected_ids
                else:
                    pc.selected_person_ids = selected_ids
        elif selected_ids:
            pc.selected_person_ids = selected_ids
    return pc


@router.post("/campaigns/parse", response_model=ParseOut)
async def parse(body: ParseRequest, ctx: Ctx) -> ParseOut:
    pc = await build_parse_context(ctx, body.list_id, body.selected_ids)
    defn, parser = await parse_prompt(body.prompt, pc)
    names = {v: k for k, v in pc.lists.items()}
    return ParseOut(
        definition=defn, interpretation=[i.model_dump() for i in interpret(defn, names)], parser=parser
    )


@router.post("/campaigns", status_code=201)
async def create(body: CampaignCreate, ctx: Ctx) -> dict[str, Any]:
    if body.definition is None and not body.prompt:
        raise ValidationFailed("Provide a prompt or a definition")
    defn = body.definition
    if defn is None:
        pc = await build_parse_context(ctx, body.list_id, body.selected_ids)
        defn, _ = await parse_prompt(body.prompt or "", pc)
    async with session_scope() as s:
        c = await svc.create_campaign(
            s,
            ctx.workspace_id,
            defn,
            user_id=ctx.user_id,
            prompt=body.prompt,
            target_list_id=body.target_list_id,
            start=body.start,
        )
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="campaign.create",
            entity_type="campaign",
            entity_ids=[c.id],
            campaign_id=c.id,
            summary=f'Started campaign "{c.name}"',
        )
        cid = c.id
    async with session_scope() as s:
        return await svc.campaign_status(s, ctx.workspace_id, cid)


@router.get("/campaigns")
async def list_campaigns(ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return await svc.list_campaigns(s, ctx.workspace_id)


@router.get("/campaigns/{campaign_id}")
async def status(campaign_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        return await svc.campaign_status(s, ctx.workspace_id, campaign_id)


@router.get("/campaigns/{campaign_id}/rejections")
async def rejections(campaign_id: uuid.UUID, ctx: Ctx, limit: int = 200) -> list[dict[str, Any]]:
    async with session_scope() as s:
        return await svc.rejected_candidates(s, ctx.workspace_id, campaign_id, limit=min(limit, 1000))


class ClarifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=3, max_length=4000)


@router.post("/campaigns/clarify")
async def clarify(body: ClarifyIn, ctx: Ctx) -> dict[str, Any]:
    """≤ 3 targeted questions whose answers change the search (empty when the request is precise)."""
    from scout.chat.clarify import build_questions

    questions, lang, missing = build_questions(body.prompt)
    return {"questions": [q.model_dump() for q in questions], "lang": lang, "missing": missing}


class AmendIn(CampaignAmendment):
    dry_run: bool = False
    resume: bool = True
    base_hash: str | None = Field(default=None, max_length=64)


async def _amend(campaign_id: uuid.UUID, body: AmendIn, ctx: Any) -> dict[str, Any]:
    am = CampaignAmendment.model_validate(body.model_dump(exclude={"dry_run", "resume", "base_hash"}))
    async with session_scope() as s:
        out = await svc.amend_campaign(
            s,
            ctx.workspace_id,
            campaign_id,
            am,
            user_id=ctx.user_id,
            dry_run=body.dry_run,
            resume=body.resume,
            base_hash=body.base_hash,
        )
    if body.dry_run:
        return out
    async with session_scope() as s:
        return {**out, "campaign": await svc.campaign_status(s, ctx.workspace_id, campaign_id)}


@router.patch("/campaigns/{campaign_id}")
async def amend(campaign_id: uuid.UUID, body: AmendIn, ctx: Ctx) -> dict[str, Any]:
    """Resume with changes: preview (`dry_run`) or apply an amendment; see `pipeline.campaigns.amend_campaign`."""
    return await _amend(campaign_id, body, ctx)


@router.post("/campaigns/{campaign_id}/amend")
async def amend_post(campaign_id: uuid.UUID, body: AmendIn, ctx: Ctx) -> dict[str, Any]:
    return await _amend(campaign_id, body, ctx)


@router.get("/campaigns/{campaign_id}/live")
async def live(campaign_id: uuid.UUID, ctx: Ctx) -> dict[str, Any]:
    """Status + in-flight candidates + recent events + stall diagnostics (restores a live run after a refresh)."""
    async with session_scope() as s:
        return await svc.live_snapshot(s, ctx.workspace_id, campaign_id)


@router.post("/campaigns/{campaign_id}/{action}")
async def lifecycle(campaign_id: uuid.UUID, action: str, ctx: Ctx) -> dict[str, Any]:
    if action == "kick":
        async with session_scope() as s:
            kicked = await svc.kick_campaign(s, ctx.workspace_id, campaign_id)
            await audit.log(
                s,
                workspace_id=ctx.workspace_id,
                actor_id=ctx.user_id,
                action="campaign.kick",
                entity_type="campaign",
                entity_ids=[campaign_id],
                campaign_id=campaign_id,
                summary="Retried a stalled campaign",
                payload=kicked,
            )
        async with session_scope() as s:
            return {**await svc.campaign_status(s, ctx.workspace_id, campaign_id), "kick": kicked}
    fn = {
        "pause": svc.pause_campaign,
        "resume": svc.resume_campaign,
        "retry": svc.resume_campaign,
        "cancel": svc.cancel_campaign,
    }.get(action)
    if fn is None:
        raise ValidationFailed("Unknown action (pause, resume, retry, kick, cancel)")
    async with session_scope() as s:
        c = await fn(s, ctx.workspace_id, campaign_id)
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action=f"campaign.{action}",
            entity_type="campaign",
            entity_ids=[campaign_id],
            campaign_id=campaign_id,
            summary=f'{action.title()} campaign "{c.name}"',
        )
    async with session_scope() as s:
        return await svc.campaign_status(s, ctx.workspace_id, campaign_id)


@router.get("/campaign-templates")
async def templates(ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (
            await s.scalars(
                sa.select(CampaignTemplate)
                .where(CampaignTemplate.workspace_id == ctx.workspace_id)
                .order_by(CampaignTemplate.updated_at.desc())
            )
        ).all()
        return [
            {
                "id": t.id,
                "name": t.name,
                "prompt": t.prompt,
                "definition": t.definition,
                "last_run_at": t.last_run_at,
                "created_at": t.created_at,
            }
            for t in rows
        ]


@router.post("/campaign-templates", status_code=201)
async def save_template(body: TemplateCreate, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        t = await svc.save_template(
            s,
            ctx.workspace_id,
            name=body.name,
            campaign_id=body.campaign_id,
            definition=body.definition,
            user_id=ctx.user_id,
        )
        await audit.log(
            s,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            action="template.create",
            summary=f'Saved search "{t.name}"',
        )
        return {"id": t.id, "name": t.name}


@router.post("/campaign-templates/{template_id}/run")
async def run_template(template_id: uuid.UUID, body: TemplateRun, ctx: Ctx) -> dict[str, Any]:
    async with session_scope() as s:
        c = await svc.run_template(
            s,
            ctx.workspace_id,
            template_id,
            user_id=ctx.user_id,
            only_new=body.only_new,
            target_count=body.target_count,
        )
        cid = c.id
    async with session_scope() as s:
        return await svc.campaign_status(s, ctx.workspace_id, cid)
