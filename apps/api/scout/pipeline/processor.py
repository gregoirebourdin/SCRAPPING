"""Per-company pipeline (`company.process`): domain-centric, checkpointed stages, filter early (spec §50–51, §99).

resolve_website → crawl (cache-first) → website conditions → company qualification → decision makers →
person registry/exclusion/reservation → email finder → verification → scoring → quality gate → deliver.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import (
    CampaignMode,
    CampaignStatus,
    CandidateOutcome,
    EmailStatus,
    EntityType,
    ExposureType,
    ReservationStatus,
    SourceType,
    VerificationRequestStatus,
    WebsiteStatus,
)
from scout.db.models import (
    Campaign,
    CampaignReservation,
    CampaignSource,
    CampaignStats,
    Company,
    CompanyDiscoveryEvent,
    Email,
    EmailVerificationRequest,
    Person,
    PersonDiscoveryEvent,
    QualificationScore,
    Signal,
    WebsitePage,
)
from scout.jobs.events import emit, emit_candidate_done, emit_candidate_stage
from scout.jobs.registry import JobContext, job_handler
from scout.learning.people import PeopleLearning
from scout.pipeline.exclusions import load_rules
from scout.pipeline.fit import industry_fit
from scout.pipeline.jobs import bump_stats
from scout.pipeline.scoring import ConditionOutcome, ScoringInput, location_fit, score, size_fit
from scout.schemas.campaign import (
    CampaignDefinition,
    KeywordCondition,
    RegexCondition,
    SemanticCondition,
    TechnologyCondition,
)
from scout.services import lists as lists_svc
from scout.services import registry
from scout.services.exclusion import excluded_entities, suppressed_companies, suppressed_people
from scout.services.usage import allow_expensive

if TYPE_CHECKING:
    from scout.extract.company_info import Fact

log = structlog.get_logger("processor")

RESERVATION_TTL = timedelta(minutes=30)
PERSON_SOURCE_LABEL = {
    "registry": "official registry",
    "jsonld": "website structured data",
    "team_card": "team page",
    "legal_notice": "legal notice",
    "text_pattern": "website",
    "ai": "AI extraction (verified on page)",
    "grounded": "web research",
    "mailto": "published email",
    "search_result": "web search result",
}


@dataclass
class Rejection(Exception):
    reason: str
    stage: str
    outcome: CandidateOutcome = CandidateOutcome.rejected


@dataclass
class PersonPick:
    candidate: Any  # scout.extract.types.PersonCandidate
    title_info: Any | None
    title_score: float
    rank: float


@dataclass
class Ctx:
    workspace_id: uuid.UUID
    campaign_id: uuid.UUID
    event_id: uuid.UUID
    company_id: uuid.UUID
    defn: CampaignDefinition
    target_list_id: uuid.UUID | None
    source_key: str
    stage_data: dict[str, Any] = field(default_factory=dict)
    pages: list[WebsitePage] = field(default_factory=list)


def _condition_label(c: Any) -> str:
    if c.label:
        return c.label
    if isinstance(c, KeywordCondition):
        return ("Mentions " + (" or " if c.type == "keyword_any" else " and ").join(c.terms))[:80]
    if isinstance(c, SemanticCondition):
        return c.concept[:80]
    if isinstance(c, TechnologyCondition):
        return "Uses " + " / ".join(c.technologies)
    if isinstance(c, RegexCondition):
        return f"Matches {c.pattern}"
    return "Condition"


async def _set_stage(ctx: Ctx, stage: str) -> None:
    async with session_scope() as s:
        res = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(CompanyDiscoveryEvent.id == ctx.event_id)
            .values(stage=stage, stage_data=ctx.stage_data)
            .returning(CompanyDiscoveryEvent.name, CompanyDiscoveryEvent.domain)
        )
        row = res.first()
        await emit_candidate_stage(  # live run: skeleton rows + "what's happening now"
            s,
            ctx.workspace_id,
            ctx.campaign_id,
            ctx.event_id,
            stage,
            name=row[0] if row else None,
            domain=row[1] if row else None,
        )


async def _count_once(ctx: Ctx, flag: str, **deltas: int) -> None:
    """Funnel counters move once per candidate: the flag is checkpointed in the same transaction as the
    increment, so a resumed / retried job never counts twice."""
    if ctx.stage_data.get(flag):
        return
    ctx.stage_data[flag] = True
    async with session_scope() as s:
        await bump_stats(s, ctx.campaign_id, **deltas)
        await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(CompanyDiscoveryEvent.id == ctx.event_id)
            .values(stage_data=ctx.stage_data)
        )


async def _finish(ctx: Ctx, outcome: CandidateOutcome, reason: str | None, stage: str) -> bool:
    """Transition the candidate out of `pending` exactly once (idempotent counters)."""
    async with session_scope() as s:
        res = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.id == ctx.event_id,
                CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
            )
            .values(
                outcome=outcome,
                reason=reason,
                stage=stage,
                stage_data=ctx.stage_data,
                processed_at=sa.func.now(),
            )
            .returning(CompanyDiscoveryEvent.id)
        )
        changed = res.scalar_one_or_none() is not None
        if changed:
            await bump_stats(
                s,
                ctx.campaign_id,
                in_flight=-1,
                rejected=1 if outcome == CandidateOutcome.rejected else 0,
                excluded_previous=1 if outcome == CandidateOutcome.excluded_previous else 0,
                suppressed=1 if outcome == CandidateOutcome.suppressed else 0,
                errors=1 if outcome == CandidateOutcome.error else 0,
            )
            await emit_candidate_done(
                s, ctx.workspace_id, ctx.campaign_id, ctx.event_id, outcome.value, reason=reason, stage=stage
            )
    if changed and outcome == CandidateOutcome.rejected and stage == "company_qualification":
        from scout.learning.feedback import discovery_outcome

        await discovery_outcome(ctx.source_key, wrong=1)  # off-ICP candidate (Empirical Source Scoring)
    return changed


@job_handler("company.process", timeout_s=900)
async def process_company(job: JobContext) -> dict[str, Any] | None:
    event_id = uuid.UUID(job.payload["event_id"])
    async with session_scope() as s:
        ev = await s.get(CompanyDiscoveryEvent, event_id)
        if ev is None or ev.outcome != CandidateOutcome.pending or ev.company_id is None:
            return {"skipped": True}
        c = await s.get(Campaign, ev.campaign_id)
        assert c is not None
        ctx = Ctx(
            workspace_id=ev.workspace_id,
            campaign_id=c.id,
            event_id=ev.id,
            company_id=ev.company_id,
            defn=CampaignDefinition.model_validate(c.definition),
            target_list_id=c.target_list_id,
            source_key=ev.source_key,
            stage_data=dict(ev.stage_data or {}),
        )
        hints = (ev.raw_data or {}).get("_hints", {})
    try:
        await job.checkpoint()
        await _stage_website(ctx, job)
        await job.checkpoint()
        await _stage_crawl(ctx)
        await job.checkpoint()
        conditions = await _stage_conditions(ctx)
        fit = await _stage_company_fit(ctx, conditions)
        await job.checkpoint()
        if ctx.defn.mode == CampaignMode.companies:
            company_delivered = await _deliver_company(ctx, conditions, fit)
            return {"delivered": int(company_delivered)}
        picks = await _stage_people(ctx, hints)
        await job.checkpoint()
        delivered, last_reason, pending_email = await _stage_persons(ctx, job, picks, conditions, fit)
        if delivered == 0 and pending_email == 0:
            await _finish(
                ctx, CandidateOutcome.rejected, last_reason or "No qualified decision maker", "people"
            )
        return {"delivered": delivered, "pending_email": pending_email}
    except Rejection as rej:
        await _finish(ctx, rej.outcome, rej.reason, rej.stage)
        return {rej.outcome.value: rej.reason}


# =============================================================================================
# Stages
# =============================================================================================


async def _company(ctx: Ctx) -> Company:
    async with session_scope() as s:
        comp = await s.get(Company, ctx.company_id)
        assert comp is not None
        s.expunge(comp)
        return comp


async def _stage_website(ctx: Ctx, job: JobContext) -> None:
    if ctx.stage_data.get("website_done"):
        return
    comp = await _company(ctx)
    needs_site = (
        bool(ctx.defn.website_conditions)
        or ctx.defn.mode == CampaignMode.people
        or "website" in ctx.defn.required_fields
    )
    if not comp.website_url and needs_site:
        from scout.crawl.resolve import resolve_website

        resolved = await resolve_website(
            comp.name,
            city=comp.city,
            postal_code=comp.postal_code,
            country=comp.country,
            registry_id=comp.registry_id,
            phone=comp.phone,
        )
        if resolved is None:
            async with session_scope() as s:
                await s.execute(
                    sa.update(Company).where(Company.id == comp.id).values(website_status=WebsiteStatus.none)
                )
            raise Rejection("Website not found", "resolve_website")
        async with session_scope() as s:
            company = await s.get(Company, comp.id)
            assert company is not None
            taken = await s.scalar(
                sa.select(Company.id).where(
                    Company.workspace_id == ctx.workspace_id,
                    Company.normalized_domain == resolved.domain,
                    Company.id != comp.id,
                )
            )
            if taken is not None:
                raise Rejection("Website belongs to a company already in the registry", "resolve_website")
            company.domain = company.normalized_domain = resolved.domain
            await registry.observe_company(
                s,
                ctx.workspace_id,
                company,
                {"website_url": resolved.url},
                registry.Evidence(
                    source_type=SourceType.website,
                    confidence=resolved.confidence,
                    source_key="website",
                    source_url=resolved.url,
                    evidence=resolved.evidence,
                ),
                campaign_id=ctx.campaign_id,
            )
    ctx.stage_data["website_done"] = True
    await _set_stage(ctx, "website")


async def _check_company_suppressed(ctx: Ctx) -> None:
    """Suppression always wins — re-checked here because the domain may only be known after website resolution
    and an entry may have been added while the candidate was queued."""
    async with session_scope() as s:
        comp = await s.get(Company, ctx.company_id)
        assert comp is not None
        ids, domains = await suppressed_companies(
            s,
            ctx.workspace_id,
            company_ids=[comp.id],
            domains=[comp.normalized_domain] if comp.normalized_domain else [],
        )
    if ids or domains:
        raise Rejection(
            "Suppressed company" if ids else "Suppressed domain", "suppression", CandidateOutcome.suppressed
        )


async def _stage_crawl(ctx: Ctx) -> None:
    from scout.crawl.cache import ensure_crawled

    await _check_company_suppressed(ctx)
    comp = await _company(ctx)
    if not comp.website_url:
        ctx.pages = []
        if ctx.defn.website_conditions or ctx.defn.mode == CampaignMode.people:
            raise Rejection("Website not found", "crawl")
        return
    pages = await ensure_crawled(ctx.workspace_id, comp.id, max_age_days=30)
    ctx.pages = pages
    comp = await _company(ctx)
    if not pages:
        label = {
            WebsiteStatus.unreachable: "Website unreachable",
            WebsiteStatus.parked: "Parked domain",
            WebsiteStatus.blocked: "Website blocks automated access",
        }.get(comp.website_status, "Website could not be crawled")
        if ctx.defn.website_conditions or ctx.defn.mode == CampaignMode.people:
            raise Rejection(label, "crawl")
    await _count_once(ctx, "crawl_counted", companies_evaluated=1)
    # deterministic company facts from the crawl (once per crawl)
    if pages and (
        comp.last_enriched_at is None
        or (comp.last_crawled_at and comp.last_enriched_at < comp.last_crawled_at)
    ):
        from scout.extract.company_info import extract_company_facts

        facts = extract_company_facts(pages)
        async with session_scope() as s:
            company = await s.get(Company, comp.id)
            assert company is not None
            await record_company_facts(
                s, ctx.workspace_id, company, facts, pages, campaign_id=ctx.campaign_id
            )
            company.last_enriched_at = datetime.now(UTC)
    await _set_stage(ctx, "crawl")


# Extractor fact names → canonical company fields (facts without a canonical column are not stored).
FACT_FIELDS = {"siren": "registry_id", "registry_id": "registry_id", "social_linkedin": "linkedin_url"}


async def record_company_facts(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    company: Company,
    facts: Sequence[Fact],
    pages: Sequence[WebsitePage] = (),
    *,
    campaign_id: uuid.UUID | None = None,
) -> int:
    """Store deterministic website facts as field observations (source URL, cached page, verbatim evidence,
    confidence) and let the field resolver update the canonical columns. Returns the observations recorded."""
    page_ids = {p.url: p.id for p in pages}
    n = 0
    for f in facts:
        fname = FACT_FIELDS.get(f.field_name, f.field_name)
        if fname not in registry.COMPANY_FIELDS:
            continue
        await registry.observe_company(
            s,
            workspace_id,
            company,
            {fname: f.value},
            registry.Evidence(
                source_type=SourceType.website,
                confidence=f.confidence,
                source_key="website",
                source_url=f.source_url,
                evidence=f.evidence,
                page_id=page_ids.get(f.source_url) if f.source_url else None,
            ),
            campaign_id=campaign_id,
        )
        n += 1
    return n


async def _stage_conditions(ctx: Ctx) -> list[ConditionOutcome]:
    """Website conditions run right after the crawl — before any people/email spend (spec §51)."""
    if not ctx.defn.website_conditions:
        return []
    from scout.enrich.conditions import evaluate_condition

    comp = await _company(ctx)
    out: list[ConditionOutcome] = []
    # deterministic conditions first: a failing keyword condition avoids any AI call
    ordered = sorted(ctx.defn.website_conditions, key=lambda c: 1 if isinstance(c, SemanticCondition) else 0)
    for cond in ordered:
        res = await evaluate_condition(ctx.workspace_id, comp, cond, ctx.pages)
        label = _condition_label(cond)
        outcome = ConditionOutcome(
            label=label,
            passed=res.passed,
            confidence=res.confidence,
            evidence=res.evidence,
            source_url=res.source_url,
            required=cond.required,
        )
        if isinstance(cond, SemanticCondition) and res.passed and res.confidence < cond.min_confidence:
            outcome.passed = None
        out.append(outcome)
        if cond.required and outcome.passed is not True:
            ctx.stage_data["conditions"] = [o.__dict__ for o in out]
            raise Rejection(
                f"{label}: "
                + ("not found on website" if outcome.passed is False else "insufficient evidence"),
                "website_conditions",
            )
    ctx.stage_data["conditions"] = [o.__dict__ for o in out]
    await _set_stage(ctx, "website_conditions")
    return out


@dataclass
class FitResult:
    industry: float | None
    size: float | None
    location: float | None
    industry_label: str | None
    company_fit_score: float | None
    company_confidence: float | None


async def _stage_company_fit(ctx: Ctx, conditions: list[ConditionOutcome]) -> FitResult:
    comp = await _company(ctx)
    text = "\n".join((p.content_text or "")[:6000] for p in ctx.pages[:6]) if ctx.pages else None
    im = industry_fit(
        ctx.defn.company_filters.industries,
        category=comp.category_raw,
        industry=comp.industry,
        name=comp.name,
        description=comp.description,
        page_text=text,
    )
    sf = size_fit(ctx.defn, comp.employee_min, comp.employee_max)
    lf = location_fit(ctx.defn, comp.country, comp.city, comp.region, comp.postal_code)
    from scout.pipeline.scoring import company_fit as _cf

    probe = ScoringInput(
        defn=ctx.defn,
        industry_fit=im.score if im else None,
        size_fit=sf,
        location_fit=lf,
        company_confidence=comp.company_confidence,
        conditions=conditions,
    )
    cf = _cf(probe)
    conf_parts = [
        0.95 if comp.registry_id else None,
        0.9 if comp.website_status == WebsiteStatus.ok else None,
        comp.employee_confidence,
    ]
    vals = [v for v in conf_parts if v is not None]
    company_conf = round(sum(vals) / len(vals), 3) if vals else 0.6
    async with session_scope() as s:
        await s.execute(
            sa.update(Company)
            .where(Company.id == comp.id)
            .values(
                company_confidence=company_conf,
                industry=sa.func.coalesce(Company.industry, im.label if im and im.score >= 0.6 else None),
            )
        )
    fit = FitResult(im.score if im else None, sf, lf, im.label if im else None, cf, company_conf)
    if sf is not None and sf == 0.0:
        raise Rejection("Company size outside requested range", "company_qualification")
    if lf is not None and lf == 0.0:
        raise Rejection("Location outside requested area", "company_qualification")
    if cf is not None and cf * 100 < ctx.defn.minimum_company_fit:
        raise Rejection(f"Company fit too low ({round(cf * 100)})", "company_qualification")
    await _count_once(ctx, "matched_counted", companies_matched=1)
    await _set_stage(ctx, "company_qualification")
    return fit


async def _stage_people(ctx: Ctx, hints: dict[str, Any]) -> list[PersonPick]:
    from scout.extract.names import is_plausible_person_name, split_name
    from scout.extract.people import extract_people
    from scout.extract.titles import normalize_title, title_match_score
    from scout.extract.types import PersonCandidate

    comp = await _company(ctx)
    pf = ctx.defn.people_filters
    candidates: list[PersonCandidate] = []
    for p in hints.get("people") or []:
        name = (p.get("full_name") or "").strip()
        if not name or not is_plausible_person_name(name):
            continue
        first, last = split_name(name)
        candidates.append(
            PersonCandidate(
                full_name=name,
                first_name=first,
                last_name=last,
                title=p.get("title"),
                source_url=p.get("source_url"),
                source_type="registry",
                method="registry",
                evidence=f"{name} — {p.get('title') or 'director'} (official registry)",
                confidence=0.95,
            )
        )
    if ctx.pages:
        candidates.extend(extract_people(ctx.pages, company_name=comp.name, domain=comp.normalized_domain))
    # Empirical Source Scoring: learned per-source confidence + attempts per people source.
    learn = await PeopleLearning.start()
    learn.website(ctx.pages, candidates, registry_hint="people" in hints)
    picks = _rank(learn.adjust(candidates), pf, normalize_title, title_match_score)
    if not picks and ctx.pages:
        from scout.extract.ai_people import ai_extract_people

        t0 = time.monotonic()
        try:
            ai_found = await ai_extract_people(ctx.pages, company_name=comp.name)
        except Exception as exc:
            log.info("people.ai_failed", error=str(exc))
            ai_found = []
        learn.step("ai_extraction", ai_found, t0)
        picks = _rank(learn.adjust(ai_found), pf, normalize_title, title_match_score)
    if not picks:  # free web search (SearXNG) before any Gemini grounding; no-op when not configured
        from scout.search.people import serp_people

        found = await serp_people(
            comp.name, domain=comp.normalized_domain, country=comp.country, titles=pf.titles
        )
        picks = _rank(learn.adjust(found), pf, normalize_title, title_match_score)
    if not picks and await allow_expensive():
        t0 = time.monotonic()
        grounded = await _grounded_people(ctx, comp)
        learn.step("gemini_grounded_result", grounded, t0)
        picks = _rank(learn.adjust(grounded), pf, normalize_title, title_match_score)
    await learn.flush()
    if not picks:
        raise Rejection("No decision maker found", "people")
    await _count_once(ctx, "people_counted", people_found=1)
    await _set_stage(ctx, "people")
    return picks


def _rank(candidates: list[Any], pf: Any, normalize_title: Any, title_match_score: Any) -> list[PersonPick]:
    from scout.extract.names import has_known_first_name

    out: dict[str, PersonPick] = {}
    wants_roles = bool(pf.titles or pf.role_families or pf.seniorities)
    for cand in candidates:
        if cand.source_type != "registry" and not has_known_first_name(cand.full_name):
            continue  # only official registry names may lack a known first name: no headings / product names as leads
        info = normalize_title(cand.title) if cand.title else None
        if wants_roles:
            ts = (
                title_match_score(
                    info,
                    titles=pf.titles,
                    role_families=[r.value for r in pf.role_families],
                    seniorities=[x.value for x in pf.seniorities],
                )
                if info
                else 0.0
            )
            if ts < 0.5:
                continue
        else:
            ts = 0.7
        rank = ts * 0.5 + cand.confidence * 0.35 + ((info.decision_power if info else 40) / 100.0) * 0.15
        key = (
            registry.normalize_person_name(cand.full_name)
            if hasattr(registry, "normalize_person_name")
            else cand.full_name.lower()
        )
        prev = out.get(key)
        if prev is None or rank > prev.rank:
            out[key] = PersonPick(cand, info, ts, rank)
    return sorted(out.values(), key=lambda p: p.rank, reverse=True)


async def _grounded_people(ctx: Ctx, comp: Company) -> list[Any]:
    """Grounded research fallback: names only accepted with grounding sources (never invented)."""
    from pydantic import BaseModel

    from scout.ai.factory import get_ai
    from scout.extract.names import is_plausible_person_name, split_name
    from scout.extract.types import PersonCandidate

    ai = get_ai()
    if not ai.available:
        return []

    class _P(BaseModel):
        full_name: str
        title: str
        source_url: str | None = None

    class _Out(BaseModel):
        people: list[_P] = []

    titles = ", ".join(ctx.defn.people_filters.titles) or "founder or CEO"
    try:
        res = await ai.grounded_search(
            query=f"Who is the {titles} of {comp.name} ({comp.normalized_domain or comp.city or ''})?",
            instructions="Use web search. Only list people explicitly named in sources as working at this exact company. "
            "If unsure, return an empty list.",
            schema=_Out,
        )
    except Exception:
        return []
    if not res.value or not res.sources:
        return []
    out = []
    domains = {s.domain or "" for s in res.sources}
    for p in res.value.people[:3]:
        if not is_plausible_person_name(p.full_name):
            continue
        supported = any(p.full_name.split()[-1].lower() in sp.text.lower() for sp in res.supports)
        if not supported:
            continue
        first, last = split_name(p.full_name)
        out.append(
            PersonCandidate(
                full_name=p.full_name,
                first_name=first,
                last_name=last,
                title=p.title,
                source_url=p.source_url or (res.sources[0].uri if res.sources else None),
                source_type="grounded_search",
                method="grounded",
                evidence=f"Web research ({', '.join(sorted(d for d in domains if d))[:120]})",
                confidence=0.7,
            )
        )
    return out


# =============================================================================================
# Persons → email → score → gate → deliver
# =============================================================================================


async def _stage_persons(
    ctx: Ctx, job: JobContext, picks: list[PersonPick], conditions: list[ConditionOutcome], fit: FitResult
) -> tuple[int, str | None, int]:
    """→ (delivered, last rejection reason, persons waiting for deep email verification)."""
    from scout.email.engine import resolve_for_person

    defn = ctx.defn
    delivered = 0
    pending_email = 0
    last_reason: str | None = None
    rules = None
    comp = await _company(ctx)
    for pick in picks[: max(3, defn.people_filters.max_people_per_company * 3)]:
        if delivered >= defn.people_filters.max_people_per_company:
            break
        await job.checkpoint()
        cand = pick.candidate
        st = {
            "registry": SourceType.registry,
            "website": SourceType.website,
            "ai_extraction": SourceType.ai_extraction,
            "grounded_search": SourceType.grounded_search,
            "public_profile": SourceType.public_profile,
            "search_snippet": SourceType.search_snippet,
        }.get(cand.source_type, SourceType.website)
        async with session_scope() as s:
            person, _created = await registry.upsert_person(
                s,
                ctx.workspace_id,
                company_id=comp.id,
                full_name=cand.full_name,
                first_name=cand.first_name,
                last_name=cand.last_name,
                job_title=cand.title,
                title_info=pick.title_info,
                profile_url=cand.profile_url,
                email=cand.email,
                evidence=registry.Evidence(
                    source_type=st,
                    confidence=cand.confidence,
                    source_key=_person_source_key(ctx, cand),
                    source_url=cand.source_url,
                    evidence=cand.evidence,
                ),
                campaign_id=ctx.campaign_id,
            )
            person_id = person.id
            if rules is None:
                rules = await load_rules(s, ctx.campaign_id)
            sup, _ = await suppressed_people(s, ctx.workspace_id, person_ids=[person_id])
            if sup:
                await _person_event(
                    s, ctx, person_id, CandidateOutcome.suppressed, "Suppressed", cand.source_url
                )
                await bump_stats(s, ctx.campaign_id, suppressed=1)
                last_reason = "Decision maker is suppressed"
                continue
            excl = await excluded_entities(
                s,
                ctx.workspace_id,
                EntityType.person,
                [person_id],
                rules,
                current_campaign_id=ctx.campaign_id,
            )
            if excl:
                await _person_event(
                    s, ctx, person_id, CandidateOutcome.excluded_previous, excl[person_id], cand.source_url
                )
                await bump_stats(s, ctx.campaign_id, excluded_previous=1)
                last_reason = "Decision maker previously seen (excluded)"
                continue
            # atomic reservation: two concurrent campaigns cannot both deliver the same "new" person
            key = registry.person_key(comp.id, person.full_name, person.public_profile_url)
            await _expire_stale_reservation(s, ctx.workspace_id, EntityType.person, key)
            res = await s.execute(
                pg_insert(CampaignReservation)
                .values(
                    workspace_id=ctx.workspace_id,
                    campaign_id=ctx.campaign_id,
                    entity_type=EntityType.person,
                    entity_key=key,
                    company_id=comp.id,
                    person_id=person_id,
                    status=ReservationStatus.reserved,
                    expires_at=datetime.now(UTC) + RESERVATION_TTL,
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "entity_type", "entity_key"],
                    index_where=sa.text("status = 'reserved'"),
                )
                .returning(CampaignReservation.id)
            )
            reservation_id = res.scalar_one_or_none()
            if reservation_id is None:
                holder = await s.scalar(
                    sa.select(CampaignReservation.campaign_id).where(
                        CampaignReservation.workspace_id == ctx.workspace_id,
                        CampaignReservation.entity_key == key,
                        CampaignReservation.status == ReservationStatus.reserved,
                    )
                )
                if holder != ctx.campaign_id:
                    await _person_event(
                        s,
                        ctx,
                        person_id,
                        CandidateOutcome.reserved_elsewhere,
                        "Reserved by another running campaign",
                        cand.source_url,
                    )
                    await bump_stats(s, ctx.campaign_id, reserved_elsewhere=1)
                    last_reason = "Decision maker reserved by another campaign"
                    continue
                reservation_id = await s.scalar(
                    sa.select(CampaignReservation.id).where(
                        CampaignReservation.campaign_id == ctx.campaign_id,
                        CampaignReservation.entity_key == key,
                        CampaignReservation.status == ReservationStatus.reserved,
                    )
                )
        # ---- email: fast path from the domain profile; ambiguous cases go to the deep path ----
        email_addr, email_status, email_conf = await _existing_email(person_id)
        if defn.requires_email or defn.mode == CampaignMode.people:
            if email_status not in defn.accepted_email_statuses:
                deferred = _deferred_context(ctx, pick, conditions, fit, reservation_id)
                try:
                    resolution = await resolve_for_person(
                        ctx.workspace_id, person_id, campaign_id=ctx.campaign_id, deliver_context=deferred
                    )
                except Exception as exc:
                    log.warning("email.resolve_failed", error=str(exc), person_id=str(person_id))
                    resolution = None
                email_addr, email_status, email_conf = await _existing_email(person_id)
                if (
                    resolution is not None
                    and resolution.deep_requested
                    and email_status not in defn.accepted_email_statuses
                ):
                    # Keep the reservation: delivery resumes when the per-domain SMTP batch concludes.
                    pending_email += 1
                    async with session_scope() as s:
                        await _person_event(
                            s,
                            ctx,
                            person_id,
                            CandidateOutcome.pending,
                            "Email verification in progress",
                            cand.source_url,
                        )
                    last_reason = "Email verification in progress"
                    continue
                if resolution is not None and not email_addr:
                    last_reason = resolution.reason or "No professional email found"
            await _count_once(
                ctx,
                f"email_counted:{person_id}",
                emails_found=1 if email_addr else 0,
                emails_safe=1 if email_status == EmailStatus.SAFE else 0,
            )
        ok, reason = await _finish_person(
            ctx,
            comp,
            person_id,
            reservation_id,
            _PersonInputs(
                title_score=pick.title_score,
                method=str(cand.method),
                evidence_quality=float(cand.confidence),
                source_url=cand.source_url,
            ),
            conditions,
            fit,
            email_addr,
            email_status,
            email_conf,
        )
        if ok:
            delivered += 1
        else:
            last_reason = reason
    return delivered, last_reason, pending_email


@dataclass
class _PersonInputs:
    title_score: float
    method: str
    evidence_quality: float
    source_url: str | None


async def _finish_person(
    ctx: Ctx,
    comp: Company,
    person_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
    pi: _PersonInputs,
    conditions: list[ConditionOutcome],
    fit: FitResult,
    email_addr: str | None,
    email_status: EmailStatus | None,
    email_conf: float | None,
) -> tuple[bool, str | None]:
    """Suppression → score → quality gate → deliver. Shared by the inline path and deferred delivery."""
    defn = ctx.defn
    # ---- suppression of the address (or its domain) always wins (spec §87) ----
    if email_addr:
        async with session_scope() as s:
            why = await _email_suppressed(s, ctx.workspace_id, email_addr)
            if why:
                await _person_event(s, ctx, person_id, CandidateOutcome.suppressed, why, pi.source_url)
                await bump_stats(s, ctx.campaign_id, suppressed=1)
                if reservation_id:
                    await s.execute(
                        sa.update(CampaignReservation)
                        .where(CampaignReservation.id == reservation_id)
                        .values(status=ReservationStatus.released)
                    )
        if why:
            return False, f"Decision maker suppressed ({why.lower()})"
    # ---- score + gate ----
    async with session_scope() as s:
        person = await s.get(Person, person_id)
        assert person is not None
        signals = [
            {"type": sg.type.value, "confidence": sg.confidence}
            for sg in (await s.scalars(sa.select(Signal).where(Signal.company_id == comp.id))).all()
        ]
        n_sources = await s.scalar(
            sa.text(
                "SELECT count(DISTINCT coalesce(source_key, source_type)) FROM (SELECT source_key, source_type FROM "
                "company_field_observations WHERE company_id = :c UNION ALL SELECT source_key, source_type FROM "
                "person_field_observations WHERE person_id = :p) x"
            ),
            {"c": comp.id, "p": person_id},
        )
    inp = ScoringInput(
        defn=defn,
        industry_fit=fit.industry,
        size_fit=fit.size,
        location_fit=fit.location,
        company_confidence=fit.company_confidence,
        conditions=conditions,
        person_identified=True,
        person_name=person.full_name,
        person_title=person.job_title,
        title_match=pi.title_score,
        decision_power=person.decision_power,
        person_confidence=person.identity_confidence,
        person_source=PERSON_SOURCE_LABEL.get(pi.method, pi.method),
        email=email_addr,
        email_status=email_status,
        email_confidence=email_conf,
        phone=bool(person.phone or comp.phone),
        profile_url=bool(person.public_profile_url),
        signals=signals,
        evidence_sources=int(n_sources or 0),
        evidence_quality=pi.evidence_quality,
        company_name=comp.name,
        industry_label=fit.industry_label,
    )
    result = score(inp)
    if result.qualified:
        if await _deliver_person(ctx, comp, person_id, reservation_id, result, email_status, email_addr):
            return True, None
        if ctx.stage_data.get("not_delivered") == "target_reached":
            return False, "Campaign target reached"
        return False, "Lost a race with another campaign"
    async with session_scope() as s:
        await _save_score(s, ctx, comp.id, person_id, result)
        await _person_event(s, ctx, person_id, CandidateOutcome.rejected, result.first_failure, pi.source_url)
        if reservation_id:
            await s.execute(
                sa.update(CampaignReservation)
                .where(CampaignReservation.id == reservation_id)
                .values(status=ReservationStatus.released)
            )
    return False, result.first_failure


def _deferred_context(
    ctx: Ctx,
    pick: PersonPick,
    conditions: list[ConditionOutcome],
    fit: FitResult,
    reservation_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Everything needed to score and deliver this person later, once the deep email path concludes."""
    cand = pick.candidate
    return {
        "v": 1,
        "company_id": str(ctx.company_id),
        "event_id": str(ctx.event_id),
        "source_key": ctx.source_key,
        "reservation_id": str(reservation_id) if reservation_id else None,
        "person": {
            "title_score": pick.title_score,
            "method": str(cand.method),
            "evidence_quality": float(cand.confidence),
            "source_url": cand.source_url,
        },
        "fit": asdict(fit),
        "conditions": [asdict(c) for c in conditions],
    }


