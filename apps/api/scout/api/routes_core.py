"""Core routes: health, me/workspace, metadata (fields, enums, tools), global search."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter

from scout.ai.models import ModelRole, model_for
from scout.api.deps import Ctx, Who
from scout.auth.context import ensure_user_and_workspace
from scout.config import get_settings
from scout.db import enums as E
from scout.db.engine import session_scope
from scout.db.enums import EntityType, MemberRole
from scout.db.models import Campaign, Company, Email, List, Person, Workspace
from scout.query.rows import field_registry, load_columns
from scout.schemas.api import FieldMeta, MeOut, WorkspaceOut, WorkspaceUpdate
from scout.services import audit

router = APIRouter()


@router.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    async with session_scope() as s:
        await s.execute(sa.text("SELECT 1"))
    return {"ok": True}


@router.get("/me", response_model=MeOut, tags=["workspace"])
async def me(principal: Who, ctx: Ctx) -> MeOut:
    memberships = await ensure_user_and_workspace(principal)
    s = get_settings()
    return MeOut(
        user_id=principal.user_id,
        email=principal.email,
        name=principal.name,
        workspaces=[
            WorkspaceOut(id=w.id, name=w.name, slug=w.slug, role=r.value, monthly_budget_usd=float(w.monthly_budget_usd),
                         hard_budget_cap=w.hard_budget_cap, settings=w.settings or {})
            for w, r in memberships
        ],
        current_workspace_id=ctx.workspace_id,
        ai_provider=s.resolved_ai_provider,
        ai_models={r.value: model_for(r) for r in ModelRole},
        features={
            "maps": bool(s.gmaps_scraper_url),
            "smtp_verification": s.smtp_enabled or bool(s.verifier_service_url),
            "verifier_service": bool(s.verifier_service_url),
            "grounded_search": s.resolved_ai_provider == "gemini",
            "browser_rendering": s.crawler_enable_browser or s.crawler_enable_crawl4ai,
        },
    )


@router.patch("/workspace", response_model=WorkspaceOut, tags=["workspace"])
async def update_workspace(body: WorkspaceUpdate, ctx: Ctx) -> WorkspaceOut:
    ctx.require(MemberRole.admin)
    async with session_scope() as s:
        ws = await s.get(Workspace, ctx.workspace_id)
        assert ws is not None
        changes: dict[str, Any] = {}
        if body.name:
            ws.name = changes["name"] = body.name.strip()
        if body.monthly_budget_usd is not None:
            ws.monthly_budget_usd = Decimal(str(body.monthly_budget_usd))
            changes["monthly_budget_usd"] = body.monthly_budget_usd
        if body.hard_budget_cap is not None:
            ws.hard_budget_cap = changes["hard_budget_cap"] = body.hard_budget_cap
        if body.settings is not None:
            ws.settings = {**(ws.settings or {}), **body.settings}
            changes["settings"] = body.settings
        await audit.log(s, workspace_id=ctx.workspace_id, actor_id=ctx.user_id, action="workspace.update",
                        summary="Updated workspace settings", payload=changes)
        return WorkspaceOut(id=ws.id, name=ws.name, slug=ws.slug, role=ctx.role.value,
                            monthly_budget_usd=float(ws.monthly_budget_usd), hard_budget_cap=ws.hard_budget_cap,
                            settings=ws.settings or {})


@router.get("/meta/fields", response_model=list[FieldMeta], tags=["meta"])
async def fields(ctx: Ctx, entity_type: EntityType = EntityType.person, list_id: uuid.UUID | None = None) -> list[FieldMeta]:
    async with session_scope() as s:
        cols = await load_columns(s, ctx.workspace_id, list_id)
    reg = field_registry(entity_type, cols)
    return [
        FieldMeta(key=f.key, label=f.label, type=f.type, enum_values=list(f.enum_values), sortable=f.sortable,
                  filterable=f.filterable, custom_column_id=f.custom_column_id)
        for f in reg.values()
    ]


@router.get("/meta/enums", tags=["meta"])
async def enums() -> dict[str, list[str]]:
    return {
        name: [m.value for m in cls]
        for name, cls in {
            "EmailStatus": E.EmailStatus, "ExposureType": E.ExposureType, "ExclusionMode": E.ExclusionMode,
            "CampaignStatus": E.CampaignStatus, "CellStatus": E.CellStatus, "ResolverType": E.ResolverType,
            "ColumnDataType": E.ColumnDataType, "SuppressionReason": E.SuppressionReason, "JobStatus": E.JobStatus,
            "RoleFamily": E.RoleFamily, "Seniority": E.Seniority, "SignalType": E.SignalType,
        }.items()
    }


@router.get("/search", tags=["search"])
async def global_search(ctx: Ctx, q: str, limit: int = 8) -> dict[str, Any]:
    """Global search across companies, domains, people, emails, lists and campaigns (pg_trgm / ILIKE)."""
    needle = q.strip().lower()
    if len(needle) < 2:
        return {"companies": [], "people": [], "lists": [], "campaigns": []}
    like = f"%{needle}%"
    limit = max(1, min(limit, 25))
    async with session_scope() as s:
        companies = (await s.execute(
            sa.select(Company.id, Company.name, Company.normalized_domain, Company.city)
            .where(Company.workspace_id == ctx.workspace_id,
                   sa.or_(Company.normalized_name.ilike(like), Company.normalized_domain.ilike(like), Company.name.ilike(like)))
            .order_by(sa.func.similarity(Company.normalized_name, needle).desc()).limit(limit)
        )).all()
        people = (await s.execute(
            sa.select(Person.id, Person.full_name, Person.job_title, Company.name, Email.address)
            .outerjoin(Company, Company.id == Person.company_id)
            .outerjoin(Email, Email.id == Person.primary_email_id)
            .where(Person.workspace_id == ctx.workspace_id,
                   sa.or_(Person.normalized_name.ilike(like), Email.address.ilike(like), Person.full_name.ilike(like)))
            .order_by(sa.func.similarity(Person.normalized_name, needle).desc()).limit(limit)
        )).all()
        lists = (await s.execute(sa.select(List.id, List.name).where(List.workspace_id == ctx.workspace_id, List.name.ilike(like)).limit(limit))).all()
        camps = (await s.execute(sa.select(Campaign.id, Campaign.name, Campaign.status).where(
            Campaign.workspace_id == ctx.workspace_id, sa.or_(Campaign.name.ilike(like), Campaign.prompt.ilike(like))).limit(limit))).all()
    return {
        "companies": [{"id": i, "name": n, "domain": d, "city": c} for i, n, d, c in companies],
        "people": [{"id": i, "name": n, "title": t, "company": c, "email": e} for i, n, t, c, e in people],
        "lists": [{"id": i, "name": n} for i, n in lists],
        "campaigns": [{"id": i, "name": n, "status": st.value} for i, n, st in camps],
    }


@router.post("/dev/seed", tags=["meta"])
async def dev_seed(ctx: Ctx) -> dict[str, Any]:
    """Load clearly-marked demo data (.example domains) into the current workspace. Disabled in production."""
    from scout.errors import Forbidden
    from scout.seed import seed_workspace

    if get_settings().is_production:
        raise Forbidden("Demo data is disabled in production")
    async with session_scope() as s:
        return await seed_workspace(s, ctx.workspace_id, ctx.user_id)
