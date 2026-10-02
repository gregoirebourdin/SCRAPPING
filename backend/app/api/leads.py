"""Leads: list / filter / detail / patch / export / stats."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..export import export_csv
from ..models import Agency, AgencyEmail, Candidate, Client, Funnel, Run
from ..pipeline.orchestrator import manager
from ..schemas import LeadDetail, LeadPatch, LeadSummary, Page, Stats

router = APIRouter(prefix="/api", tags=["leads"])

SORTS = {"score": Agency.score, "created": Agency.created_at, "name": Agency.name, "country": Agency.country, "tier": Agency.tier}


def _summary(a: Agency) -> LeadSummary:
    primary = next((e for e in a.emails if e.is_primary), None) or (a.emails[0] if a.emails else None)
    client = sorted(a.clients, key=lambda c: (c.status != "resolved", not c.funnels, -c.confidence))[0] if a.clients else None
    funnel = client.funnels[0] if client and client.funnels else None
    data = LeadSummary.model_validate(a)
    data.primary_email = primary.email if primary else None
    data.email_verification = primary.verification if primary else None
    data.email_count = len(a.emails)
    data.client_count = len(a.clients)
    data.top_client = client.name if client else None
    data.top_client_role = client.role_title if client else None
    data.top_client_website = client.website if client else None
    data.top_funnel_url = funnel.url if funnel else None
    data.top_funnel_type = funnel.funnel_type if funnel else None
    data.top_funnel_platform = funnel.platform if funnel else None
    return data


def _detail(a: Agency) -> LeadDetail:
    base = _summary(a).model_dump()
    detail = LeadDetail.model_validate(a)
    for k, v in base.items():
        setattr(detail, k, v)
    return detail


_LOAD = (selectinload(Agency.emails), selectinload(Agency.clients).selectinload(Client.funnels))


@router.get("/leads", response_model=Page)
async def list_leads(
    q: str | None = None,
    status: str | None = Query(None, description="qualified|review|rejected (comma separated)"),
    tier: str | None = Query(None, description="A|B|C (comma separated)"),
    min_score: int = 0,
    has_email: bool | None = None,
    has_client: bool | None = None,
    has_funnel: bool | None = None,
    has_linkedin: bool | None = None,
    country: str | None = None,
    user_status: str | None = None,
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    size: int = 50,
    session: AsyncSession = Depends(get_session),
) -> Page:
    stmt = select(Agency).options(*_LOAD)
    conds = [Agency.score >= min_score]
    if status:
        conds.append(Agency.status.in_(status.split(",")))
    else:
        conds.append(Agency.status.in_(["qualified", "review"]))
    if tier:
        conds.append(Agency.tier.in_(tier.split(",")))
    if q:
        like = f"%{q}%"
        conds.append(or_(Agency.name.ilike(like), Agency.domain.ilike(like), Agency.tagline.ilike(like), Agency.founder_name.ilike(like)))
    if country:
        conds.append(Agency.country == country)
    if user_status:
        conds.append(Agency.user_status == user_status)
    if has_email is not None:
        sub = select(AgencyEmail.agency_id).where(AgencyEmail.verification.notin_(["invalid", "no_mx"]))
        conds.append(Agency.id.in_(sub) if has_email else Agency.id.notin_(sub))
    if has_client is not None:
        sub = select(Client.agency_id)
        conds.append(Agency.id.in_(sub) if has_client else Agency.id.notin_(sub))
    if has_funnel is not None:
        sub = select(Client.agency_id).join(Funnel, Funnel.client_id == Client.id)
        conds.append(Agency.id.in_(sub) if has_funnel else Agency.id.notin_(sub))
    if has_linkedin is not None:
        conds.append(Agency.founder_linkedin.isnot(None) if has_linkedin else Agency.founder_linkedin.is_(None))
    stmt = stmt.where(*conds)
    total = (await session.execute(select(func.count()).select_from(select(Agency.id).where(*conds).subquery()))).scalar_one()
    col = SORTS.get(sort, Agency.score)
    stmt = stmt.order_by(col.asc() if order == "asc" else col.desc(), Agency.id.desc())
    size = max(1, min(size, 500))
    rows = list((await session.execute(stmt.offset((page - 1) * size).limit(size))).scalars())
    return Page(items=[_summary(a) for a in rows], total=total, page=page, size=size)


@router.get("/leads/{lead_id}", response_model=LeadDetail)
async def get_lead(lead_id: int, session: AsyncSession = Depends(get_session)) -> LeadDetail:
    a = (await session.execute(select(Agency).where(Agency.id == lead_id).options(*_LOAD))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "lead not found")
    return _detail(a)


@router.patch("/leads/{lead_id}", response_model=LeadDetail)
async def patch_lead(lead_id: int, body: LeadPatch, session: AsyncSession = Depends(get_session)) -> LeadDetail:
    a = (await session.execute(select(Agency).where(Agency.id == lead_id).options(*_LOAD))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "lead not found")
    if body.user_status is not None:
        a.user_status = body.user_status
    if body.notes is not None:
        a.notes = body.notes
    if body.status is not None:
        if body.status not in ("qualified", "review", "rejected"):
            raise HTTPException(422, "status must be qualified|review|rejected")
        a.status = body.status
    await session.commit()
    await session.refresh(a)
    return _detail(a)


@router.get("/export.csv")
async def export(status: str = "qualified,review", tier: str | None = None, min_score: int = 0):
    path = settings.data_dir / "exports" / f"leads_{datetime.utcnow():%Y%m%d_%H%M%S}.csv"
    await export_csv(path, statuses=tuple(status.split(",")), tiers=tuple(tier.split(",")) if tier else None, min_score=min_score)
    return FileResponse(Path(path), media_type="text/csv", filename=path.name)


@router.get("/stats", response_model=Stats)
async def stats(session: AsyncSession = Depends(get_session)) -> Stats:
    cands = dict((await session.execute(select(Candidate.status, func.count()).group_by(Candidate.status))).all())
    ags = dict((await session.execute(select(Agency.status, func.count()).group_by(Agency.status))).all())
    live = Agency.status.in_(["qualified", "review"])
    qualified = (await session.execute(select(func.count()).where(Agency.status == "qualified"))).scalar_one()
    with_email = (await session.execute(select(func.count(func.distinct(AgencyEmail.agency_id))).join(Agency).where(live, AgencyEmail.verification.notin_(["invalid", "no_mx"])))).scalar_one()
    with_verified = (await session.execute(select(func.count(func.distinct(AgencyEmail.agency_id))).join(Agency).where(live, AgencyEmail.verification.in_(["smtp_valid", "catch_all"])))).scalar_one()
    with_client = (await session.execute(select(func.count(func.distinct(Client.agency_id))).join(Agency).where(live))).scalar_one()
    with_funnel = (await session.execute(select(func.count(func.distinct(Client.agency_id))).join(Funnel, Funnel.client_id == Client.id).join(Agency, Agency.id == Client.agency_id).where(live))).scalar_one()
    avg = (await session.execute(select(func.avg(Agency.score)).where(live))).scalar_one() or 0.0
    active = await session.get(Run, manager.run_id) if manager.run_id else None
    engines = (active.stats or {}).get("engines", {}) if active else {}
    return Stats(
        candidates=sum(cands.values()), candidates_by_status=cands, agencies=sum(ags.values()), agencies_by_status=ags,
        qualified=qualified, with_email=with_email, with_verified_email=with_verified, with_client=with_client, with_funnel=with_funnel,
        avg_score=round(float(avg), 1), engines=engines, active_run=active,
    )