async def deliver_after_email(request: Any, verdict: Any) -> None:
    """Deferred delivery: the deep email path concluded for a person a campaign was waiting on."""
    dc = request.deliver_context or {}
    if not dc or request.campaign_id is None:
        return
    async with session_scope() as s:
        c = await s.get(Campaign, request.campaign_id)
        if c is None:
            return
        running = c.status in (CampaignStatus.running, CampaignStatus.planning, CampaignStatus.paused)
        defn = CampaignDefinition.model_validate(c.definition)
        target_list_id = c.target_list_id
    reservation_id = uuid.UUID(dc["reservation_id"]) if dc.get("reservation_id") else None
    ctx = Ctx(
        workspace_id=request.workspace_id,
        campaign_id=request.campaign_id,
        event_id=uuid.UUID(dc["event_id"]),
        company_id=uuid.UUID(dc["company_id"]),
        defn=defn,
        target_list_id=target_list_id,
        source_key=dc.get("source_key") or "unknown",
    )
    if not running:
        if reservation_id:
            async with session_scope() as s:
                await s.execute(
                    sa.update(CampaignReservation)
                    .where(
                        CampaignReservation.id == reservation_id,
                        CampaignReservation.status == ReservationStatus.reserved,
                    )
                    .values(status=ReservationStatus.released)
                )
        return
    comp = await _company(ctx)
    email_addr, email_status, email_conf = await _existing_email(request.person_id)
    async with session_scope() as s:
        await bump_stats(
            s,
            ctx.campaign_id,
            emails_found=1 if email_addr else 0,
            emails_safe=1 if email_status == EmailStatus.SAFE else 0,
        )
        delivered_here = await s.scalar(
            sa.select(sa.func.count())
            .select_from(CampaignReservation)
            .where(
                CampaignReservation.campaign_id == ctx.campaign_id,
                CampaignReservation.company_id == ctx.company_id,
                CampaignReservation.status == ReservationStatus.qualified,
            )
        )
    p = dc.get("person") or {}
    pi = _PersonInputs(
        title_score=float(p.get("title_score") or 0.0),
        method=str(p.get("method") or "website"),
        evidence_quality=float(p.get("evidence_quality") or 0.5),
        source_url=p.get("source_url"),
    )
    reason: str | None
    if (delivered_here or 0) >= defn.people_filters.max_people_per_company:
        ok, reason = False, "Enough decision makers already delivered for this company"
        if reservation_id:
            async with session_scope() as s:
                await s.execute(
                    sa.update(CampaignReservation)
                    .where(CampaignReservation.id == reservation_id)
                    .values(status=ReservationStatus.released)
                )
    else:
        ok, reason = await _finish_person(
            ctx,
            comp,
            request.person_id,
            reservation_id,
            pi,
            _conditions_from(dc),
            _fit_from(dc),
            email_addr,
            email_status,
            email_conf,
        )
    if ok:
        return
    async with session_scope() as s:
        waiting = await s.scalar(
            sa.select(sa.func.count())
            .select_from(EmailVerificationRequest)
            .where(
                EmailVerificationRequest.campaign_id == ctx.campaign_id,
                EmailVerificationRequest.company_id == ctx.company_id,
                EmailVerificationRequest.id != request.id,
                EmailVerificationRequest.status.in_(
                    [
                        VerificationRequestStatus.pending,
                        VerificationRequestStatus.processing,
                        VerificationRequestStatus.retry,
                    ]
                ),
            )
        )
    if not waiting:
        await _finish(ctx, CandidateOutcome.rejected, reason or "No qualified decision maker", "email")


