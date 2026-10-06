"""Campaign service: create, lifecycle (pause/resume/cancel), status with live funnel and real ETA,
templates (spec §26–28, §76–77, §128, §131–132, §159)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import (
    CAMPAIGN_TERMINAL,
    CampaignMode,
    CampaignStatus,
    EntityType,
    ExclusionMode,
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
    JobEvent,
    List,
)
from scout.errors import Conflict, NotFound, ValidationFailed
from scout.jobs import queue
from scout.jobs.events import emit
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
            add("email", "confidence", "gte", d.minimum_email_confidence, "numeric")
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


async def pause_campaign(s: AsyncSession, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    c = await get_campaign(s, workspace_id, campaign_id)
    if c.status not in (CampaignStatus.running, CampaignStatus.planning):
        raise Conflict(f"Cannot pause a campaign that is {c.status.value}")
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
    c = await get_campaign(s, workspace_id, campaign_id)
    if c.status != CampaignStatus.paused:
        raise Conflict(f"Cannot resume a campaign that is {c.status.value}")
    c.status = CampaignStatus.running
    c.paused_at = None
    await queue.resume_campaign_jobs(s, c.id)
    await queue.enqueue(
        s,
        workspace_id=workspace_id,
        campaign_id=c.id,
        type="campaign.tick",
        priority=15,
        dedupe_key=f"tick:{c.id}",
    )
    await emit(
        workspace_id,
        "campaign.status",
        {"campaign_id": str(c.id), "status": "running"},
        campaign_id=c.id,
        session=s,
    )
    return c


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
    c = await get_campaign(s, workspace_id, campaign_id)
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
