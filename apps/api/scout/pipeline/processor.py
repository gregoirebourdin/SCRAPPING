"""Per-company pipeline (`company.process`): domain-centric, checkpointed stages, filter early (spec §50–51, §99).

resolve_website → crawl (cache-first) → website conditions → company qualification → decision makers →
person registry/exclusion/reservation → email finder → verification → scoring → quality gate → deliver.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.enums import (
    CampaignMode,
    CandidateOutcome,
    EmailStatus,
    EntityType,
    ExposureType,
    ReservationStatus,
    SourceType,
    WebsiteStatus,
)
from scout.db.models import (
    Campaign,
    CampaignReservation,
    CampaignSource,
    Company,
    CompanyDiscoveryEvent,
    Email,
    PersonDiscoveryEvent,
    QualificationScore,
    Signal,
    WebsitePage,
)
from scout.jobs.events import emit
from scout.jobs.registry import JobContext, job_handler
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
from scout.services.exclusion import excluded_entities, suppressed_people
from scout.services.usage import allow_expensive

log = structlog.get_logger("processor")

RESERVATION_TTL = timedelta(minutes=30)
PERSON_SOURCE_LABEL = {
    "registry": "official registry", "jsonld": "website structured data", "team_card": "team page",
    "legal_notice": "legal notice", "text_pattern": "website", "ai": "AI extraction (verified on page)",
    "grounded": "web research", "mailto": "published email",
}


@dataclass
class Rejection(Exception):
    reason: str
    stage: str


@dataclass
class PersonPick:
    candidate: Any            # scout.extract.types.PersonCandidate
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
    pages: list[Any] = field(default_factory=list)


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
        await s.execute(
            sa.update(CompanyDiscoveryEvent).where(CompanyDiscoveryEvent.id == ctx.event_id)
            .values(stage=stage, stage_data=ctx.stage_data)
        )


async def _finish(ctx: Ctx, outcome: CandidateOutcome, reason: str | None, stage: str) -> bool:
    """Transition the candidate out of `pending` exactly once (idempotent counters)."""
    async with session_scope() as s:
        res = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(CompanyDiscoveryEvent.id == ctx.event_id, CompanyDiscoveryEvent.outcome == CandidateOutcome.pending)
            .values(outcome=outcome, reason=reason, stage=stage, stage_data=ctx.stage_data, processed_at=sa.func.now())
            .returning(CompanyDiscoveryEvent.id)
        )
        changed = res.scalar_one_or_none() is not None
        if changed:
            await bump_stats(
                s, ctx.campaign_id, in_flight=-1,
                rejected=1 if outcome == CandidateOutcome.rejected else 0,
                excluded_previous=1 if outcome == CandidateOutcome.excluded_previous else 0,
                errors=1 if outcome == CandidateOutcome.error else 0,
            )
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
            workspace_id=ev.workspace_id, campaign_id=c.id, event_id=ev.id, company_id=ev.company_id,
            defn=CampaignDefinition.model_validate(c.definition), target_list_id=c.target_list_id,
            source_key=ev.source_key, stage_data=dict(ev.stage_data or {}),
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
            delivered = await _deliver_company(ctx, conditions, fit)
            return {"delivered": delivered}
        picks = await _stage_people(ctx, hints)
        await job.checkpoint()
        delivered, last_reason = await _stage_persons(ctx, job, picks, conditions, fit)
        if delivered == 0:
            await _finish(ctx, CandidateOutcome.rejected, last_reason or "No qualified decision maker", "people")
        return {"delivered": delivered}
    except Rejection as rej:
        await _finish(ctx, CandidateOutcome.rejected, rej.reason, rej.stage)
        return {"rejected": rej.reason}


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
    needs_site = bool(ctx.defn.website_conditions) or ctx.defn.mode == CampaignMode.people or "website" in ctx.defn.required_fields
    if not comp.website_url and needs_site:
        from scout.crawl.resolve import resolve_website  # type: ignore[import-not-found]

        resolved = await resolve_website(
            comp.name, city=comp.city, postal_code=comp.postal_code, country=comp.country,
            registry_id=comp.registry_id, phone=comp.phone,
        )
        if resolved is None:
            async with session_scope() as s:
                await s.execute(sa.update(Company).where(Company.id == comp.id).values(website_status=WebsiteStatus.none))
            raise Rejection("Website not found", "resolve_website")
        async with session_scope() as s:
            company = await s.get(Company, comp.id)
            assert company is not None
            taken = await s.scalar(
                sa.select(Company.id).where(Company.workspace_id == ctx.workspace_id, Company.normalized_domain == resolved.domain, Company.id != comp.id)
            )
            if taken is not None:
                raise Rejection("Website belongs to a company already in the registry", "resolve_website")
            company.domain = company.normalized_domain = resolved.domain
            await registry.observe_company(
                s, ctx.workspace_id, company, {"website_url": resolved.url},
                registry.Evidence(source_type=SourceType.website, confidence=resolved.confidence, source_key="website",
                                  source_url=resolved.url, evidence=resolved.evidence),
                campaign_id=ctx.campaign_id,
            )
    ctx.stage_data["website_done"] = True
    await _set_stage(ctx, "website")


async def _stage_crawl(ctx: Ctx) -> None:
    from scout.crawl.cache import ensure_crawled  # type: ignore[import-not-found]

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
            WebsiteStatus.unreachable: "Website unreachable", WebsiteStatus.parked: "Parked domain",
            WebsiteStatus.blocked: "Website blocks automated access",
        }.get(comp.website_status, "Website could not be crawled")
        if ctx.defn.website_conditions or ctx.defn.mode == CampaignMode.people:
            raise Rejection(label, "crawl")
    if not ctx.stage_data.get("crawl_counted"):
        ctx.stage_data["crawl_counted"] = True
        async with session_scope() as s:
            await bump_stats(s, ctx.campaign_id, companies_evaluated=1)
    # deterministic company facts from the crawl (once per crawl)
    if pages and (comp.last_enriched_at is None or (comp.last_crawled_at and comp.last_enriched_at < comp.last_crawled_at)):
        from scout.extract.company_info import extract_company_facts  # type: ignore[import-not-found]

        facts = extract_company_facts(pages)
        async with session_scope() as s:
            company = await s.get(Company, comp.id)
            assert company is not None
            for f in facts:
                if f.field_name.startswith("social_"):
                    if f.field_name == "social_linkedin" and not company.linkedin_url:
                        company.linkedin_url = str(f.value)
                    continue
                fname = "registry_id" if f.field_name in ("siren", "registry_id") else f.field_name
                if fname not in registry.COMPANY_FIELDS:
                    continue
                await registry.observe_company(
                    s, ctx.workspace_id, company, {fname: f.value},
                    registry.Evidence(source_type=SourceType.website, confidence=f.confidence, source_key="website",
                                      source_url=f.source_url, evidence=f.evidence),
                    campaign_id=ctx.campaign_id,
                )
            company.last_enriched_at = datetime.now(UTC)
    await _set_stage(ctx, "crawl")


async def _stage_conditions(ctx: Ctx) -> list[ConditionOutcome]:
    """Website conditions run right after the crawl — before any people/email spend (spec §51)."""
    if not ctx.defn.website_conditions:
        return []
    from scout.enrich.conditions import evaluate_condition  # type: ignore[import-not-found]

    comp = await _company(ctx)
    out: list[ConditionOutcome] = []
    # deterministic conditions first: a failing keyword condition avoids any AI call
    ordered = sorted(ctx.defn.website_conditions, key=lambda c: 1 if isinstance(c, SemanticCondition) else 0)
    for cond in ordered:
        res = await evaluate_condition(ctx.workspace_id, comp, cond, ctx.pages)
        label = _condition_label(cond)
        outcome = ConditionOutcome(label=label, passed=res.passed, confidence=res.confidence, evidence=res.evidence,
                                   source_url=res.source_url, required=cond.required)
        if isinstance(cond, SemanticCondition) and res.passed and res.confidence < cond.min_confidence:
            outcome.passed = None
        out.append(outcome)
        if cond.required and outcome.passed is not True:
            ctx.stage_data["conditions"] = [o.__dict__ for o in out]
            raise Rejection(f"{label}: " + ("not found on website" if outcome.passed is False else "insufficient evidence"), "website_conditions")
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
    im = industry_fit(ctx.defn.company_filters.industries, category=comp.category_raw, industry=comp.industry,
                      name=comp.name, description=comp.description, page_text=text)
    sf = size_fit(ctx.defn, comp.employee_min, comp.employee_max)
    lf = location_fit(ctx.defn, comp.country, comp.city, comp.region)
    from scout.pipeline.scoring import company_fit as _cf

    probe = ScoringInput(defn=ctx.defn, industry_fit=im.score if im else None, size_fit=sf, location_fit=lf,
                         company_confidence=comp.company_confidence, conditions=conditions)
    cf = _cf(probe)
    conf_parts = [0.95 if comp.registry_id else None, 0.9 if comp.website_status == WebsiteStatus.ok else None,
                  comp.employee_confidence]
    vals = [v for v in conf_parts if v is not None]
    company_conf = round(sum(vals) / len(vals), 3) if vals else 0.6
    async with session_scope() as s:
        await s.execute(sa.update(Company).where(Company.id == comp.id).values(
            company_confidence=company_conf,
            industry=sa.func.coalesce(Company.industry, im.label if im and im.score >= 0.6 else None),
        ))
    fit = FitResult(im.score if im else None, sf, lf, im.label if im else None, cf, company_conf)
    if sf is not None and sf == 0.0:
        raise Rejection("Company size outside requested range", "company_qualification")
    if lf is not None and lf == 0.0:
        raise Rejection("Location outside requested area", "company_qualification")
    if cf is not None and cf * 100 < ctx.defn.minimum_company_fit:
        raise Rejection(f"Company fit too low ({round(cf * 100)})", "company_qualification")
    if not ctx.stage_data.get("matched_counted"):
        ctx.stage_data["matched_counted"] = True
        async with session_scope() as s:
            await bump_stats(s, ctx.campaign_id, companies_matched=1)
    await _set_stage(ctx, "company_qualification")
    return fit


async def _stage_people(ctx: Ctx, hints: dict[str, Any]) -> list[PersonPick]:
    from scout.extract.names import is_plausible_person_name, split_name  # type: ignore[import-not-found]
    from scout.extract.people import extract_people  # type: ignore[import-not-found]
    from scout.extract.titles import normalize_title, title_match_score  # type: ignore[import-not-found]
    from scout.extract.types import PersonCandidate

    comp = await _company(ctx)
    pf = ctx.defn.people_filters
    candidates: list[PersonCandidate] = []
    for p in hints.get("people") or []:
        name = (p.get("full_name") or "").strip()
        if not name or not is_plausible_person_name(name):
            continue
        first, last = split_name(name)
        candidates.append(PersonCandidate(
            full_name=name, first_name=first, last_name=last, title=p.get("title"), source_url=p.get("source_url"),
            source_type="registry", method="registry", evidence=f"{name} — {p.get('title') or 'director'} (official registry)",
            confidence=0.95,
        ))
    if ctx.pages:
        candidates.extend(extract_people(ctx.pages, company_name=comp.name, domain=comp.normalized_domain))
    picks = _rank(candidates, pf, normalize_title, title_match_score)
    if not picks and ctx.pages:
        from scout.extract.ai_people import ai_extract_people  # type: ignore[import-not-found]

        try:
            ai_found = await ai_extract_people(ctx.pages, company_name=comp.name)
        except Exception as exc:
            log.info("people.ai_failed", error=str(exc))
            ai_found = []
        picks = _rank(ai_found, pf, normalize_title, title_match_score)
    if not picks and await allow_expensive():
        picks = _rank(await _grounded_people(ctx, comp), pf, normalize_title, title_match_score)
    if not picks:
        raise Rejection("No decision maker found", "people")
    if not ctx.stage_data.get("people_counted"):
        ctx.stage_data["people_counted"] = True
        async with session_scope() as s:
            await bump_stats(s, ctx.campaign_id, people_found=1)
    await _set_stage(ctx, "people")
    return picks


def _rank(candidates: list[Any], pf: Any, normalize_title: Any, title_match_score: Any) -> list[PersonPick]:
    out: dict[str, PersonPick] = {}
    wants_roles = bool(pf.titles or pf.role_families or pf.seniorities)
    for cand in candidates:
        info = normalize_title(cand.title) if cand.title else None
        if wants_roles:
            ts = title_match_score(info, titles=pf.titles, role_families=[r.value for r in pf.role_families],
                                   seniorities=[x.value for x in pf.seniorities]) if info else 0.0
            if ts < 0.5:
                continue
        else:
            ts = 0.7
        rank = ts * 0.5 + cand.confidence * 0.35 + ((info.decision_power if info else 40) / 100.0) * 0.15
        key = registry.normalize_person_name(cand.full_name) if hasattr(registry, "normalize_person_name") else cand.full_name.lower()
        prev = out.get(key)
        if prev is None or rank > prev.rank:
            out[key] = PersonPick(cand, info, ts, rank)
    return sorted(out.values(), key=lambda p: p.rank, reverse=True)


async def _grounded_people(ctx: Ctx, comp: Company) -> list[Any]:
    """Grounded research fallback: names only accepted with grounding sources (never invented)."""
    from pydantic import BaseModel

    from scout.ai.factory import get_ai
    from scout.extract.names import is_plausible_person_name, split_name  # type: ignore[import-not-found]
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
        out.append(PersonCandidate(
            full_name=p.full_name, first_name=first, last_name=last, title=p.title,
            source_url=p.source_url or (res.sources[0].uri if res.sources else None), source_type="grounded_search",
            method="grounded", evidence=f"Web research ({', '.join(sorted(d for d in domains if d))[:120]})", confidence=0.7,
        ))
    return out


# =============================================================================================
# Persons → email → score → gate → deliver
# =============================================================================================


async def _stage_persons(
    ctx: Ctx, job: JobContext, picks: list[PersonPick], conditions: list[ConditionOutcome], fit: FitResult
) -> tuple[int, str | None]:
    from scout.email.store import find_and_save_for_person  # type: ignore[import-not-found]

    defn = ctx.defn
    delivered = 0
    last_reason: str | None = None
    rules = None
    comp = await _company(ctx)
    for pick in picks[: max(3, defn.people_filters.max_people_per_company * 3)]:
        if delivered >= defn.people_filters.max_people_per_company:
            break
        await job.checkpoint()
        cand = pick.candidate
        st = {
            "registry": SourceType.registry, "website": SourceType.website, "ai_extraction": SourceType.ai_extraction,
            "grounded_search": SourceType.grounded_search, "public_profile": SourceType.public_profile,
        }.get(cand.source_type, SourceType.website)
        async with session_scope() as s:
            person, _created = await registry.upsert_person(
                s, ctx.workspace_id, company_id=comp.id, full_name=cand.full_name, first_name=cand.first_name,
                last_name=cand.last_name, job_title=cand.title, title_info=pick.title_info, profile_url=cand.profile_url,
                evidence=registry.Evidence(source_type=st, confidence=cand.confidence, source_key=_person_source_key(ctx, cand),
                                           source_url=cand.source_url, evidence=cand.evidence),
                campaign_id=ctx.campaign_id,
            )
            person_id = person.id
            if rules is None:
                rules = await load_rules(s, ctx.campaign_id)
            sup, _ = await suppressed_people(s, ctx.workspace_id, person_ids=[person_id])
            if sup:
                await _person_event(s, ctx, person_id, CandidateOutcome.suppressed, "Suppressed", cand.source_url)
                last_reason = "Decision maker is suppressed"
                continue
            excl = await excluded_entities(s, ctx.workspace_id, EntityType.person, [person_id], rules, current_campaign_id=ctx.campaign_id)
            if excl:
                await _person_event(s, ctx, person_id, CandidateOutcome.excluded_previous, excl[person_id], cand.source_url)
                await bump_stats(s, ctx.campaign_id, excluded_previous=1)
                last_reason = "Decision maker previously seen (excluded)"
                continue
            # atomic reservation: two concurrent campaigns cannot both deliver the same "new" person
            key = registry.person_key(comp.id, person.full_name, person.public_profile_url)
            res = await s.execute(
                pg_insert(CampaignReservation).values(
                    workspace_id=ctx.workspace_id, campaign_id=ctx.campaign_id, entity_type=EntityType.person,
                    entity_key=key, company_id=comp.id, person_id=person_id, status=ReservationStatus.reserved,
                    expires_at=datetime.now(UTC) + RESERVATION_TTL,
                ).on_conflict_do_nothing(
                    index_elements=["workspace_id", "entity_type", "entity_key"], index_where=sa.text("status = 'reserved'")
                ).returning(CampaignReservation.id)
            )
            reservation_id = res.scalar_one_or_none()
            if reservation_id is None:
                holder = await s.scalar(sa.select(CampaignReservation.campaign_id).where(
                    CampaignReservation.workspace_id == ctx.workspace_id, CampaignReservation.entity_key == key,
                    CampaignReservation.status == ReservationStatus.reserved))
                if holder != ctx.campaign_id:
                    await _person_event(s, ctx, person_id, CandidateOutcome.reserved_elsewhere, "Reserved by another running campaign", cand.source_url)
                    await bump_stats(s, ctx.campaign_id, reserved_elsewhere=1)
                    last_reason = "Decision maker reserved by another campaign"
                    continue
                reservation_id = await s.scalar(sa.select(CampaignReservation.id).where(
                    CampaignReservation.campaign_id == ctx.campaign_id, CampaignReservation.entity_key == key,
                    CampaignReservation.status == ReservationStatus.reserved))
        # ---- email (reuse fresh SAFE emails: no re-verification, spec §89) ----
        email_addr, email_status, email_conf = await _existing_email(person_id)
        if defn.requires_email or defn.mode == CampaignMode.people:
            if email_status != EmailStatus.SAFE:
                try:
                    finding = await find_and_save_for_person(ctx.workspace_id, person_id)
                except Exception as exc:
                    log.warning("email.find_failed", error=str(exc), person_id=str(person_id))
                    finding = None
                email_addr, email_status, email_conf = await _existing_email(person_id)
                if finding is not None and not email_addr:
                    last_reason = finding.reason or "No professional email found"
            async with session_scope() as s:
                await bump_stats(s, ctx.campaign_id, emails_found=1 if email_addr else 0,
                                 emails_safe=1 if email_status == EmailStatus.SAFE else 0)
        # ---- score + gate ----
        async with session_scope() as s:
            person = await s.get(type(person), person_id)
            assert person is not None
            signals = [
                {"type": sg.type.value, "confidence": sg.confidence}
                for sg in (await s.scalars(sa.select(Signal).where(Signal.company_id == comp.id))).all()
            ]
            n_sources = await s.scalar(sa.text(
                "SELECT count(DISTINCT coalesce(source_key, source_type)) FROM (SELECT source_key, source_type FROM "
                "company_field_observations WHERE company_id = :c UNION ALL SELECT source_key, source_type FROM "
                "person_field_observations WHERE person_id = :p) x"), {"c": comp.id, "p": person_id})
        inp = ScoringInput(
            defn=defn, industry_fit=fit.industry, size_fit=fit.size, location_fit=fit.location,
            company_confidence=fit.company_confidence, conditions=conditions, person_identified=True,
            person_name=person.full_name, person_title=person.job_title, title_match=pick.title_score,
            decision_power=person.decision_power, person_confidence=person.identity_confidence,
            person_source=PERSON_SOURCE_LABEL.get(cand.method, cand.method), email=email_addr,
            email_status=email_status, email_confidence=email_conf, phone=bool(person.phone or comp.phone),
            profile_url=bool(person.public_profile_url), signals=signals, evidence_sources=int(n_sources or 0),
            evidence_quality=cand.confidence, company_name=comp.name, industry_label=fit.industry_label,
        )
        result = score(inp)
        if result.qualified:
            ok = await _deliver_person(ctx, comp, person_id, reservation_id, result, email_status)
            if ok:
                delivered += 1
                continue
            last_reason = "Lost a race with another campaign"
        else:
            last_reason = result.first_failure
            async with session_scope() as s:
                await _save_score(s, ctx, comp.id, person_id, result)
                await _person_event(s, ctx, person_id, CandidateOutcome.rejected, result.first_failure, cand.source_url)
                if reservation_id:
                    await s.execute(sa.update(CampaignReservation).where(CampaignReservation.id == reservation_id)
                                    .values(status=ReservationStatus.released))
    return delivered, last_reason


def _person_source_key(ctx: Ctx, cand: Any) -> str:
    if cand.source_type == "registry":
        return ctx.source_key if ctx.source_key in ("fr_registry", "fixture") else "registry"
    if cand.source_type == "grounded_search":
        return "gemini_search"
    if cand.source_type == "ai_extraction":
        return "ai_extraction"
    return "website"


async def _existing_email(person_id: uuid.UUID) -> tuple[str | None, EmailStatus | None, float | None]:
    async with session_scope() as s:
        row = (await s.execute(
            sa.select(Email.address, Email.status, Email.overall_confidence, Email.last_checked_at)
            .where(Email.person_id == person_id)
            .order_by(Email.is_primary.desc(), Email.overall_confidence.desc().nullslast())
            .limit(1)
        )).first()
    if row is None:
        return None, None, None
    addr, status, conf, checked = row
    if status == EmailStatus.SAFE and checked and datetime.now(UTC) - checked > timedelta(days=60):
        return addr, EmailStatus.UNKNOWN, conf  # stale SAFE → re-verify
    return addr, status, conf


async def _person_event(s: Any, ctx: Ctx, person_id: uuid.UUID, outcome: CandidateOutcome, reason: str | None, url: str | None) -> None:
    s.add(PersonDiscoveryEvent(workspace_id=ctx.workspace_id, campaign_id=ctx.campaign_id, person_id=person_id,
                               company_id=ctx.company_id, source_key=ctx.source_key, source_url=url, outcome=outcome,
                               reason=(reason or "")[:500] or None))


async def _save_score(s: Any, ctx: Ctx, company_id: uuid.UUID, person_id: uuid.UUID | None, r: Any) -> None:
    values = dict(
        workspace_id=ctx.workspace_id, campaign_id=ctx.campaign_id, company_id=company_id, person_id=person_id,
        icp_score=r.icp_score, company_fit=r.company_fit, person_fit=r.person_fit, intent=r.intent,
        contactability=r.contactability, evidence_score=r.evidence, company_confidence=r.company_confidence,
        person_confidence=r.person_confidence, email_confidence=r.email_confidence,
        enrichment_confidence=r.enrichment_confidence, overall_confidence=r.overall_confidence, qualified=r.qualified,
        gate_results=r.gates, weights=r.weights, explanation=r.explanation,
    )
    stmt = pg_insert(QualificationScore).values(**values).on_conflict_do_update(
        index_elements=["campaign_id", "company_id", "person_id"],
        set_={k: v for k, v in values.items() if k not in ("workspace_id", "campaign_id", "company_id", "person_id")} | {"computed_at": sa.func.now()},
    )
    await s.execute(stmt)


async def _deliver_person(
    ctx: Ctx, comp: Company, person_id: uuid.UUID, reservation_id: uuid.UUID | None, result: Any, email_status: EmailStatus | None
) -> bool:
    """Single transaction: re-check, reservation → qualified, exposures, list membership, stats, event."""
    async with session_scope() as s:
        rules = await load_rules(s, ctx.campaign_id)
        excl = await excluded_entities(s, ctx.workspace_id, EntityType.person, [person_id], rules, current_campaign_id=ctx.campaign_id)
        sup, _ = await suppressed_people(s, ctx.workspace_id, person_ids=[person_id])
        if excl or sup:
            if reservation_id:
                await s.execute(sa.update(CampaignReservation).where(CampaignReservation.id == reservation_id).values(status=ReservationStatus.released))
            return False
        if reservation_id:
            await s.execute(sa.update(CampaignReservation).where(CampaignReservation.id == reservation_id).values(status=ReservationStatus.qualified))
        await _save_score(s, ctx, comp.id, person_id, result)
        await registry.record_exposures(s, ctx.workspace_id, ExposureType.DISCOVERED, person_ids=[person_id], campaign_id=ctx.campaign_id)
        if ctx.target_list_id:
            await lists_svc.add_to_list(s, ctx.workspace_id, ctx.target_list_id, EntityType.person, [person_id],
                                        user_id=None, added_via="campaign", campaign_id=ctx.campaign_id)
        await _person_event(s, ctx, person_id, CandidateOutcome.qualified, None, None)
        first = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(CompanyDiscoveryEvent.id == ctx.event_id, CompanyDiscoveryEvent.outcome == CandidateOutcome.pending)
            .values(outcome=CandidateOutcome.qualified, stage="deliver", stage_data=ctx.stage_data, processed_at=sa.func.now())
            .returning(CompanyDiscoveryEvent.id)
        )
        await bump_stats(
            s, ctx.campaign_id, qualified=1, emails_accepted=1 if email_status else 0,
            in_flight=-1 if first.scalar_one_or_none() is not None else 0,
        )
        await s.execute(sa.update(CampaignSource).where(CampaignSource.campaign_id == ctx.campaign_id, CampaignSource.source_key == ctx.source_key)
                        .values(qualified_count=CampaignSource.qualified_count + 1))
        await s.execute(sa.update(Campaign).where(Campaign.id == ctx.campaign_id).values(last_progress_at=sa.func.now()))
        from scout.db.models import Person

        p = await s.get(Person, person_id)
        await emit(ctx.workspace_id, "lead.qualified", {
            "campaign_id": str(ctx.campaign_id), "list_id": str(ctx.target_list_id) if ctx.target_list_id else None,
            "person_id": str(person_id), "company_id": str(comp.id), "name": p.full_name if p else None,
            "company": comp.name, "icp_score": result.icp_score,
        }, campaign_id=ctx.campaign_id, session=s)
    try:
        from scout.discovery.health import record_outcomes  # type: ignore[import-not-found]

        await record_outcomes(ctx.source_key, qualified=1)
    except Exception:
        pass
    return True


async def _deliver_company(ctx: Ctx, conditions: list[ConditionOutcome], fit: FitResult) -> bool:
    comp = await _company(ctx)
    inp = ScoringInput(
        defn=ctx.defn, industry_fit=fit.industry, size_fit=fit.size, location_fit=fit.location,
        company_confidence=fit.company_confidence, conditions=conditions, phone=bool(comp.phone),
        evidence_sources=2 if comp.registry_id else 1, evidence_quality=0.9, company_name=comp.name,
        industry_label=fit.industry_label,
    )
    result = score(inp)
    async with session_scope() as s:
        await _save_score(s, ctx, comp.id, None, result)
    if not result.qualified:
        await _finish(ctx, CandidateOutcome.rejected, result.first_failure, "gate")
        return False
    async with session_scope() as s:
        rules = await load_rules(s, ctx.campaign_id)
        if await excluded_entities(s, ctx.workspace_id, EntityType.company, [comp.id], rules, current_campaign_id=ctx.campaign_id):
            await _finish(ctx, CandidateOutcome.excluded_previous, "Previously seen company", "deliver")
            return False
        key = f"d:{comp.normalized_domain}" if comp.normalized_domain else f"id:{comp.id}"
        res = await s.execute(
            pg_insert(CampaignReservation).values(
                workspace_id=ctx.workspace_id, campaign_id=ctx.campaign_id, entity_type=EntityType.company, entity_key=key,
                company_id=comp.id, status=ReservationStatus.qualified, expires_at=datetime.now(UTC) + RESERVATION_TTL,
            ).on_conflict_do_nothing(index_elements=["workspace_id", "entity_type", "entity_key"], index_where=sa.text("status = 'reserved'"))
            .returning(CampaignReservation.id)
        )
        if res.scalar_one_or_none() is None:
            await _finish(ctx, CandidateOutcome.reserved_elsewhere, "Reserved by another running campaign", "deliver")
            return False
        await registry.record_exposures(s, ctx.workspace_id, ExposureType.DISCOVERED, company_ids=[comp.id], campaign_id=ctx.campaign_id)
        if ctx.target_list_id:
            await lists_svc.add_to_list(s, ctx.workspace_id, ctx.target_list_id, EntityType.company, [comp.id],
                                        user_id=None, added_via="campaign", campaign_id=ctx.campaign_id)
        first = await s.execute(
            sa.update(CompanyDiscoveryEvent)
            .where(CompanyDiscoveryEvent.id == ctx.event_id, CompanyDiscoveryEvent.outcome == CandidateOutcome.pending)
            .values(outcome=CandidateOutcome.qualified, stage="deliver", stage_data=ctx.stage_data, processed_at=sa.func.now())
            .returning(CompanyDiscoveryEvent.id)
        )
        if first.scalar_one_or_none() is not None:
            await bump_stats(s, ctx.campaign_id, qualified=1, in_flight=-1)
        await emit(ctx.workspace_id, "lead.qualified", {
            "campaign_id": str(ctx.campaign_id), "list_id": str(ctx.target_list_id) if ctx.target_list_id else None,
            "company_id": str(comp.id), "company": comp.name, "icp_score": result.icp_score,
        }, campaign_id=ctx.campaign_id, session=s)
    return True


async def pages_for(workspace_id: uuid.UUID, company_id: uuid.UUID) -> list[WebsitePage]:
    async with session_scope() as s:
        return list((await s.scalars(sa.select(WebsitePage).where(WebsitePage.company_id == company_id))).all())