def _conditions_from(dc: dict[str, Any]) -> list[ConditionOutcome]:
    return [ConditionOutcome(**c) for c in dc.get("conditions") or []]


def _fit_from(dc: dict[str, Any]) -> FitResult:
    f = dc.get("fit") or {}
    return FitResult(
        industry=f.get("industry"),
        size=f.get("size"),
        location=f.get("location"),
        industry_label=f.get("industry_label"),
        company_fit_score=f.get("company_fit_score"),
        company_confidence=f.get("company_confidence"),
    )


def _register_email_hook() -> None:
    from scout.email.engine import on_email_resolved

    on_email_resolved(deliver_after_email)


_register_email_hook()


async def _expire_stale_reservation(
    s: AsyncSession, workspace_id: uuid.UUID, entity_type: EntityType, key: str
) -> None:
    """Holds expire (a paused or crashed campaign must not block an entity forever)."""
    await s.execute(
        sa.update(CampaignReservation)
        .where(
            CampaignReservation.workspace_id == workspace_id,
            CampaignReservation.entity_type == entity_type,
            CampaignReservation.entity_key == key,
            CampaignReservation.status == ReservationStatus.reserved,
            CampaignReservation.expires_at < sa.func.now(),
        )
        .values(status=ReservationStatus.expired)
    )


