"""Campaign engine jobs: plan → discover (per source, resumable) → tick (adaptive sourcing, stop conditions).

Per-company work lives in scout.pipeline.processor (job `company.process`).
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.enums import (
    CAMPAIGN_TERMINAL,
    CampaignSourceStatus,
    CampaignStatus,
    CandidateOutcome,
    CompanyStatus,
    EntityType,
    JobStatus,
    SourceType,
    VerificationRequestStatus,
)
from scout.db.models import (
    Campaign,
    CampaignSource,
    CampaignStats,
    Company,
    CompanyDiscoveryEvent,
    EmailVerificationRequest,
    Job,
    ListMembership,
    Person,
)
from scout.errors import BlockedError, CampaignPaused, RateLimitedError, RetryableError
from scout.jobs import queue
from scout.jobs.events import emit
from scout.jobs.registry import JobContext, job_handler
from scout.pipeline.campaigns import stop_campaign
from scout.pipeline.exclusions import load_rules
from scout.schemas.campaign import CampaignDefinition
from scout.services import registry
from scout.services.exclusion import excluded_entities, suppressed_companies
from scout.services.usage import workspace_budget
from scout.util.text import normalize_company_name

log = structlog.get_logger("pipeline")

SOURCE_EVIDENCE_TYPE: dict[str, SourceType] = {
    "fr_registry": SourceType.registry,
    "google_maps": SourceType.maps,
    "osm": SourceType.directory,
    "web_search": SourceType.search_snippet,
    "gemini_search": SourceType.grounded_search,
    "yc": SourceType.directory,
    "hn_hiring": SourceType.search_snippet,
    "github": SourceType.directory,
    "fixture": SourceType.directory,
    "seed": SourceType.derived,
}

TICK_SECONDS = 15.0
PRIOR_YIELD = 0.15


async def bump_stats(s: Any, campaign_id: uuid.UUID, **deltas: int) -> None:
    deltas = {k: v for k, v in deltas.items() if v}
    if not deltas:
        return
    await s.execute(
        sa.update(CampaignStats)
        .where(CampaignStats.campaign_id == campaign_id)
        .values(**{k: getattr(CampaignStats, k) + v for k, v in deltas.items()}, updated_at=sa.func.now())
    )


# =============================================================================================
# campaign.plan
# =============================================================================================


@job_handler("campaign.plan", timeout_s=180)
async def plan_campaign(ctx: JobContext) -> dict[str, Any]:
    from scout.discovery.health import health_snapshot
    from scout.discovery.router import select_sources

    async with session_scope() as s:
        c = await s.get(Campaign, ctx.campaign_id)
        if c is None or c.status in CAMPAIGN_TERMINAL:
            return {"skipped": True}
        if c.status == CampaignStatus.paused:
            raise CampaignPaused("paused")  # planned on resume: a pause during planning is never overridden
        defn = CampaignDefinition.model_validate(c.definition)
        if defn.seed.type != "search":
            n = await _plan_seeded(s, c, defn)
            c.status = CampaignStatus.running
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
                {"campaign_id": str(c.id), "status": "running", "seeded": n},
                campaign_id=c.id,
                session=s,
            )
            return {"seeded": n}
    health = await health_snapshot()
    chosen = select_sources(defn, health=health)
    async with session_scope() as s:
        c = await s.get(Campaign, ctx.campaign_id)
        assert c is not None
        if not chosen:
            await stop_campaign(
                s,
                c,
                CampaignStatus.failed,
                "No suitable discovery source is configured for this ICP (check source settings)",
            )
            return {"sources": 0}
        for source, priority in chosen:
            plan = [asdict(q) for q in source.plan(defn, expansion=0)]
            stmt = (
                pg_insert(CampaignSource)
                .values(
                    campaign_id=c.id,
                    source_key=source.key,
                    priority=priority,
                    status=CampaignSourceStatus.active,
                    query_plan=plan,
                    cursor={"q": 0, "page": None, "expansion": 0},
                )
                .on_conflict_do_nothing(index_elements=["campaign_id", "source_key"])
            )
            await s.execute(stmt)
            await queue.enqueue(
                s,
                workspace_id=c.workspace_id,
                campaign_id=c.id,
                type="campaign.discover",
                priority=10 + priority,
                payload={"source_key": source.key},
                dedupe_key=f"discover:{c.id}:{source.key}",
            )
        if c.status == CampaignStatus.planning:  # paused meanwhile → stays paused (sources are kept)
            c.status = CampaignStatus.running
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
            {"campaign_id": str(c.id), "status": c.status.value, "sources": [x.key for x, _ in chosen]},
            campaign_id=c.id,
            session=s,
        )
    return {"sources": [x.key for x, _ in chosen]}


async def _plan_seeded(s: Any, c: Campaign, defn: CampaignDefinition) -> int:
    """Campaign seeded from a list / selection / domains / import: no rediscovery (spec §107, §175)."""
    company_ids: list[uuid.UUID] = []
    seed = defn.seed
    if seed.type == "list" and seed.list_id:
        rows = (
            await s.execute(
                sa.select(ListMembership.company_id, Person.company_id)
                .outerjoin(Person, Person.id == ListMembership.person_id)
                .where(ListMembership.list_id == seed.list_id, ListMembership.workspace_id == c.workspace_id)
            )
        ).all()
        company_ids = [a or b for a, b in rows if (a or b)]
    elif seed.type == "selection":
        company_ids = list(seed.company_ids)
        if seed.person_ids:
            company_ids += [
                x
                for x in (
                    await s.scalars(
                        sa.select(Person.company_id).where(
                            Person.workspace_id == c.workspace_id, Person.id.in_(seed.person_ids)
                        )
                    )
                ).all()
                if x
            ]
    elif seed.type == "import" and seed.import_id:
        rows = await s.execute(
            sa.text(
                "SELECT DISTINCT company_id FROM lead_exposures WHERE workspace_id = :ws AND import_id = :i "
                "AND company_id IS NOT NULL"
            ),
            {"ws": c.workspace_id, "i": seed.import_id},
        )
        company_ids = [r[0] for r in rows]
    elif seed.type == "domains":
        ev = registry.Evidence(
            source_type=SourceType.user,
            confidence=0.9,
            source_key="user",
            evidence="Seed domain provided by user",
        )
        for d in seed.domains[:5000]:
            dom = registry.canonical_domain(d)
            if not dom:
                continue
            comp, _ = await registry.upsert_company(
                s,
                c.workspace_id,
                registry.CompanyInput(
                    name=dom.split(".")[0].replace("-", " ").title(), domain=dom, website=f"https://{dom}/"
                ),
                ev,
                campaign_id=c.id,
            )
            company_ids.append(comp.id)
    company_ids = list(dict.fromkeys(company_ids))
    if not company_ids:
        await stop_campaign(s, c, CampaignStatus.exhausted, "The seed contains no companies")
        return 0
    s.add(
        CampaignSource(
            campaign_id=c.id,
            source_key="seed",
            priority=100,
            status=CampaignSourceStatus.exhausted,
            raw_count=len(company_ids),
            unique_count=len(company_ids),
        )
    )
    comps = (
        await s.scalars(
            sa.select(Company).where(Company.id.in_(company_ids), Company.workspace_id == c.workspace_id)
        )
    ).all()
    n = 0
    for comp in comps:
        res = await s.execute(
            pg_insert(CompanyDiscoveryEvent)
            .values(
                workspace_id=c.workspace_id,
                campaign_id=c.id,
                company_id=comp.id,
                source_key="seed",
                source_entity_id=str(comp.id),
                candidate_key=f"id:{comp.id}",
                name=comp.name,
                website=comp.website_url,
                domain=comp.normalized_domain,
                location={"city": comp.city, "country": comp.country},
                category=comp.category_raw,
            )
            .on_conflict_do_nothing(index_elements=["campaign_id", "candidate_key"])
            .returning(CompanyDiscoveryEvent.id)
        )
        ev_id = res.scalar_one_or_none()
        if ev_id:
            await queue.enqueue(
                s,
                workspace_id=c.workspace_id,
                campaign_id=c.id,
                type="company.process",
                priority=5,
                payload={"event_id": str(ev_id)},
                dedupe_key=f"cp:{c.id}:{comp.id}",
            )
            n += 1
    await bump_stats(s, c.id, raw_discovered=n, unique_new_companies=n, in_flight=n)
    return n


# =============================================================================================
# campaign.discover
# =============================================================================================


async def _pipeline_need(s: Any, c: Campaign) -> tuple[int, int, float]:
    """(in_flight, desired_in_flight, estimated_yield)."""
    st = await s.get(CampaignStats, c.id)
    processed = (
        await s.scalar(
            sa.select(sa.func.count())
            .select_from(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.campaign_id == c.id,
                CompanyDiscoveryEvent.outcome.in_([CandidateOutcome.qualified, CandidateOutcome.rejected]),
            )
        )
        or 0
    )
    in_flight = (
        await s.scalar(
            sa.select(sa.func.count())
            .select_from(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.campaign_id == c.id,
                CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
            )
        )
        or 0
    )
    qualified = st.qualified if st else 0
    # Bayesian-smoothed yield (prior 15 % worth 50 candidates)
    yield_ = (qualified + PRIOR_YIELD * 50) / (processed + 50)
    remaining = max(0, c.target_qualified_count - qualified)
    desired = int(min(600, max(25, math.ceil(remaining / max(yield_, 0.01) * 0.6))))
    return int(in_flight), desired, yield_


@job_handler("campaign.discover", timeout_s=600)
async def discover(ctx: JobContext) -> dict[str, Any] | None:
    from scout.discovery.base import DiscoveryQuery
    from scout.discovery.health import record_request
    from scout.discovery.router import get_source

    key = ctx.payload["source_key"]
    source = get_source(key)
    async with session_scope() as s:
        c = await s.get(Campaign, ctx.campaign_id)
        if c is None or c.status not in (CampaignStatus.running, CampaignStatus.planning):
            return {"skipped": "campaign not running"}
        cs = await s.scalar(
            sa.select(CampaignSource).where(
                CampaignSource.campaign_id == c.id, CampaignSource.source_key == key
            )
        )
        if cs is None or cs.status in (CampaignSourceStatus.exhausted, CampaignSourceStatus.disabled):
            return {"skipped": "source inactive"}
        if source is None:
            cs.status = CampaignSourceStatus.disabled
            cs.last_error = "Source adapter not available"
            return {"skipped": "unknown source"}
        defn = CampaignDefinition.model_validate(c.definition)
        st = await s.get(CampaignStats, c.id)
        if st and st.raw_discovered >= c.max_raw_candidates:
            cs.status = CampaignSourceStatus.exhausted
            cs.last_error = "Safety limit: max raw candidates reached"
            return {"limit": True}
        in_flight, desired, _ = await _pipeline_need(s, c)
        if in_flight >= desired:
            ctx.later(20.0)  # back-pressure: let the per-company pipeline catch up
            return None
        cursor = dict(cs.cursor or {})
        plan = list(cs.query_plan or [])
        qi = int(cursor.get("q", 0))
        expansion = int(cursor.get("expansion", 0))
        if qi >= len(plan):
            if expansion < 2:
                known = {q.get("key") for q in plan}
                new = [asdict(q) for q in source.plan(defn, expansion=expansion + 1) if q.key not in known]
                cursor["expansion"] = expansion + 1
                if new:
                    plan.extend(new)
                    cs.query_plan = plan
                    cs.cursor = cursor
                    ctx.later(0.2)
                    return {"expanded": len(new)}
                cs.cursor = cursor
                ctx.later(0.2)
                return {"expanded": 0}
            cs.status = CampaignSourceStatus.exhausted
            await emit(
                c.workspace_id,
                "campaign.progress",
                {"campaign_id": str(c.id), "source_exhausted": key},
                campaign_id=c.id,
                session=s,
            )
            return {"exhausted": True}
        qd = plan[qi]
        query = DiscoveryQuery(key=qd["key"], params=qd.get("params", {}), weight=qd.get("weight", 1.0))
        page_cursor = cursor.get("page")
        workspace_id, campaign_id = c.workspace_id, c.id
    started = time.monotonic()
    try:
        page = await source.discover(query, page_cursor)
    except (RateLimitedError, BlockedError) as exc:
        await record_request(
            key, ok=False, blocked=True, latency_ms=int((time.monotonic() - started) * 1000), error=str(exc)
        )
        async with session_scope() as s:
            await s.execute(
                sa.update(CampaignSource)
                .where(CampaignSource.campaign_id == campaign_id, CampaignSource.source_key == key)
                .values(
                    error_count=CampaignSource.error_count + 1,
                    last_error=f"Search source rate-limited: {exc}"[:500],
                )
            )
        ctx.later(90.0 if isinstance(exc, RateLimitedError) else 300.0)
        return None
    except RetryableError as exc:
        await record_request(
            key, ok=False, latency_ms=int((time.monotonic() - started) * 1000), error=str(exc)
        )
        async with session_scope() as s:
            await s.execute(
                sa.update(CampaignSource)
                .where(CampaignSource.campaign_id == campaign_id, CampaignSource.source_key == key)
                .values(error_count=CampaignSource.error_count + 1, last_error=str(exc)[:500])
            )
        raise
    except Exception as exc:  # adapter bug or bad query: skip this query, never loop forever
        await record_request(
            key, ok=False, latency_ms=int((time.monotonic() - started) * 1000), error=str(exc)
        )
        log.warning("discover.query_failed", source=key, query=query.key, error=str(exc))
        async with session_scope() as s:
            cs = await s.scalar(
                sa.select(CampaignSource).where(
                    CampaignSource.campaign_id == campaign_id, CampaignSource.source_key == key
                )
            )
            if cs is not None:
                cur = dict(cs.cursor or {})
                cur["q"] = int(cur.get("q", 0)) + 1
                cur["page"] = None
                cs.cursor = cur
                cs.error_count += 1
                cs.last_error = f"{type(exc).__name__}: {exc}"[:500]
        ctx.later(1.0)
        return None
    await record_request(
        key,
        ok=True,
        blocked=page.blocked,
        latency_ms=int((time.monotonic() - started) * 1000),
        results=len(page.candidates),
    )
    result = await ingest_candidates(workspace_id, campaign_id, key, page.candidates, defn)
    async with session_scope() as s:
        cs = await s.scalar(
            sa.select(CampaignSource).where(
                CampaignSource.campaign_id == campaign_id, CampaignSource.source_key == key
            )
        )
        if cs is not None:
            cur = dict(cs.cursor or {})
            if page.next_cursor is None:
                cur["q"] = int(cur.get("q", 0)) + 1
                cur["page"] = None
            else:
                cur["page"] = page.next_cursor
            cs.cursor = cur
            cs.raw_count += len(page.candidates)
            cs.unique_count += result["queued"]
            cs.last_run_at = datetime.now(UTC)
        await emit(
            workspace_id,
            "campaign.progress",
            {"campaign_id": str(campaign_id), **result, "source": key},
            campaign_id=campaign_id,
            session=s,
        )
    if not page.candidates and page.next_cursor and page.next_cursor.get("poll_after_s"):
        ctx.later(float(page.next_cursor["poll_after_s"]))  # remote job still running (e.g. Maps scraper)
    else:
        ctx.later(0.2 if page.candidates or page.next_cursor else 0.5)
    return None


async def ingest_candidates(
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    source_key: str,
    candidates: list[Any],
    defn: CampaignDefinition,
) -> dict[str, int]:
    """Canonicalize → in-campaign dedupe → registry → suppression/exclusion → cheap prequal → queue."""
    from scout.discovery.health import record_outcomes

    counts = {
        "raw": len(candidates),
        "duplicates": 0,
        "excluded": 0,
        "suppressed": 0,
        "rejected": 0,
        "queued": 0,
    }
    if not candidates:
        return counts
    async with session_scope() as s:
        rules = await load_rules(s, campaign_id)
        inserted: list[tuple[uuid.UUID, Any, str | None]] = []
        for cand in candidates:
            domain = registry.canonical_domain(cand.website, cand.domain)
            city = (cand.location or {}).get("city")
            key = registry.company_key(domain, cand.name, city)
            res = await s.execute(
                pg_insert(CompanyDiscoveryEvent)
                .values(
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    source_key=source_key,
                    source_entity_id=cand.source_entity_id,
                    candidate_key=key,
                    name=cand.name[:300],
                    website=cand.website,
                    domain=domain,
                    location=cand.location or {},
                    category=cand.category,
                    source_url=cand.source_url,
                    raw_data=_raw_payload(cand),
                )
                .on_conflict_do_nothing(index_elements=["campaign_id", "candidate_key"])
                .returning(CompanyDiscoveryEvent.id)
            )
            ev_id = res.scalar_one_or_none()
            if ev_id is None:
                counts["duplicates"] += 1
                continue
            inserted.append((ev_id, cand, domain))
        # suppression by domain (batch)
        _, sup_domains = await suppressed_companies(s, workspace_id, domains=[d for _, _, d in inserted if d])
        queue_ids: list[tuple[uuid.UUID, uuid.UUID]] = []
        for ev_id, cand, domain in inserted:
            ev = await s.get(CompanyDiscoveryEvent, ev_id)
            assert ev is not None
            if domain and domain in sup_domains:
                ev.outcome, ev.reason, ev.processed_at = (
                    CandidateOutcome.suppressed,
                    "Suppressed domain",
                    sa.func.now(),
                )
                counts["suppressed"] += 1
                continue
            reason = _prequalify(defn, cand)
            existing = await registry.find_company(
                s,
                workspace_id,
                domain=domain,
                registry_source=cand.registry_source,
                registry_id=cand.registry_id,
                name=cand.name,
                city=(cand.location or {}).get("city"),
            )
            if existing is not None:
                ev.company_id = existing.id
                sup_ids, _ = await suppressed_companies(s, workspace_id, company_ids=[existing.id])
                if sup_ids:
                    ev.outcome, ev.reason, ev.processed_at = (
                        CandidateOutcome.suppressed,
                        "Suppressed company",
                        sa.func.now(),
                    )
                    counts["suppressed"] += 1
                    continue
                excl = await excluded_entities(
                    s, workspace_id, EntityType.company, [existing.id], rules, current_campaign_id=campaign_id
                )
                if excl:
                    ev.outcome, ev.reason, ev.processed_at = (
                        CandidateOutcome.excluded_previous,
                        f"Excluded: {excl[existing.id]}",
                        sa.func.now(),
                    )
                    counts["excluded"] += 1
                    continue
            if reason:
                ev.outcome, ev.reason, ev.processed_at = CandidateOutcome.rejected, reason, sa.func.now()
                ev.stage = "prequalification"
                counts["rejected"] += 1
                # still remember the company in the registry (cheap, no network)
                comp, _ = await registry.upsert_company(
                    s,
                    workspace_id,
                    _company_input(cand, domain),
                    _evidence(source_key, cand),
                    campaign_id=campaign_id,
                    existing=existing,
                )
                ev.company_id = comp.id
                continue
            comp, _created = await registry.upsert_company(
                s,
                workspace_id,
                _company_input(cand, domain),
                _evidence(source_key, cand),
                campaign_id=campaign_id,
                existing=existing,
            )
            ev.company_id = comp.id
            # same company reached through another candidate key in this campaign → duplicate
            other = await s.scalar(
                sa.select(CompanyDiscoveryEvent.id).where(
                    CompanyDiscoveryEvent.campaign_id == campaign_id,
                    CompanyDiscoveryEvent.company_id == comp.id,
                    CompanyDiscoveryEvent.id != ev.id,
                )
            )
            if other is not None:
                ev.outcome, ev.reason, ev.processed_at = (
                    CandidateOutcome.duplicate,
                    "Same company already in this campaign",
                    sa.func.now(),
                )
                counts["duplicates"] += 1
                continue
            queue_ids.append((ev.id, comp.id))
        for ev_id, comp_id in queue_ids:
            await queue.enqueue(
                s,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                type="company.process",
                priority=5,
                payload={"event_id": str(ev_id)},
                dedupe_key=f"cp:{campaign_id}:{comp_id}",
            )
        counts["queued"] = len(queue_ids)
        await bump_stats(
            s,
            campaign_id,
            raw_discovered=counts["raw"],
            duplicates=counts["duplicates"],
            excluded_previous=counts["excluded"],
            suppressed=counts["suppressed"],
            rejected=counts["rejected"],
            unique_new_companies=counts["queued"],
            in_flight=counts["queued"],
        )
    if counts["duplicates"]:
        await record_outcomes(source_key, duplicates=counts["duplicates"])
    return counts


def _raw_payload(cand: Any) -> dict[str, Any]:
    data = dict(cand.raw_data or {})
    data["_hints"] = {
        "phone": cand.phone,
        "employee_min": cand.employee_min,
        "employee_max": cand.employee_max,
        "registry_source": cand.registry_source,
        "registry_id": cand.registry_id,
        "status": cand.status,
        "people": cand.people,
        "emails": cand.emails,
    }
    return data


def _company_input(cand: Any, domain: str | None) -> registry.CompanyInput:
    loc = cand.location or {}
    status = None
    if cand.status == "closed":
        status = CompanyStatus.closed
    elif cand.status == "active":
        status = CompanyStatus.active
    return registry.CompanyInput(
        name=cand.name,
        website=cand.website,
        domain=domain,
        country=(loc.get("country") or None),
        region=loc.get("region"),
        city=loc.get("city"),
        postal_code=loc.get("postal_code"),
        address=loc.get("address"),
        latitude=_f(loc.get("lat")),
        longitude=_f(loc.get("lng")),
        category_raw=cand.category,
        employee_min=cand.employee_min,
        employee_max=cand.employee_max,
        phone=cand.phone,
        registry_source=cand.registry_source,
        registry_id=cand.registry_id,
        status=status,
    )


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _evidence(source_key: str, cand: Any) -> registry.Evidence:
    try:
        from scout.discovery.catalog import source_quality

        q = source_quality(source_key)
    except Exception:
        q = 0.7
    return registry.Evidence(
        source_type=SOURCE_EVIDENCE_TYPE.get(source_key, SourceType.directory),
        confidence=q,
        source_key=source_key,
        source_url=cand.source_url,
        evidence=f"{cand.name} — {cand.category or ''}".strip(" —")[:300],
        observed_at=cand.observed_at,
    )


def _prequalify(defn: CampaignDefinition, cand: Any) -> str | None:
    """Cheap, network-free prequalification from source data only."""
    loc = cand.location or {}
    cf = defn.company_filters
    country = (loc.get("country") or "").upper()[:2]
    if cf.countries and country and country not in cf.countries:
        return f"Country {country} not requested"
    if cand.status == "closed":
        return "Company closed"
    er = cf.employee_range
    if er and (cand.employee_min is not None or cand.employee_max is not None):
        lo = cand.employee_min if cand.employee_min is not None else 0
        hi = cand.employee_max if cand.employee_max is not None else 10**9
        if (er.max is not None and lo > er.max) or (er.min is not None and hi < er.min):
            return f"Company size {lo}–{hi if hi < 10**9 else '+'} outside {er.min or 0}–{er.max or '∞'}"
    if cf.exclude_keywords:
        hay = normalize_company_name(f"{cand.name} {cand.category or ''}")
        for kw in cf.exclude_keywords:
            if normalize_company_name(kw) in hay:
                return f"Excluded keyword '{kw}'"
    return None


# =============================================================================================
# campaign.tick — adaptive discovery + stop conditions + live progress
# =============================================================================================


@job_handler("campaign.tick", timeout_s=60)
async def tick(ctx: JobContext) -> dict[str, Any] | None:
    async with session_scope() as s:
        c = await s.get(Campaign, ctx.campaign_id)
        if c is None or c.status not in (CampaignStatus.running, CampaignStatus.planning):
            return {"stopped": True}
        st = await s.get(CampaignStats, c.id)
        assert st is not None
        defn = CampaignDefinition.model_validate(c.definition)
        qualified = st.qualified
        # ---- stop conditions (explicit reasons) ----
        if qualified >= c.target_qualified_count:
            await stop_campaign(
                s, c, CampaignStatus.completed, f"Target reached: {qualified:,} qualified leads"
            )
            return {"completed": True}
        if c.max_cost_usd is not None and st.cost_usd >= c.max_cost_usd:
            await stop_campaign(
                s,
                c,
                CampaignStatus.budget_reached,
                f"Campaign budget reached (${float(c.max_cost_usd):.2f}) — {qualified:,}/{c.target_qualified_count:,} qualified",
            )
            return {"budget": True}
        budget = await workspace_budget(c.workspace_id)
        if budget.exceeded:
            await stop_campaign(
                s,
                c,
                CampaignStatus.budget_reached,
                f"Workspace monthly budget reached (${budget.monthly_budget_usd:.0f}) — {qualified:,}/{c.target_qualified_count:,} qualified",
            )
            return {"budget": True}
        if c.started_at and datetime.now(UTC) - c.started_at > timedelta(hours=c.max_runtime_hours):
            await stop_campaign(
                s,
                c,
                CampaignStatus.limit_reached,
                f"Safety limit: max runtime {c.max_runtime_hours}h — {qualified:,}/{c.target_qualified_count:,} qualified",
            )
            return {"limit": True}
        sources = (await s.scalars(sa.select(CampaignSource).where(CampaignSource.campaign_id == c.id))).all()
        active = [
            x for x in sources if x.status in (CampaignSourceStatus.active, CampaignSourceStatus.pending)
        ]
        in_flight_jobs = (
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(Job)
                .where(
                    Job.campaign_id == c.id,
                    Job.type.in_(["company.process", "campaign.discover", "campaign.plan"]),
                    Job.status.in_(
                        [JobStatus.pending, JobStatus.claimed, JobStatus.running, JobStatus.retrying]
                    ),
                )
            )
            or 0
        )
        pending_candidates = (
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(CompanyDiscoveryEvent)
                .where(
                    CompanyDiscoveryEvent.campaign_id == c.id,
                    CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
                )
            )
            or 0
        )
        st.in_flight = int(pending_candidates)
        pending_email = (
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(EmailVerificationRequest)
                .where(
                    EmailVerificationRequest.campaign_id == c.id,
                    EmailVerificationRequest.status.in_(
                        [
                            VerificationRequestStatus.pending,
                            VerificationRequestStatus.processing,
                            VerificationRequestStatus.retry,
                        ]
                    ),
                )
            )
            or 0
        )
        if not active and in_flight_jobs == 0 and pending_email == 0:
            reason = (
                f"All eligible sources exhausted — {qualified:,}/{c.target_qualified_count:,} qualified. "
                "Broaden criteria (location, size, conditions) to find more."
            )
            await stop_campaign(s, c, CampaignStatus.exhausted, reason)
            return {"exhausted": True}
        # ---- adaptive: make sure every active source has a discover job ----
        for x in active:
            await queue.enqueue(
                s,
                workspace_id=c.workspace_id,
                campaign_id=c.id,
                type="campaign.discover",
                priority=10 + x.priority,
                payload={"source_key": x.source_key},
                dedupe_key=f"discover:{c.id}:{x.source_key}",
            )
        in_flight, _desired, yield_ = await _pipeline_need(s, c)
        from scout.pipeline.campaigns import rolling_rate

        rate = await rolling_rate(s, c.id)
        remaining = c.target_qualified_count - qualified
        await emit(
            c.workspace_id,
            "campaign.progress",
            {
                "campaign_id": str(c.id),
                "qualified": qualified,
                "target": c.target_qualified_count,
                "raw": st.raw_discovered,
                "evaluated": st.companies_evaluated,
                "people": st.people_found,
                "emails": st.emails_found,
                "safe": st.emails_safe,
                "excluded": st.excluded_previous,
                "duplicates": st.duplicates,
                "in_flight": in_flight,
                "yield": round(yield_, 3),
                "estimated_raw_needed": int(remaining / max(yield_, 0.01)),
                "rate_per_minute": round(rate, 2) if rate else None,
                "eta_minutes": round(remaining / rate) if rate else None,
                "cost_usd": float(st.cost_usd),
                "status": c.status.value,
                "exhausting": len(active) < len(sources) and len(active) <= 1,
                "requires_email": defn.requires_email,
            },
            campaign_id=c.id,
            session=s,
        )
    ctx.later(TICK_SECONDS)
    return None
