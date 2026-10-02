"""Run control: start / inspect / cancel pipeline runs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Event, Run
from ..pipeline.orchestrator import STAGES, RunConfig, manager
from ..schemas import EventOut, RunCreate, RunOut

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=list[RunOut])
async def list_runs(session: AsyncSession = Depends(get_session)) -> list[Run]:
    return list((await session.execute(select(Run).order_by(Run.id.desc()).limit(50))).scalars())


@router.post("", response_model=RunOut, status_code=201)
async def start_run(body: RunCreate, session: AsyncSession = Depends(get_session)) -> Run:
    if manager.active:
        raise HTTPException(409, f"run {manager.run_id} is still active")
    bad = [s for s in (body.stages or []) if s not in STAGES]
    if bad:
        raise HTTPException(422, f"unknown stages: {bad}; valid: {STAGES}")
    cfg = RunConfig(
        target_leads=body.target_leads,
        engines=body.engines or RunConfig().engines,
        countries=body.countries or RunConfig().countries,
        max_queries=body.max_queries,
        stages=body.stages or list(STAGES),
        seed_domains=body.seed_domains,
        extra_queries=body.extra_queries,
        min_score=body.min_score,
        resume=body.resume,
        name=body.name,
    )
    run_id = await manager.start(cfg)
    run = await session.get(Run, run_id)
    assert run is not None
    return run


@router.get("/active", response_model=RunOut | None)
async def active_run(session: AsyncSession = Depends(get_session)) -> Run | None:
    if manager.run_id is None:
        return None
    return await session.get(Run, manager.run_id)


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: int, session: AsyncSession = Depends(get_session)) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if manager.run_id == run_id and manager.active:
        manager.cancel.set()
    elif run.status in ("running", "pending"):
        run.status = "cancelled"
        await session.commit()
    await session.refresh(run)
    return run


@router.get("/{run_id}/events", response_model=list[EventOut])
async def run_events(run_id: int, after: int = 0, limit: int = 200, session: AsyncSession = Depends(get_session)) -> list[Event]:
    q = select(Event).where(Event.run_id == run_id, Event.id > after).order_by(Event.id.desc()).limit(min(limit, 500))
    rows = list((await session.execute(q)).scalars())
    rows.reverse()
    return rows