async def _email_suppressed(s: AsyncSession, workspace_id: uuid.UUID, address: str) -> str | None:
    """Reason when the address or its domain is on the suppression list, else None."""
    _, emails = await suppressed_people(s, workspace_id, emails=[address])
    if emails:
        return "Email address is suppressed"
    domain = address.rsplit("@", 1)[-1].lower()
    _, domains = await suppressed_companies(
        s, workspace_id, domains=[domain, registry.canonical_domain(None, domain) or domain]
    )
    if domains:
        return "Email domain is suppressed"
    return None


def _person_source_key(ctx: Ctx, cand: Any) -> str:
    if cand.source_type == "registry":
        return ctx.source_key if ctx.source_key in ("fr_registry", "fixture") else "registry"
    if cand.source_type == "grounded_search":
        return "gemini_search"
    if cand.source_type == "ai_extraction":
        return "ai_extraction"
    if cand.source_type == "search_snippet":
        return "web_search"
    return "website"


async def _existing_email(person_id: uuid.UUID) -> tuple[str | None, EmailStatus | None, float | None]:
    async with session_scope() as s:
        row = (
            await s.execute(
                sa.select(Email.address, Email.status, Email.overall_confidence, Email.last_checked_at)
                .where(Email.person_id == person_id)
                .order_by(Email.is_primary.desc(), Email.overall_confidence.desc().nullslast())
                .limit(1)
            )
        ).first()
    if row is None:
        return None, None, None
    addr, status, conf, checked = row
    if status == EmailStatus.SAFE and checked and datetime.now(UTC) - checked > timedelta(days=60):
        return addr, EmailStatus.UNKNOWN, conf  # stale SAFE → re-verify
    return addr, status, conf


