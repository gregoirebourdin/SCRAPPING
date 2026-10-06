"""Campaign service: create, lifecycle (pause/resume/cancel), status with live funnel and real ETA,
templates (spec §26–28, §76–77, §128, §131–132, §159)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import (
    CAMPAIGN_TERMINAL,
    CampaignMode,
    CampaignSourceStatus,
    CampaignStatus,
    CandidateOutcome,
    EntityType,
    ExclusionMode,
    JobStatus,
    SeedType,
)
from scout.db.models import (
    Campaign,
    CampaignExclusion,
    CampaignFilter,
    CampaignSource,
    CampaignStats,
    CampaignTemplate,
    CompanyDiscoveryEvent,
    Import,
    Job,
    JobEvent,
    List,
)
from scout.errors import Conflict, NotFound, ValidationFailed
from scout.jobs import queue
from scout.jobs.events import emit
from scout.pipeline.scoring import email_confidence_floor
from scout.schemas.campaign import (
    CampaignDefinition,
    KeywordCondition,
    RegexCondition,
    SemanticCondition,
    TechnologyCondition,
    interpret,
)
from scout.services import lists as lists_svc
from scout.services.exclusion import compile_rules


def definition_hash(defn: CampaignDefinition) -> str:
    return hashlib.sha256(
        orjson.dumps(defn.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    ).hexdigest()[:32]


def _filters_rows(campaign_id: uuid.UUID, d: CampaignDefinition) -> list[CampaignFilter]:
    rows: list[CampaignFilter] = []
    pos = 0

    def add(scope: str, field: str, op: str, value: Any, kind: str = "exact", required: bool = True) -> None:
        nonlocal pos
        rows.append(
            CampaignFilter(
                campaign_id=campaign_id,
                scope=scope,
                field=field,
                operator=op,
                value=value,
                condition_kind=kind,
                is_required=required,
                position=pos,
            )
        )
        pos += 1

    cf = d.company_filters
    if cf.industries:
        add("company", "industry", "in", cf.industries, "category")
    if cf.countries:
        add("company", "country", "in", cf.countries, "geo")
    if cf.regions:
        add("company", "region", "in", cf.regions, "geo")
    if cf.cities:
        add("company", "city", "in", cf.cities, "geo")
    if cf.employee_range:
        add("company", "employee_count", "between", [cf.employee_range.min, cf.employee_range.max], "numeric")
    if cf.exclude_keywords:
        add("company", "keywords", "not_in", cf.exclude_keywords)
    for c in d.website_conditions:
        if isinstance(c, KeywordCondition):
            add("website", "text", c.type, c.terms, "exact", c.required)
        elif isinstance(c, RegexCondition):
            add("website", "text", "regex", c.pattern, "exact", c.required)
        elif isinstance(c, SemanticCondition):
            add(
                "website",
                "semantic",
                c.type,
                {"concept": c.concept, "keywords": c.keywords},
                "semantic",
                c.required,
            )
        elif isinstance(c, TechnologyCondition):
            add("website", "technology", c.match, c.technologies, "exact", c.required)
    pf = d.people_filters
    if d.mode == CampaignMode.people:
        if pf.titles:
            add("person", "title", "in", pf.titles)
        if pf.role_families:
            add("person", "role_family", "in", [r.value for r in pf.role_families])
        if d.requires_email:
            add("email", "status", "in", [s.value for s in d.accepted_email_statuses])
            add("email", "confidence", "gte", email_confidence_floor(d), "numeric")
        add("person", "confidence", "gte", d.minimum_person_confidence, "numeric")
    add("score", "icp_score", "gte", d.minimum_icp_score, "numeric")
    return rows


async def create_campaign(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    defn: CampaignDefinition,
    *,
    user_id: str | None,
    prompt: str | None = None,
    target_list_id: uuid.UUID | None = None,
    start: bool = True,
    parent_campaign_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
) -> Campaign:
    """Persist a campaign (definition snapshot + normalized criteria) and kick off planning."""
    name = (defn.name or (prompt or "Campaign")[:60]).strip() or "Campaign"
    if target_list_id is None:
        lst, _ = await lists_svc.create_list(
            s,
            workspace_id,
            name=name,
            user_id=user_id,
            entity_type=EntityType.company if defn.mode == CampaignMode.companies else EntityType.person,
            if_exists="suffix",
            description=prompt[:300] if prompt else None,
        )
        target_list_id = lst.id
    else:
        await lists_svc.get_list(s, workspace_id, target_list_id)
    list_names: dict[uuid.UUID, str] = {}
    if defn.exclusion.list_ids:
        list_names = dict(
            (
                await s.execute(
                    sa.select(List.id, List.name).where(
                        List.workspace_id == workspace_id, List.id.in_(defn.exclusion.list_ids)
                    )
                )
            ).all()
        )
        if set(defn.exclusion.list_ids) - set(list_names):
            raise NotFound("Exclusion list not found")
    campaign = Campaign(
        workspace_id=workspace_id,
        name=name,
        prompt=prompt,
        status=CampaignStatus.draft,
        definition=defn.model_dump(mode="json"),
        definition_hash=definition_hash(defn),
        interpretation=[i.model_dump() for i in interpret(defn, list_names)],
        target_qualified_count=defn.target_qualified_count,
        target_list_id=target_list_id,
        mode=defn.mode,
        seed_type=SeedType(defn.seed.type) if defn.seed.type != "import" else SeedType.import_,
        seed_ref=defn.seed.model_dump(mode="json"),
        max_cost_usd=Decimal(str(defn.limits.max_cost_usd)) if defn.limits.max_cost_usd is not None else None,
        max_raw_candidates=defn.limits.max_raw_candidates,
        max_runtime_hours=defn.limits.max_runtime_hours,
        parent_campaign_id=parent_campaign_id,
        template_id=template_id,
        created_by=user_id,
    )
    s.add(campaign)
    await s.flush()
    await s.execute(
        sa.update(List)
        .where(List.id == target_list_id, List.source_campaign_id.is_(None))
        .values(source_campaign_id=campaign.id)
    )
    s.add(CampaignStats(campaign_id=campaign.id))
    for row in _filters_rows(campaign.id, defn):
        s.add(row)
    for rule in compile_rules(defn.exclusion, target_list_id=target_list_id):
        s.add(
            CampaignExclusion(
                campaign_id=campaign.id,
                mode=rule.mode if rule.mode != ExclusionMode.NONE else defn.exclusion.mode,
                entity=rule.entity,
                exposure_types=[t.value for t in rule.exposure_types] if rule.exposure_types else None,
                within_days=rule.within_days,
                list_ids=rule.list_ids,
                campaign_ids=rule.campaign_ids,
                import_ids=rule.import_ids,
                include_list_history=rule.include_list_history,
            )
        )
    await s.flush()
    if start:
        await start_campaign(s, campaign)
    return campaign


async def start_campaign(s: AsyncSession, campaign: Campaign) -> None:
    if campaign.status not in (CampaignStatus.draft,):
        raise Conflict(f"Campaign is already {campaign.status.value}")
    campaign.status = CampaignStatus.planning
    campaign.started_at = datetime.now(UTC)
    await queue.enqueue(
        s,
        workspace_id=campaign.workspace_id,
        campaign_id=campaign.id,
        type="campaign.plan",
        priority=20,
        dedupe_key=f"plan:{campaign.id}",
    )
    # Workspace housekeeping (reservation expiry, stale cells) — self-rescheduling, one per workspace.
    await queue.enqueue(
        s,
        workspace_id=campaign.workspace_id,
        type="system.maintenance",
        priority=1,
        dedupe_key=f"maintenance:{campaign.workspace_id}",
        run_after=datetime.now(UTC) + timedelta(minutes=10),
    )
    await emit(
        campaign.workspace_id,
        "campaign.status",
        {"campaign_id": str(campaign.id), "status": campaign.status.value},
        campaign_id=campaign.id,
        session=s,
    )


async def get_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    c = await s.get(Campaign, campaign_id)
    if c is None or c.workspace_id != workspace_id:
        raise NotFound("Campaign not found")
    return c


async def lock_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    """Row-locked load for lifecycle mutations: concurrent pause / resume / amend calls serialize (double
    clicks, chat + UI at the same time) instead of interleaving."""
    c = await s.scalar(
        sa.select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if c is None or c.workspace_id != workspace_id:
        raise NotFound("Campaign not found")
    return c


STATUS_LABEL = {
    CampaignStatus.draft: "a draft",
    CampaignStatus.planning: "starting",
    CampaignStatus.running: "running",
    CampaignStatus.paused: "paused",
    CampaignStatus.completed: "completed",
    CampaignStatus.exhausted: "out of results",
    CampaignStatus.budget_reached: "stopped by its budget",
    CampaignStatus.limit_reached: "stopped by a safety limit",
    CampaignStatus.cancelled: "cancelled",
    CampaignStatus.failed: "failed",
}

# Stopped by a condition that an amendment can lift (raise target / budget / runtime, broaden criteria).
REOPENABLE = (
    CampaignStatus.completed,
    CampaignStatus.exhausted,
    CampaignStatus.budget_reached,
    CampaignStatus.limit_reached,
)


async def pause_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    """Pause keeps everything: delivered leads, partially processed candidates (checkpointed stage data) and
    the discovery frontier (per-source cursors). Running jobs stop at their next stage boundary. Idempotent."""
    c = await lock_campaign(s, workspace_id, campaign_id)
    if c.status == CampaignStatus.paused:
        return c
    if c.status not in (CampaignStatus.running, CampaignStatus.planning):
        raise Conflict(
            f"This search is {STATUS_LABEL.get(c.status, c.status.value)} — nothing to pause",
            code="not_running",
        )
    c.status = CampaignStatus.paused
    c.paused_at = datetime.now(UTC)
    await queue.set_campaign_jobs_paused(s, c.id)
    await emit(
        workspace_id,
        "campaign.status",
        {"campaign_id": str(c.id), "status": "paused"},
        campaign_id=c.id,
        session=s,
    )
    return c


async def resume_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    """Continue where it stopped. Paused → running; stopped by a liftable condition (target, budget, runtime,
    exhausted sources) → reopened once the condition is lifted; failed → planned again. Idempotent."""
    c = await lock_campaign(s, workspace_id, campaign_id)
    if c.status in (CampaignStatus.running, CampaignStatus.planning):
        return c
    if c.status == CampaignStatus.paused:
        await _resume_paused(s, c)
    elif c.status in REOPENABLE:
        await assert_can_continue(s, c)
        await reopen_campaign(s, c)
    elif c.status == CampaignStatus.failed:
        await retry_failed(s, c)
    elif c.status == CampaignStatus.draft:
        await start_campaign(s, c)
    else:
        raise Conflict(
            "This search was cancelled — start a new one (its leads are kept)",
            code="cancelled",
            hint="Ask the assistant to “run this campaign again” to continue without repeating anyone.",
        )
    return c


async def _resume_paused(s: AsyncSession, c: Campaign) -> None:
    c.status = CampaignStatus.running
    c.paused_at = None
    await queue.resume_campaign_jobs(s, c.id)
    await queue.enqueue(
        s,
        workspace_id=c.workspace_id,
        campaign_id=c.id,
        type="campaign.tick",
        priority=15,
        dedupe_key=f"tick:{c.id}",
    )
    await emit(
        c.workspace_id,
        "campaign.status",
        {"campaign_id": str(c.id), "status": "running"},
        campaign_id=c.id,
        session=s,
    )


async def _active_sources(s: AsyncSession, campaign_id: uuid.UUID) -> int:
    return int(
        await s.scalar(
            sa.select(sa.func.count())
            .select_from(CampaignSource)
            .where(
                CampaignSource.campaign_id == campaign_id,
                CampaignSource.status.in_([CampaignSourceStatus.active, CampaignSourceStatus.pending]),
            )
        )
        or 0
    )


async def _pending_candidates(s: AsyncSession, campaign_id: uuid.UUID) -> int:
    return int(
        await s.scalar(
            sa.select(sa.func.count())
            .select_from(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.campaign_id == campaign_id,
                CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
            )
        )
        or 0
    )


async def assert_can_continue(s: AsyncSession, c: Campaign) -> None:
    """Refuse to reopen a stopped campaign while the reason it stopped still holds — with the exact fix."""
    from scout.services.usage import workspace_budget

    st = await s.get(CampaignStats, c.id)
    qualified = st.qualified if st else 0
    if qualified >= c.target_qualified_count:
        raise Conflict(
            f"Target already reached ({qualified:,}/{c.target_qualified_count:,})",
            code="target_reached",
            hint="Raise the target to continue, e.g. “+100 leads”.",
        )
    if c.max_cost_usd is not None and st is not None and st.cost_usd >= c.max_cost_usd:
        raise Conflict(
            f"Campaign budget reached (${float(c.max_cost_usd):.2f})",
            code="budget_reached",
            hint="Raise the campaign budget to continue.",
        )
    budget = await workspace_budget(c.workspace_id, max_age_s=0)
    if budget.exceeded:
        raise Conflict(
            f"Workspace monthly budget reached (${budget.monthly_budget_usd:.0f})",
            code="workspace_budget",
            hint="Raise the monthly budget in Settings → Workspace.",
        )
    if c.started_at and datetime.now(UTC) - c.started_at > timedelta(hours=c.max_runtime_hours):
        raise Conflict(
            f"Maximum runtime reached ({c.max_runtime_hours} h)",
            code="runtime_limit",
            hint="Raise the runtime limit to continue.",
        )
    if c.status == CampaignStatus.exhausted and not await _active_sources(s, c.id):
        if not await _pending_candidates(s, c.id):
            raise Conflict(
                "Every source is exhausted for these criteria",
                code="exhausted",
                hint="Broaden the search (another city, a wider size range, more job titles) to find more.",
            )


async def reopen_campaign(s: AsyncSession, c: Campaign) -> None:
    """Back to running after a stop. Candidates that were mid-pipeline get their job back (stage data was
    checkpointed, so nothing is redone or double counted)."""
    c.status = CampaignStatus.running
    c.stop_reason = None
    c.stopped_at = None
    c.paused_at = None
    await queue.resume_campaign_jobs(s, c.id)
    pending = (
        await s.execute(
            sa.select(CompanyDiscoveryEvent.id, CompanyDiscoveryEvent.company_id).where(
                CompanyDiscoveryEvent.campaign_id == c.id,
                CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
                CompanyDiscoveryEvent.company_id.is_not(None),
            )
        )
    ).all()
    for ev_id, comp_id in pending:
        await queue.enqueue(
            s,
            workspace_id=c.workspace_id,
            campaign_id=c.id,
            type="company.process",
            priority=5,
            payload={"event_id": str(ev_id)},
            dedupe_key=f"cp:{c.id}:{comp_id}",
        )
    await queue.enqueue(
        s,
        workspace_id=c.workspace_id,
        campaign_id=c.id,
        type="campaign.tick",
        priority=15,
        dedupe_key=f"tick:{c.id}",
    )
    await emit(
        c.workspace_id,
        "campaign.status",
        {"campaign_id": str(c.id), "status": "running", "reopened": True},
        campaign_id=c.id,
        session=s,
    )


async def retry_failed(s: AsyncSession, c: Campaign) -> None:
    """A failed campaign (e.g. no usable source at planning time) is planned again; sources that were already
    planned keep their cursors."""
    has_sources = await s.scalar(
        sa.select(sa.func.count()).select_from(CampaignSource).where(CampaignSource.campaign_id == c.id)
    )
    if has_sources:
        await reopen_campaign(s, c)
        return
    c.status = CampaignStatus.planning
    c.stop_reason = None
    c.stopped_at = None
    await queue.enqueue(
        s,
        workspace_id=c.workspace_id,
        campaign_id=c.id,
        type="campaign.plan",
        priority=20,
        dedupe_key=f"plan:{c.id}",
    )
    await emit(
        c.workspace_id,
        "campaign.status",
        {"campaign_id": str(c.id), "status": "planning", "retry": True},
        campaign_id=c.id,
        session=s,
    )


async def kick_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> dict[str, Any]:
    """'Looks stuck → Retry': wake every delayed job of a running campaign now, make sure a tick exists and
    reclaim jobs whose worker died. Paused / stopped campaigns are resumed instead."""
    c = await lock_campaign(s, workspace_id, campaign_id)
    if c.status not in (CampaignStatus.running, CampaignStatus.planning):
        await resume_campaign(s, workspace_id, campaign_id)
        return {"resumed": True, "woken": 0}
    res = await s.execute(
        sa.update(Job)
        .where(
            Job.campaign_id == c.id,
            Job.status.in_([JobStatus.pending, JobStatus.retrying]),
            Job.run_after > sa.func.now(),
        )
        .values(run_after=sa.func.now())
        .returning(Job.id)
    )
    woken = len(res.scalars().all())
    await queue.enqueue(
        s,
        workspace_id=c.workspace_id,
        campaign_id=c.id,
        type="campaign.tick",
        priority=15,
        dedupe_key=f"tick:{c.id}",
    )
    await s.execute(sa.text("SELECT pg_notify(:ch, 'kick')"), {"ch": queue.JOBS_CHANNEL})
    await emit(
        workspace_id,
        "campaign.progress",
        {"campaign_id": str(c.id), "kicked": True, "status": c.status.value},
        campaign_id=c.id,
        session=s,
    )
    return {"resumed": False, "woken": woken}


async def stop_campaign(s: AsyncSession, campaign: Campaign, status: CampaignStatus, reason: str) -> None:
    """Terminal transition with an explicit, human-readable reason (spec §131)."""
    if campaign.status in CAMPAIGN_TERMINAL:
        return
    campaign.status = status
    campaign.stop_reason = reason
    campaign.stopped_at = datetime.now(UTC)
    await queue.cancel_campaign_jobs(s, campaign.id)
    # release in-flight reservations
    await s.execute(
        sa.text(
            "UPDATE campaign_reservations SET status = 'released' WHERE campaign_id = :c AND status = 'reserved'"
        ),
        {"c": campaign.id},
    )
    await emit(
        campaign.workspace_id,
        "campaign.status",
        {"campaign_id": str(campaign.id), "status": status.value, "reason": reason},
        campaign_id=campaign.id,
        session=s,
    )


async def cancel_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    c = await lock_campaign(s, workspace_id, campaign_id)
    await stop_campaign(s, c, CampaignStatus.cancelled, "Stopped by user")
    return c


# ---------------------------------------------------------------------------------------------
# Status / funnel / ETA
# ---------------------------------------------------------------------------------------------


async def rolling_rate(s: AsyncSession, campaign_id: uuid.UUID, minutes: int = 30) -> float | None:
    """Qualified leads per minute over the recent window — no fake ETA (spec §128)."""
    n = await s.scalar(
        sa.select(sa.func.count())
        .select_from(JobEvent)
        .where(
            JobEvent.campaign_id == campaign_id,
            JobEvent.type == "lead.qualified",
            JobEvent.created_at >= sa.func.now() - timedelta(minutes=minutes),
        )
    )
    n = int(n or 0)
    if n < 20:
        return None
    return n / float(minutes)


async def campaign_status(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> dict[str, Any]:
    c = await get_campaign(s, workspace_id, campaign_id)
    st = await s.get(CampaignStats, c.id)
    sources = (
        await s.scalars(
            sa.select(CampaignSource)
            .where(CampaignSource.campaign_id == c.id)
            .order_by(CampaignSource.priority.desc())
        )
    ).all()
    rate = await rolling_rate(s, c.id) if c.status == CampaignStatus.running else None
    remaining = max(0, c.target_qualified_count - (st.qualified if st else 0))
    eta_minutes = round(remaining / rate) if rate else None
    rejections = (
        await s.execute(
            sa.select(CompanyDiscoveryEvent.reason, sa.func.count())
            .where(CompanyDiscoveryEvent.campaign_id == c.id, CompanyDiscoveryEvent.outcome == "rejected")
            .group_by(CompanyDiscoveryEvent.reason)
            .order_by(sa.func.count().desc())
            .limit(8)
        )
    ).all()
    stats = (
        {
            k: getattr(st, k)
            for k in (
                "raw_discovered",
                "unique_new_companies",
                "duplicates",
                "excluded_previous",
                "suppressed",
                "reserved_elsewhere",
                "companies_evaluated",
                "companies_matched",
                "people_found",
                "emails_found",
                "emails_safe",
                "emails_accepted",
                "qualified",
                "rejected",
                "errors",
                "in_flight",
            )
        }
        if st
        else {}
    )
    cost = float(st.cost_usd) if st else 0.0
    exhausted_sources = sum(1 for x in sources if x.status.value == "exhausted")
    return {
        "id": c.id,
        "name": c.name,
        "status": c.status.value,
        "stop_reason": c.stop_reason,
        "prompt": c.prompt,
        "mode": c.mode.value,
        "target": c.target_qualified_count,
        "target_list_id": c.target_list_id,
        "interpretation": c.interpretation,
        "definition": c.definition,
        "created_at": c.created_at,
        "started_at": c.started_at,
        "stopped_at": c.stopped_at,
        "stats": stats,
        "cost_usd": round(cost, 4),
        "cost_per_qualified": round(cost / stats["qualified"], 4) if stats.get("qualified") else None,
        "rate_per_minute": round(rate, 2) if rate else None,
        "eta_minutes": eta_minutes,
        "sources_exhausting": bool(sources)
        and exhausted_sources >= max(1, len(sources) - 0)
        and remaining > 0,
        "sources": [
            {
                "key": x.source_key,
                "status": x.status.value,
                "priority": x.priority,
                "raw": x.raw_count,
                "unique": x.unique_count,
                "qualified": x.qualified_count,
                "errors": x.error_count,
                "last_error": x.last_error,
            }
            for x in sources
        ],
        "top_rejections": [{"reason": r or "unknown", "count": n} for r, n in rejections],
    }


async def list_campaigns(
    s: AsyncSession, workspace_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = (
        await s.execute(
            sa.select(Campaign, CampaignStats)
            .outerjoin(CampaignStats, CampaignStats.campaign_id == Campaign.id)
            .where(Campaign.workspace_id == workspace_id)
            .order_by(Campaign.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "status": c.status.value,
            "target": c.target_qualified_count,
            "qualified": st.qualified if st else 0,
            "raw": st.raw_discovered if st else 0,
            "cost_usd": float(st.cost_usd) if st else 0.0,
            "target_list_id": c.target_list_id,
            "created_at": c.created_at,
            "stopped_at": c.stopped_at,
            "stop_reason": c.stop_reason,
            "mode": c.mode.value,
        }
        for c, st in rows
    ]


async def rejected_candidates(
    s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Why leads were rejected — never hidden (spec §187)."""
    await get_campaign(s, workspace_id, campaign_id)
    rows = (
        await s.scalars(
            sa.select(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.campaign_id == campaign_id, CompanyDiscoveryEvent.outcome != "pending"
            )
            .order_by(CompanyDiscoveryEvent.processed_at.desc().nullslast())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "domain": r.domain,
            "source": r.source_key,
            "outcome": r.outcome.value,
            "reason": r.reason,
            "stage": r.stage,
            "company_id": r.company_id,
            "observed_at": r.observed_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------------------------
# Templates (spec §159) — reusable searches; reruns only return new leads via the registry
# ---------------------------------------------------------------------------------------------


async def save_template(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str,
    campaign_id: uuid.UUID | None,
    definition: CampaignDefinition | None,
    user_id: str | None,
) -> CampaignTemplate:
    if campaign_id:
        c = await get_campaign(s, workspace_id, campaign_id)
        defn = CampaignDefinition.model_validate(c.definition)
        prompt = c.prompt
    elif definition is not None:
        defn, prompt = definition, None
    else:
        raise ValidationFailed("Provide a campaign or a definition")
    t = CampaignTemplate(
        workspace_id=workspace_id,
        name=name,
        prompt=prompt,
        definition=defn.model_dump(mode="json"),
        enrichment_plan=[e.model_dump() for e in defn.enrichments],
        created_by=user_id,
    )
    s.add(t)
    await s.flush()
    return t


async def run_template(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    template_id: uuid.UUID,
    *,
    user_id: str | None,
    only_new: bool = True,
    target_count: int | None = None,
) -> Campaign:
    t = await s.get(CampaignTemplate, template_id)
    if t is None or t.workspace_id != workspace_id:
        raise NotFound("Template not found")
    defn = CampaignDefinition.model_validate(t.definition)
    if only_new and defn.exclusion.mode == ExclusionMode.NONE:
        defn.exclusion.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
        defn.exclusion.previous_people = True
    if target_count:
        defn.target_qualified_count = target_count
    defn.name = f"{t.name} · {datetime.now(UTC).strftime('%b %d')}"
    t.last_run_at = datetime.now(UTC)
    return await create_campaign(s, workspace_id, defn, user_id=user_id, prompt=t.prompt, template_id=t.id)


async def latest_import_id(s: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID | None:
    return await s.scalar(
        sa.select(Import.id)
        .where(Import.workspace_id == workspace_id)
        .order_by(Import.created_at.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------------------------
# Amend ("resume with changes") — preview diff, apply, extend the discovery frontier, resume
# ---------------------------------------------------------------------------------------------

SAFE_WHILE_RUNNING = {"target", "budget", "runtime"}


async def _list_names(s: AsyncSession, workspace_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    return dict(
        (
            await s.execute(
                sa.select(List.id, List.name).where(List.workspace_id == workspace_id, List.id.in_(ids))
            )
        ).all()
    )


async def _extend_sources(s: AsyncSession, c: Campaign, defn: CampaignDefinition) -> int:
    """New criteria → new discovery queries appended to each source's plan (cursors untouched, so nothing
    already fetched is fetched again); exhausted sources with new queries come back; newly suitable sources
    are added. Returns the number of new queries."""
    from scout.discovery.health import health_snapshot
    from scout.discovery.router import get_source, select_sources

    if defn.company_filters.cities:
        from scout.discovery import communes

        await communes.prefetch(defn.company_filters.cities)
    added = 0
    existing = (await s.scalars(sa.select(CampaignSource).where(CampaignSource.campaign_id == c.id))).all()
    known_keys = {x.source_key for x in existing}
    for cs in existing:
        src = get_source(cs.source_key)
        if src is None or cs.source_key == "seed":
            continue
        plan = list(cs.query_plan or [])
        seen = {(q.get("key"), orjson.dumps(q.get("params", {}), option=orjson.OPT_SORT_KEYS)) for q in plan}
        keys = {q.get("key") for q in plan}
        new_q: list[dict[str, Any]] = []
        for q in src.plan(defn, expansion=0):
            qd = asdict(q)
            sig = (qd.get("key"), orjson.dumps(qd.get("params", {}), option=orjson.OPT_SORT_KEYS))
            if sig in seen:
                continue
            if qd.get("key") in keys:  # same query key, different parameters → a new, versioned query
                qd["key"] = f"{qd['key']}#a{len(plan) + len(new_q)}"
            new_q.append(qd)
        if new_q:
            cs.query_plan = [*plan, *new_q]
            if cs.status in (CampaignSourceStatus.exhausted, CampaignSourceStatus.pending):
                cs.status = CampaignSourceStatus.active
                cs.last_error = None
            added += len(new_q)
    if defn.seed.type == "search":
        try:
            chosen = select_sources(defn, health=await health_snapshot())
        except Exception:  # health is advisory; never block an amendment on it
            chosen = []
        for src, priority in chosen:
            if src.key in known_keys:
                continue
            plan = [asdict(q) for q in src.plan(defn, expansion=0)]
            s.add(
                CampaignSource(
                    campaign_id=c.id,
                    source_key=src.key,
                    priority=priority,
                    status=CampaignSourceStatus.active,
                    query_plan=plan,
                    cursor={"q": 0, "page": None, "expansion": 0},
                )
            )
            added += len(plan)
    if added:
        await s.flush()
    return added


async def _set_definition(
    s: AsyncSession,
    c: Campaign,
    defn: CampaignDefinition,
    interpretation: list[dict[str, Any]],
    *,
    criteria_changed: bool,
) -> None:
    c.definition = defn.model_dump(mode="json")
    c.definition_hash = definition_hash(defn)
    c.interpretation = interpretation
    c.target_qualified_count = defn.target_qualified_count
    c.max_cost_usd = Decimal(str(defn.limits.max_cost_usd)) if defn.limits.max_cost_usd is not None else None
    c.max_runtime_hours = defn.limits.max_runtime_hours
    if criteria_changed:
        await s.execute(sa.delete(CampaignFilter).where(CampaignFilter.campaign_id == c.id))
        for row in _filters_rows(c.id, defn):
            s.add(row)


async def amend_campaign(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    amendment: Any,
    *,
    user_id: str | None,
    dry_run: bool = False,
    resume: bool = True,
    base_hash: str | None = None,
    actor_type: Any = None,
    assistant_action_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Preview (dry_run) or apply a change. Applying is allowed while paused or stopped; while running, safe
    fields (target, budget, runtime) apply live and criteria changes pause → apply → resume in one transaction.
    Already delivered leads, exclusions and the discovery frontier are kept."""
    from scout.chat.amend import apply_amendment, merge, parse_instruction
    from scout.schemas.campaign import CampaignAmendment
    from scout.services import audit

    am = (
        amendment if isinstance(amendment, CampaignAmendment) else CampaignAmendment.model_validate(amendment)
    )
    c = await (get_campaign if dry_run else lock_campaign)(s, workspace_id, campaign_id)
    if c.status == CampaignStatus.cancelled:
        raise Conflict(
            "This search was cancelled — start a new one instead",
            code="cancelled",
            hint="Ask the assistant to “run this campaign again” with your change.",
        )
    base = CampaignDefinition.model_validate(c.definition)
    parsed = parse_instruction(am.instruction or "", base) if (am.instruction or "").strip() else None
    merged = merge(parsed, am)
    if merged.is_empty:
        raise ValidationFailed(
            "Nothing to change", code="amend_empty", hint="Describe the change, e.g. “+100 leads”."
        )
    st = await s.get(CampaignStats, c.id)
    qualified = st.qualified if st else 0
    new_defn, changes, warnings = apply_amendment(base, merged, qualified=qualified)
    running = c.status in (CampaignStatus.running, CampaignStatus.planning)
    requires_pause = running and any(ch.field not in SAFE_WHILE_RUNNING for ch in changes)
    preview: dict[str, Any] = {
        "campaign_id": str(c.id),
        "status": c.status.value,
        "changes": [ch.model_dump() for ch in changes],
        "warnings": warnings,
        "requires_pause": requires_pause,
        "noop": not changes,
        "base_hash": c.definition_hash,
        "instruction": merged.instruction,
        "interpretation": [
            i.model_dump()
            for i in interpret(new_defn, await _list_names(s, workspace_id, new_defn.exclusion.list_ids))
        ],
    }
    if dry_run:
        return {**preview, "applied": False}
    if base_hash and base_hash != c.definition_hash:
        raise Conflict(
            "This search changed since the preview",
            code="stale_preview",
            hint="Review the updated changes and confirm again.",
        )
    if not changes:
        raise ValidationFailed(
            "Nothing to change — the search already uses these criteria", code="amend_noop"
        )
    before = c.definition
    auto_paused = False
    if requires_pause:
        c.status = CampaignStatus.paused
        c.paused_at = datetime.now(UTC)
        await queue.set_campaign_jobs_paused(s, c.id)
        auto_paused = True
    # ---- apply ----
    criteria_changed = any(ch.field not in SAFE_WHILE_RUNNING for ch in changes)
    await _set_definition(s, c, new_defn, preview["interpretation"], criteria_changed=criteria_changed)
    new_queries = await _extend_sources(s, c, new_defn) if criteria_changed else 0
    await s.flush()
    from scout.db.enums import ActorType

    entry = await audit.log(
        s,
        workspace_id=workspace_id,
        actor_id=user_id,
        actor_type=actor_type or ActorType.user,
        action="campaign.amend",
        entity_type="campaign",
        entity_ids=[c.id],
        campaign_id=c.id,
        assistant_action_id=assistant_action_id,
        summary=f'Changed "{c.name}": ' + "; ".join(f"{ch.label} {ch.before} → {ch.after}" for ch in changes),
        payload={
            "instruction": merged.instruction,
            "changes": [ch.model_dump() for ch in changes],
            "before": before,
            "after": c.definition,
        },
        undo={
            "op": "restore_campaign_definition",
            "campaign_id": str(c.id),
            "definition": before,
            "expected_hash": c.definition_hash,
            "changes": [{**ch.model_dump(), "before": ch.after, "after": ch.before} for ch in changes],
        },
    )
    await emit(
        workspace_id,
        "campaign.amended",
        {
            "campaign_id": str(c.id),
            "changes": [ch.model_dump() for ch in changes],
            "target": c.target_qualified_count,
            "new_queries": new_queries,
        },
        campaign_id=c.id,
        session=s,
    )
    resumed = False
    resume_blocked: dict[str, Any] | None = None
    if resume or auto_paused:
        try:
            if c.status == CampaignStatus.paused:
                await _resume_paused(s, c)
                resumed = True
            elif c.status in REOPENABLE:
                await assert_can_continue(s, c)
                await reopen_campaign(s, c)
                resumed = True
            elif c.status == CampaignStatus.failed:
                await retry_failed(s, c)
                resumed = True
        except Conflict as exc:
            resume_blocked = {"code": exc.code, "message": exc.message, "hint": exc.hint}
    return {
        **preview,
        "applied": True,
        "auto_paused": auto_paused,
        "resumed": resumed,
        "resume_blocked": resume_blocked,
        "new_queries": new_queries,
        "status": c.status.value,
        "base_hash": c.definition_hash,
        "audit_id": entry.id,
    }


_LIVE_FIELDS: dict[str, Any] = {
    "target_qualified_count": True,
    "limits": {"max_cost_usd", "max_runtime_hours"},
}


async def restore_definition(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    definition: dict[str, Any],
    *,
    expected_hash: str | None = None,
    changes: list[dict[str, Any]] | None = None,
) -> Campaign:
    """Undo of an amendment: the previous criteria, target and limits come back, with the same pause → apply →
    resume as the amendment when criteria change mid-run. Delivered leads stay (they qualified under the
    criteria in force at the time); discovery queries added for the reverted criteria stay in the plan, and
    whatever they still find is checked against the restored filters."""
    c = await lock_campaign(s, workspace_id, campaign_id)
    if c.status == CampaignStatus.cancelled:
        raise Conflict("This search was cancelled — it can no longer be changed", code="cancelled")
    if expected_hash and c.definition_hash != expected_hash:
        raise Conflict(
            "This search changed again since that edit",
            code="stale_undo",
            hint="Undo the most recent change first.",
        )
    defn = CampaignDefinition.model_validate(definition)
    if definition_hash(defn) == c.definition_hash:
        return c
    current = CampaignDefinition.model_validate(c.definition)
    criteria_changed = current.model_dump(mode="json", exclude=_LIVE_FIELDS) != defn.model_dump(
        mode="json", exclude=_LIVE_FIELDS
    )
    paused_here = False
    if criteria_changed and c.status in (CampaignStatus.running, CampaignStatus.planning):
        c.status = CampaignStatus.paused
        c.paused_at = datetime.now(UTC)
        await queue.set_campaign_jobs_paused(s, c.id)
        paused_here = True
    interpretation = [
        i.model_dump() for i in interpret(defn, await _list_names(s, workspace_id, defn.exclusion.list_ids))
    ]
    await _set_definition(s, c, defn, interpretation, criteria_changed=criteria_changed)
    await s.flush()
    if paused_here:
        await _resume_paused(s, c)
    await emit(
        workspace_id,
        "campaign.amended",
        {
            "campaign_id": str(c.id),
            "changes": changes or [],
            "target": c.target_qualified_count,
            "new_queries": 0,
            "undone": True,
        },
        campaign_id=c.id,
        session=s,
    )
    return c


# ---------------------------------------------------------------------------------------------
# Live snapshot — what the run header / chat run card restore after a refresh, plus stall diagnostics
# ---------------------------------------------------------------------------------------------


async def live_snapshot(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> dict[str, Any]:
    status = await campaign_status(s, workspace_id, campaign_id)
    cid = campaign_id
    stages = dict(
        (
            await s.execute(
                sa.select(CompanyDiscoveryEvent.stage, sa.func.count())
                .where(
                    CompanyDiscoveryEvent.campaign_id == cid,
                    CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
                )
                .group_by(CompanyDiscoveryEvent.stage)
            )
        ).all()
    )
    in_flight = [
        {"event_id": str(r.id), "name": r.name, "domain": r.domain, "stage": r.stage}
        for r in (
            await s.execute(
                sa.select(
                    CompanyDiscoveryEvent.id,
                    CompanyDiscoveryEvent.name,
                    CompanyDiscoveryEvent.domain,
                    CompanyDiscoveryEvent.stage,
                )
                .where(
                    CompanyDiscoveryEvent.campaign_id == cid,
                    CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
                )
                .order_by(
                    sa.case((CompanyDiscoveryEvent.stage == "discovered", 1), else_=0),
                    CompanyDiscoveryEvent.observed_at,
                )
                .limit(12)
            )
        ).all()
    ]
    recent = [
        {"id": r.id, "type": r.type, "payload": r.payload, "at": r.created_at}
        for r in (
            await s.execute(
                sa.select(JobEvent.id, JobEvent.type, JobEvent.payload, JobEvent.created_at)
                .where(JobEvent.campaign_id == cid)
                .order_by(JobEvent.id.desc())
                .limit(15)
            )
        ).all()
    ]
    jobs = dict(
        (
            await s.execute(
                sa.select(Job.status, sa.func.count())
                .where(Job.campaign_id == cid, Job.status.in_(_LIVE_JOB_STATES))
                .group_by(Job.status)
            )
        ).all()
    )
    overdue = (
        await s.execute(
            sa.select(sa.func.count(), sa.func.min(Job.run_after)).where(
                Job.campaign_id == cid,
                Job.status.in_([JobStatus.pending, JobStatus.retrying]),
                Job.run_after < sa.func.now() - timedelta(seconds=30),
                Job.blocked_by_count == 0,
            )
        )
    ).one()
    last_error = await s.scalar(
        sa.select(Job.last_error)
        .where(Job.campaign_id == cid, Job.last_error.is_not(None))
        .order_by(Job.updated_at.desc())
        .limit(1)
    )
    now = await s.scalar(sa.select(sa.func.now()))
    last_event_at = recent[0]["at"] if recent else None
    c = await s.get(Campaign, cid)
    from scout.config import get_settings

    return {
        **status,
        "stages": {k: int(v) for k, v in stages.items()},
        "in_flight": in_flight,
        "recent": recent,
        "health": {
            "server_time": now,
            "last_event_at": last_event_at,
            "last_progress_at": c.last_progress_at if c else None,
            "jobs": {k.value if hasattr(k, "value") else str(k): int(v) for k, v in jobs.items()},
            "overdue_jobs": int(overdue[0] or 0),
            "oldest_overdue_s": int((now - overdue[1]).total_seconds())
            if overdue[1] is not None and now is not None
            else None,
            "last_error": last_error,
            "workers_enabled": get_settings().worker_enabled,
        },
    }


_LIVE_JOB_STATES = [
    JobStatus.pending,
    JobStatus.claimed,
    JobStatus.running,
    JobStatus.retrying,
    JobStatus.paused,
]