async def _person_event(
    s: Any, ctx: Ctx, person_id: uuid.UUID, outcome: CandidateOutcome, reason: str | None, url: str | None
) -> None:
    s.add(
        PersonDiscoveryEvent(
            workspace_id=ctx.workspace_id,
            campaign_id=ctx.campaign_id,
            person_id=person_id,
            company_id=ctx.company_id,
            source_key=ctx.source_key,
            source_url=url,
            outcome=outcome,
            reason=(reason or "")[:500] or None,
        )
    )


async def _save_score(s: Any, ctx: Ctx, company_id: uuid.UUID, person_id: uuid.UUID | None, r: Any) -> None:
    values = dict(
        workspace_id=ctx.workspace_id,
        campaign_id=ctx.campaign_id,
        company_id=company_id,
        person_id=person_id,
        icp_score=r.icp_score,
        company_fit=r.company_fit,
        person_fit=r.person_fit,
        intent=r.intent,
        contactability=r.contactability,
        evidence_score=r.evidence,
        company_confidence=r.company_confidence,
        person_confidence=r.person_confidence,
        email_confidence=r.email_confidence,
        enrichment_confidence=r.enrichment_confidence,
        overall_confidence=r.overall_confidence,
        qualified=r.qualified,
        gate_results=r.gates,
        weights=r.weights,
        explanation=r.explanation,
    )
    stmt = (
        pg_insert(QualificationScore)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["campaign_id", "company_id", "person_id"],
            set_={
                k: v
                for k, v in values.items()
                if k not in ("workspace_id", "campaign_id", "company_id", "person_id")
            }
            | {"computed_at": sa.func.now()},
        )
    )
    await s.execute(stmt)


async def _target_reached(s: AsyncSession, campaign_id: uuid.UUID) -> bool:
    """Lock the campaign's stats row (serializes deliveries of one campaign) and compare with its target."""
    qualified = await s.scalar(
        sa.select(CampaignStats.qualified).where(CampaignStats.campaign_id == campaign_id).with_for_update()
    )
    target = await s.scalar(sa.select(Campaign.target_qualified_count).where(Campaign.id == campaign_id))
    return target is not None and target > 0 and qualified is not None and qualified >= target


async def _deliver_person(
    ctx: Ctx,
    comp: Company,
    person_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
    result: Any,
    email_status: EmailStatus | None,
    email_addr: str | None = None,
) -> bool:
    """Single transaction: re-check, reservation → qualified, exposures, list membership, stats, event.

    The campaign's stats row is locked first so concurrent deliveries never overshoot the target.
    """
    async with session_scope() as s:
        if await _target_reached(s, ctx.campaign_id):
            if reservation_id:
                await s.execute(
                    sa.update(CampaignReservation)
                    .where(CampaignReservation.id == reservation_id)
                    .values(status=ReservationStatus.released)
                )
            ctx.stage_data["not_delivered"] = "target_reached"
            return False
        rules = await load_rules(s, ctx.campaign_id)
        excl = await excluded_entities(
            s, ctx.workspace_id, EntityType.person, [person_id], rules, current_campaign_id=ctx.campaign_id
        )
        sup, _ = await suppressed_people(s, ctx.workspace_id, person_ids=[person_id])
        if excl or sup or (email_addr and await _email_suppressed(s, ctx.workspace_id, email_addr)):
            if reservation_id:
                await s.execute(
                    sa.update(CampaignReservation)
                    .where(CampaignReservation.id == reservation_id)
                    .values(status=ReservationStatus.released)
                )
            return False
        if reservation_id:
            await s.execute(
                sa.update(CampaignReservation)
                .where(CampaignReservation.id == reservation_id)
                .values(status=ReservationStatus.qualified)
            )
        await _save_score(s, ctx, comp.id, person_id, result)
        await registry.record_exposures(
            s, ctx.workspace_id, ExposureType.DISCOVERED, person_ids=[person_id], campaign_id=ctx.campaign_id
        )
        if ctx.target_list_id:
            await lists_svc.add_to_list(
                s,
                ctx.workspace_id,
                ctx.target_list_id,
                EntityType.person,
                [person_id],
                user_id=None,
                added_via="campaign",
                campaign_id=ctx.campaign_id,
            )
        await _person_event(s, ctx, person_id, CandidateOutcome.qualified, None, None)
        first = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.id == ctx.event_id,
                CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
            )
            .values(
                outcome=CandidateOutcome.qualified,
                stage="deliver",
                stage_data=ctx.stage_data,
                processed_at=sa.func.now(),
            )
            .returning(CompanyDiscoveryEvent.id)
        )
        await bump_stats(
            s,
            ctx.campaign_id,
            qualified=1,
            emails_accepted=1 if email_status else 0,
            in_flight=-1 if first.scalar_one_or_none() is not None else 0,
        )
        await s.execute(
            sa.update(CampaignSource)
            .where(CampaignSource.campaign_id == ctx.campaign_id, CampaignSource.source_key == ctx.source_key)
            .values(qualified_count=CampaignSource.qualified_count + 1)
        )
        await s.execute(
            sa.update(Campaign).where(Campaign.id == ctx.campaign_id).values(last_progress_at=sa.func.now())
        )
        p = await s.get(Person, person_id)
        await emit(
            ctx.workspace_id,
            "lead.qualified",
            {
                "campaign_id": str(ctx.campaign_id),
                "event_id": str(ctx.event_id),
                "list_id": str(ctx.target_list_id) if ctx.target_list_id else None,
                "person_id": str(person_id),
                "company_id": str(comp.id),
                "name": p.full_name if p else None,
                "company": comp.name,
                "icp_score": result.icp_score,
            },
            campaign_id=ctx.campaign_id,
            session=s,
        )
    try:
        from scout.discovery.health import record_outcomes

        await record_outcomes(ctx.source_key, qualified=1)
    except Exception as exc:  # source health is best-effort bookkeeping, never fails a delivered lead
        log.info("source_health.record_failed", source=ctx.source_key, error=str(exc))
    return True


async def _deliver_company(ctx: Ctx, conditions: list[ConditionOutcome], fit: FitResult) -> bool:
    comp = await _company(ctx)
    inp = ScoringInput(
        defn=ctx.defn,
        industry_fit=fit.industry,
        size_fit=fit.size,
        location_fit=fit.location,
        company_confidence=fit.company_confidence,
        conditions=conditions,
        phone=bool(comp.phone),
        evidence_sources=2 if comp.registry_id else 1,
        evidence_quality=0.9,
        company_name=comp.name,
        industry_label=fit.industry_label,
    )
    result = score(inp)
    async with session_scope() as s:
        await _save_score(s, ctx, comp.id, None, result)
    if not result.qualified:
        await _finish(ctx, CandidateOutcome.rejected, result.first_failure, "gate")
        return False
    # Phase 1 — atomic hold (committed): a concurrent campaign of the workspace cannot take this company now.
    key = f"d:{comp.normalized_domain}" if comp.normalized_domain else f"id:{comp.id}"
    async with session_scope() as s:
        await _expire_stale_reservation(s, ctx.workspace_id, EntityType.company, key)
        res = await s.execute(
            pg_insert(CampaignReservation)
            .values(
                workspace_id=ctx.workspace_id,
                campaign_id=ctx.campaign_id,
                entity_type=EntityType.company,
                entity_key=key,
                company_id=comp.id,
                status=ReservationStatus.reserved,
                expires_at=datetime.now(UTC) + RESERVATION_TTL,
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "entity_type", "entity_key"],
                index_where=sa.text("status = 'reserved'"),
            )
            .returning(CampaignReservation.id)
        )
        reservation_id = res.scalar_one_or_none()
        if reservation_id is None:
            holder = await s.scalar(
                sa.select(CampaignReservation.campaign_id).where(
                    CampaignReservation.workspace_id == ctx.workspace_id,
                    CampaignReservation.entity_type == EntityType.company,
                    CampaignReservation.entity_key == key,
                    CampaignReservation.status == ReservationStatus.reserved,
                )
            )
            if holder == ctx.campaign_id:  # our own hold from an interrupted attempt
                reservation_id = await s.scalar(
                    sa.select(CampaignReservation.id).where(
                        CampaignReservation.campaign_id == ctx.campaign_id,
                        CampaignReservation.entity_key == key,
                        CampaignReservation.status == ReservationStatus.reserved,
                    )
                )
    if reservation_id is None:
        async with session_scope() as s:
            await bump_stats(s, ctx.campaign_id, reserved_elsewhere=1)
        await _finish(
            ctx, CandidateOutcome.reserved_elsewhere, "Reserved by another running campaign", "deliver"
        )
        return False
    # Phase 2 — single transaction: re-check exclusion + suppression, convert the hold, exposures, list, stats.
    async with session_scope() as s:
        rules = await load_rules(s, ctx.campaign_id)
        excluded = await excluded_entities(
            s, ctx.workspace_id, EntityType.company, [comp.id], rules, current_campaign_id=ctx.campaign_id
        )
        sup_ids, sup_domains = await suppressed_companies(
            s,
            ctx.workspace_id,
            company_ids=[comp.id],
            domains=[comp.normalized_domain] if comp.normalized_domain else [],
        )
        if excluded or sup_ids or sup_domains:
            await s.execute(
                sa.update(CampaignReservation)
                .where(CampaignReservation.id == reservation_id)
                .values(status=ReservationStatus.released)
            )
    if excluded:
        await _finish(ctx, CandidateOutcome.excluded_previous, "Previously seen company", "deliver")
        return False
    if sup_ids or sup_domains:
        await _finish(ctx, CandidateOutcome.suppressed, "Suppressed company", "deliver")
        return False
    async with session_scope() as s:
        await s.execute(
            sa.update(CampaignReservation)
            .where(CampaignReservation.id == reservation_id)
            .values(status=ReservationStatus.qualified)
        )
        await registry.record_exposures(
            s, ctx.workspace_id, ExposureType.DISCOVERED, company_ids=[comp.id], campaign_id=ctx.campaign_id
        )
        if ctx.target_list_id:
            await lists_svc.add_to_list(
                s,
                ctx.workspace_id,
                ctx.target_list_id,
                EntityType.company,
                [comp.id],
                user_id=None,
                added_via="campaign",
                campaign_id=ctx.campaign_id,
            )
        first = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(
                CompanyDiscoveryEvent.id == ctx.event_id,
                CompanyDiscoveryEvent.outcome == CandidateOutcome.pending,
            )
            .values(
                outcome=CandidateOutcome.qualified,
                stage="deliver",
                stage_data=ctx.stage_data,
                processed_at=sa.func.now(),
            )
            .returning(CompanyDiscoveryEvent.id)
        )
        if first.scalar_one_or_none() is not None:
            await bump_stats(s, ctx.campaign_id, qualified=1, in_flight=-1)
            await s.execute(
                sa.update(CampaignSource)
                .where(
                    CampaignSource.campaign_id == ctx.campaign_id, CampaignSource.source_key == ctx.source_key
                )
                .values(qualified_count=CampaignSource.qualified_count + 1)
            )
            await s.execute(
                sa.update(Campaign)
                .where(Campaign.id == ctx.campaign_id)
                .values(last_progress_at=sa.func.now())
            )
        await emit(
            ctx.workspace_id,
            "lead.qualified",
            {
                "campaign_id": str(ctx.campaign_id),
                "event_id": str(ctx.event_id),
                "list_id": str(ctx.target_list_id) if ctx.target_list_id else None,
                "company_id": str(comp.id),
                "company": comp.name,
                "icp_score": result.icp_score,
            },
            campaign_id=ctx.campaign_id,
            session=s,
        )
    return True


async def pages_for(workspace_id: uuid.UUID, company_id: uuid.UUID) -> list[WebsitePage]:
    async with session_scope() as s:
        return list(
            (await s.scalars(sa.select(WebsitePage).where(WebsitePage.company_id == company_id))).all()
        )
