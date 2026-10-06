"""Lead detail, provenance (source inspector), history timeline, human overrides and review approvals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import EntityType, ExposureType, SourceType, VerificationRequestStatus
from scout.db.models import (
    Campaign,
    Company,
    CompanyDiscoveryEvent,
    CompanyFieldObservation,
    CustomColumn,
    CustomFieldValue,
    DomainEmailPattern,
    DomainProfile,
    Email,
    EmailCheck,
    EmailVerificationRequest,
    LeadExposure,
    List,
    ListMembership,
    Person,
    PersonDiscoveryEvent,
    PersonFieldObservation,
    QualificationScore,
    Signal,
    Technology,
    WebsiteCrawlRun,
    WebsitePage,
)
from scout.errors import NotFound, ValidationFailed
from scout.services import registry
from scout.services.freshness import freshness_label

log = structlog.get_logger(__name__)

SOURCE_LABELS = {
    "website": "Official company website",
    "registry": "Official registry",
    "fr_registry": "French company registry (SIRENE/RNE)",
    "maps": "Google Maps listing",
    "google_maps": "Google Maps listing",
    "directory": "Directory",
    "search_snippet": "Search result",
    "web_search": "Web search result",
    "grounded_search": "Grounded web research",
    "gemini_search": "Grounded web research",
    "ai_extraction": "AI extraction from website",
    "public_profile": "Public professional profile",
    "import": "CSV import",
    "user": "Edited by you",
    "tech_scan": "Technology fingerprint",
    "derived": "Derived",
    "osm": "OpenStreetMap",
    "yc": "Y Combinator directory",
    "hn_hiring": "Hacker News hiring thread",
    "github": "GitHub",
    "fixture": "Fixture source",
}


def source_label(source_key: str | None, source_type: str | None, page_type: str | None = None) -> str:
    if source_type == "website" and page_type and page_type not in ("other", "home"):
        return f"Official company {page_type.replace('_', ' ')} page"
    return SOURCE_LABELS.get(
        source_key or "", SOURCE_LABELS.get(source_type or "", (source_key or source_type or "Source"))
    )


def _obs_dict(o: Any, page_types: dict[uuid.UUID, str]) -> dict[str, Any]:
    pt = page_types.get(o.page_id) if o.page_id else None
    return {
        "id": o.id,
        "field": o.field_name,
        "value": o.value_json,
        "source_type": o.source_type.value,
        "source_key": o.source_key,
        "source_url": o.source_url,
        "source_title": o.source_title,
        "source_label": source_label(o.source_key, o.source_type.value, pt),
        "evidence": o.evidence,
        "confidence": o.confidence,
        "user_confirmed": o.is_user_confirmed,
        "observed_at": o.observed_at,
        "freshness": freshness_label(o.observed_at),
    }


async def _page_types(s: AsyncSession, page_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not page_ids:
        return {}
    rows = (
        await s.execute(sa.select(WebsitePage.id, WebsitePage.page_type).where(WebsitePage.id.in_(page_ids)))
    ).all()
    return {pid: pt.value for pid, pt in rows}


async def company_detail(s: AsyncSession, workspace_id: uuid.UUID, company_id: uuid.UUID) -> dict[str, Any]:
    c = await s.get(Company, company_id)
    if c is None or c.workspace_id != workspace_id:
        raise NotFound("Company not found")
    obs = (
        await s.scalars(
            sa.select(CompanyFieldObservation)
            .where(CompanyFieldObservation.company_id == c.id)
            .order_by(CompanyFieldObservation.observed_at.desc())
        )
    ).all()
    pts = await _page_types(s, [o.page_id for o in obs if o.page_id])
    people = (
        await s.scalars(
            sa.select(Person)
            .where(Person.company_id == c.id)
            .order_by(Person.decision_power.desc().nullslast())
        )
    ).all()
    techs = (
        await s.scalars(sa.select(Technology).where(Technology.company_id == c.id).order_by(Technology.name))
    ).all()
    signals = (
        await s.scalars(
            sa.select(Signal).where(Signal.company_id == c.id).order_by(Signal.observed_at.desc())
        )
    ).all()
    pages = (
        await s.execute(
            sa.select(
                WebsitePage.id,
                WebsitePage.url,
                WebsitePage.page_type,
                WebsitePage.title,
                WebsitePage.fetched_at,
            )
            .where(WebsitePage.company_id == c.id)
            .order_by(WebsitePage.page_type)
        )
    ).all()
    company_emails = (
        await s.scalars(sa.select(Email).where(Email.company_id == c.id, Email.person_id.is_(None)))
    ).all()
    by_field: dict[str, list[dict[str, Any]]] = {}
    for o in obs:
        by_field.setdefault(o.field_name, []).append(_obs_dict(o, pts))
    return {
        "id": c.id,
        "name": c.name,
        "domain": c.normalized_domain,
        "website_url": c.website_url,
        "description": c.description,
        "country": c.country,
        "region": c.region,
        "city": c.city,
        "postal_code": c.postal_code,
        "address": c.address,
        "industry": c.industry,
        "sub_industry": c.sub_industry,
        "category_raw": c.category_raw,
        "employee_min": c.employee_min,
        "employee_max": c.employee_max,
        "employee_confidence": c.employee_confidence,
        "phone": c.phone,
        "linkedin_url": c.linkedin_url,
        "registry_source": c.registry_source,
        "registry_id": c.registry_id,
        "founded_year": c.founded_year,
        "status": c.status.value,
        "website_status": c.website_status.value,
        "company_confidence": c.company_confidence,
        "has_conflicts": c.has_conflicts,
        "needs_review": c.needs_review,
        "first_seen_at": c.first_seen_at,
        "last_seen_at": c.last_seen_at,
        "last_crawled_at": c.last_crawled_at,
        "last_enriched_at": c.last_enriched_at,
        "times_discovered": c.times_discovered,
        "times_exported": c.times_exported,
        "freshness": freshness_label(c.last_crawled_at or c.updated_at),
        "observations": by_field,
        "people": [
            {
                "id": p.id,
                "full_name": p.full_name,
                "job_title": p.job_title,
                "role_family": p.role_family.value if p.role_family else None,
                "identity_confidence": p.identity_confidence,
            }
            for p in people
        ],
        "technologies": [
            {
                "name": t.name,
                "category": t.category,
                "version": t.version,
                "confidence": t.confidence,
                "detector": t.detector,
                "source_url": t.source_url,
                "observed_at": t.observed_at,
            }
            for t in techs
        ],
        "signals": [
            {
                "id": sg.id,
                "type": sg.type.value,
                "value": sg.value,
                "source_url": sg.source_url,
                "evidence": sg.evidence,
                "confidence": sg.confidence,
                "observed_at": sg.observed_at,
            }
            for sg in signals
        ],
        "pages": [
            {"id": pid, "url": url, "page_type": pt.value, "title": title, "fetched_at": fa}
            for pid, url, pt, title, fa in pages
        ],
        "company_emails": [
            {"address": e.address, "status": e.status.value, "kind": e.kind.value, "source_url": e.source_url}
            for e in company_emails
        ],
        "email_intel": await _email_intel(s, c.normalized_domain),
    }


def _explanation(checks: list[EmailCheck]) -> dict[str, Any]:
    """Why the latest verdict is what it is (signals recorded by the Email Intelligence Engine)."""
    raw = ((checks[0].result or {}).get("raw") or {}) if checks else {}
    return {
        "explanation": raw.get("signals") or [],
        "resolution_path": raw.get("path"),
        "resolver": raw.get("resolver"),
        "name_affinity": raw.get("affinity"),
    }


async def _email_intel(s: AsyncSession, domain: str | None) -> dict[str, Any] | None:
    """Domain Intelligence Profile summary for the company drawer (public, domain-level facts only)."""
    if not domain:
        return None
    prof = await s.get(DomainProfile, domain)
    patterns = (
        await s.scalars(
            sa.select(DomainEmailPattern)
            .where(DomainEmailPattern.domain == domain, DomainEmailPattern.confidence > 0)
            .order_by(DomainEmailPattern.confidence.desc())
            .limit(3)
        )
    ).all()
    if prof is None and not patterns:
        return None
    return {
        "domain": domain,
        "provider": prof.provider.value if prof else None,
        "mx_hosts": (prof.mx_hosts if prof else [])[:3],
        "accepts_mail": prof.accepts_mail if prof else None,
        "catch_all": prof.catch_all if prof else None,
        "catch_all_checked_at": prof.catch_all_checked_at if prof else None,
        "smtp_reachable": prof.smtp_reachable if prof else None,
        "greylisting_seen": prof.greylisting_seen if prof else False,
        "named_samples": prof.named_samples if prof else 0,
        "observed_emails": prof.observed_emails if prof else 0,
        "updated_at": prof.updated_at if prof else None,
        "patterns": [
            {
                "pattern": p.pattern,
                "confidence": p.confidence,
                "share": p.share,
                "samples": p.supporting_samples,
                "successes": p.successful_checks,
                "failures": p.failed_checks,
                "last_confirmed_at": p.last_confirmed_at,
            }
            for p in patterns
        ],
    }


async def person_detail(s: AsyncSession, workspace_id: uuid.UUID, person_id: uuid.UUID) -> dict[str, Any]:
    p = await s.get(Person, person_id)
    if p is None or p.workspace_id != workspace_id:
        raise NotFound("Person not found")
    obs = (
        await s.scalars(
            sa.select(PersonFieldObservation)
            .where(PersonFieldObservation.person_id == p.id)
            .order_by(PersonFieldObservation.observed_at.desc())
        )
    ).all()
    pts = await _page_types(s, [o.page_id for o in obs if o.page_id])
    emails = (
        await s.scalars(
            sa.select(Email)
            .where(Email.person_id == p.id)
            .order_by(Email.is_primary.desc(), Email.overall_confidence.desc().nullslast())
        )
    ).all()
    checks: dict[uuid.UUID, list[EmailCheck]] = {}
    if emails:
        for ch in (
            await s.scalars(
                sa.select(EmailCheck)
                .where(EmailCheck.email_id.in_([e.id for e in emails]))
                .order_by(EmailCheck.checked_at.desc())
            )
        ).all():
            checks.setdefault(ch.email_id, []).append(ch)
    score = await s.scalar(
        sa.select(QualificationScore)
        .where(QualificationScore.person_id == p.id)
        .order_by(QualificationScore.computed_at.desc())
        .limit(1)
    )
    verifying = bool(
        await s.scalar(
            sa.select(sa.func.count())
            .select_from(EmailVerificationRequest)
            .where(
                EmailVerificationRequest.person_id == p.id,
                EmailVerificationRequest.status.in_(
                    [
                        VerificationRequestStatus.pending,
                        VerificationRequestStatus.processing,
                        VerificationRequestStatus.retry,
                    ]
                ),
            )
        )
    )
    by_field: dict[str, list[dict[str, Any]]] = {}
    for o in obs:
        by_field.setdefault(o.field_name, []).append(_obs_dict(o, pts))
    company = await company_detail(s, workspace_id, p.company_id) if p.company_id else None
    return {
        "id": p.id,
        "full_name": p.full_name,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "job_title": p.job_title,
        "normalized_title": p.normalized_title,
        "department": p.department,
        "seniority": p.seniority.value if p.seniority else None,
        "role_family": p.role_family.value if p.role_family else None,
        "decision_power": p.decision_power,
        "public_profile_url": p.public_profile_url,
        "location": p.location,
        "phone": p.phone,
        "identity_confidence": p.identity_confidence,
        "needs_review": p.needs_review,
        "has_conflicts": p.has_conflicts,
        "first_seen_at": p.first_seen_at,
        "last_seen_at": p.last_seen_at,
        "last_verified_at": p.last_verified_at,
        "times_discovered": p.times_discovered,
        "times_exported": p.times_exported,
        "last_exported_at": p.last_exported_at,
        "contacted_at": p.contacted_at,
        "suppressed_at": p.suppressed_at,
        "freshness": freshness_label(p.updated_at),
        "observations": by_field,
        "emails": [
            {
                "id": e.id,
                "address": e.address,
                "status": e.status.value,
                "kind": e.kind.value,
                "discovery_method": e.discovery_method.value,
                "pattern": e.pattern,
                "source_url": e.source_url,
                "mx_valid": e.mx_valid,
                "smtp_result": e.smtp_result.value,
                "catch_all": e.catch_all,
                "disposable": e.disposable,
                "role_address": e.role_address,
                "free_provider": e.free_provider,
                "pattern_confidence": e.pattern_confidence,
                "overall_confidence": e.overall_confidence,
                "is_primary": e.is_primary,
                "last_checked_at": e.last_checked_at,
                "freshness": freshness_label(e.last_checked_at),
                "checks": [
                    {
                        "verifier": c.verifier,
                        "status": c.status.value,
                        "smtp_result": c.smtp_result.value,
                        "catch_all": c.catch_all,
                        "checked_at": c.checked_at,
                        "error": c.error,
                    }
                    for c in checks.get(e.id, [])[:5]
                ],
                **_explanation(checks.get(e.id, [])),
            }
            for e in emails
        ],
        "email_verifying": verifying,
        "score": _score_dict(score) if score else None,
        "company": company,
    }


def _score_dict(q: QualificationScore) -> dict[str, Any]:
    return {
        "icp_score": q.icp_score,
        "qualified": q.qualified,
        "company_fit": q.company_fit,
        "person_fit": q.person_fit,
        "intent": q.intent,
        "contactability": q.contactability,
        "evidence": q.evidence_score,
        "company_confidence": q.company_confidence,
        "person_confidence": q.person_confidence,
        "email_confidence": q.email_confidence,
        "enrichment_confidence": q.enrichment_confidence,
        "overall_confidence": q.overall_confidence,
        "gates": q.gate_results,
        "weights": q.weights,
        "explanation": q.explanation,
        "campaign_id": q.campaign_id,
        "computed_at": q.computed_at,
    }


async def lead_lists(
    s: AsyncSession, workspace_id: uuid.UUID, entity_type: EntityType, entity_id: uuid.UUID
) -> list[dict[str, Any]]:
    col = ListMembership.person_id if entity_type == EntityType.person else ListMembership.company_id
    rows = (
        await s.execute(
            sa.select(List.id, List.name, ListMembership.added_at)
            .join(ListMembership, ListMembership.list_id == List.id)
            .where(col == entity_id, List.workspace_id == workspace_id)
        )
    ).all()
    return [{"id": i, "name": n, "added_at": a} for i, n, a in rows]


async def lead_history(
    s: AsyncSession, workspace_id: uuid.UUID, entity_type: EntityType, entity_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Timeline (spec §183): discovered, added to list, crawled, people/email found, verified, enriched, exported."""
    # Workspace isolation: the entity must belong to the caller's workspace (ids are never trusted).
    owner_model = Person if entity_type == EntityType.person else Company
    owner = await s.scalar(sa.select(owner_model.workspace_id).where(owner_model.id == entity_id))
    if owner != workspace_id:
        raise NotFound("Person not found" if entity_type == EntityType.person else "Company not found")
    events: list[dict[str, Any]] = []
    expos = (
        await s.execute(
            sa.select(LeadExposure, Campaign.name, List.name)
            .outerjoin(Campaign, Campaign.id == LeadExposure.campaign_id)
            .outerjoin(List, List.id == LeadExposure.list_id)
            .where(
                LeadExposure.workspace_id == workspace_id,
                LeadExposure.entity_type == entity_type,
                LeadExposure.entity_id == entity_id,
            )
            .order_by(LeadExposure.occurred_at)
        )
    ).all()
    labels = {
        ExposureType.DISCOVERED: "Discovered",
        ExposureType.SHOWN: "Viewed",
        ExposureType.ADDED_TO_LIST: "Added to list",
        ExposureType.IMPORTED: "Imported",
        ExposureType.EXPORTED: "Exported",
        ExposureType.CONTACTED: "Contacted",
        ExposureType.ENRICHED: "Enriched",
    }
    for e, cname, lname in expos:
        if e.exposure_type == ExposureType.SHOWN:
            continue
        detail = lname or cname
        events.append(
            {
                "at": e.occurred_at,
                "type": e.exposure_type.value,
                "label": labels[e.exposure_type],
                "detail": detail,
                "campaign_id": e.campaign_id,
                "list_id": e.list_id,
            }
        )
    company_id: uuid.UUID | None = entity_id
    if entity_type == EntityType.person:
        p = await s.get(Person, entity_id)
        if p is None:
            raise NotFound("Person not found")
        company_id = p.company_id
        for o in (
            await s.scalars(
                sa.select(PersonFieldObservation)
                .where(
                    PersonFieldObservation.person_id == entity_id,
                    PersonFieldObservation.field_name == "full_name",
                )
                .order_by(PersonFieldObservation.observed_at)
            )
        ).all():
            events.append(
                {
                    "at": o.observed_at,
                    "type": "PERSON_FOUND",
                    "label": "Person identified",
                    "detail": source_label(o.source_key, o.source_type.value),
                    "source_url": o.source_url,
                }
            )
        for em in (await s.scalars(sa.select(Email).where(Email.person_id == entity_id))).all():
            events.append(
                {
                    "at": em.created_at,
                    "type": "EMAIL_FOUND",
                    "label": "Email found",
                    "detail": f"{em.address} ({em.discovery_method.value.replace('_', ' ')})",
                }
            )
            for ch in (await s.scalars(sa.select(EmailCheck).where(EmailCheck.email_id == em.id))).all():
                events.append(
                    {
                        "at": ch.checked_at,
                        "type": "EMAIL_VERIFIED",
                        "label": "Email verified",
                        "detail": ch.status.value,
                    }
                )
        for pev, cname in (
            await s.execute(
                sa.select(PersonDiscoveryEvent, Campaign.name)
                .join(Campaign, Campaign.id == PersonDiscoveryEvent.campaign_id)
                .where(PersonDiscoveryEvent.person_id == entity_id)
            )
        ).all():
            if pev.outcome.value not in ("qualified",):
                events.append(
                    {
                        "at": pev.observed_at,
                        "type": "CANDIDATE",
                        "label": f"Evaluated ({pev.outcome.value.replace('_', ' ')})",
                        "detail": f"{cname}: {pev.reason or ''}".strip(": "),
                    }
                )
    if company_id:
        for run in (
            await s.scalars(
                sa.select(WebsiteCrawlRun)
                .where(WebsiteCrawlRun.company_id == company_id)
                .order_by(WebsiteCrawlRun.started_at)
            )
        ).all():
            events.append(
                {
                    "at": run.started_at,
                    "type": "CRAWLED",
                    "label": "Website crawled",
                    "detail": f"{run.pages_fetched} pages · {run.status}"
                    + (f" · {run.error}" if run.error else ""),
                }
            )
        for ev, cname in (
            await s.execute(
                sa.select(CompanyDiscoveryEvent, Campaign.name)
                .join(Campaign, Campaign.id == CompanyDiscoveryEvent.campaign_id)
                .where(CompanyDiscoveryEvent.company_id == company_id)
            )
        ).all():
            if entity_type == EntityType.company or ev.outcome.value != "qualified":
                events.append(
                    {
                        "at": ev.observed_at,
                        "type": "COMPANY_CANDIDATE",
                        "label": f"Company evaluated ({ev.outcome.value.replace('_', ' ')})",
                        "detail": f"{cname} · via {source_label(ev.source_key, None)}"
                        + (f" · {ev.reason}" if ev.reason else ""),
                    }
                )
        cols = dict(
            (
                await s.execute(
                    sa.select(CustomColumn.id, CustomColumn.name).where(
                        CustomColumn.workspace_id == workspace_id
                    )
                )
            ).all()
        )
        for v in (
            await s.scalars(
                sa.select(CustomFieldValue).where(
                    CustomFieldValue.entity_id.in_([entity_id, company_id]),
                    CustomFieldValue.observed_at.isnot(None),
                )
            )
        ).all():
            events.append(
                {
                    "at": v.observed_at,
                    "type": "ENRICHED_COLUMN",
                    "label": f"Enriched {cols.get(v.column_id, 'column')}",
                    "detail": f"{v.display_value or v.status.value}",
                }
            )
    events.sort(key=lambda e: e["at"] or datetime.min.replace(tzinfo=UTC))
    return events


async def lead_campaigns(
    s: AsyncSession, workspace_id: uuid.UUID, entity_type: EntityType, entity_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Which campaigns produced this lead, and when it was first scraped (spec §184)."""
    rows = (
        await s.execute(
            sa.select(Campaign.id, Campaign.name, Campaign.status, sa.func.min(LeadExposure.occurred_at))
            .join(LeadExposure, LeadExposure.campaign_id == Campaign.id)
            .where(
                LeadExposure.workspace_id == workspace_id,
                LeadExposure.entity_type == entity_type,
                LeadExposure.entity_id == entity_id,
                LeadExposure.exposure_type == ExposureType.DISCOVERED,
            )
            .group_by(Campaign.id)
        )
    ).all()
    return [{"id": i, "name": n, "status": st.value, "first_discovered_at": at} for i, n, st, at in rows]


EDITABLE_PERSON_FIELDS = {"full_name", "job_title", "public_profile_url", "location", "phone"}
EDITABLE_COMPANY_FIELDS = {
    "name",
    "website_url",
    "description",
    "city",
    "country",
    "industry",
    "phone",
    "employee_count",
    "linkedin_url",
}


async def edit_field(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    entity_type: EntityType,
    entity_id: uuid.UUID,
    field: str,
    value: Any,
    *,
    user_id: str,
) -> Any:
    """Human override (spec §156): stored as source=user, strongest trust, never silently overwritten."""
    ev = registry.Evidence(
        source_type=SourceType.user,
        confidence=1.0,
        source_key="user",
        evidence=f"Edited by user {user_id}",
        user_confirmed=True,
    )
    if entity_type == EntityType.person:
        if field == "email":
            return await _set_person_email(s, workspace_id, entity_id, str(value), user_id=user_id)
        if field not in EDITABLE_PERSON_FIELDS:
            raise ValidationFailed(f"Field '{field}' is not editable")
        p = await s.get(Person, entity_id)
        if p is None or p.workspace_id != workspace_id:
            raise NotFound("Person not found")
        old = getattr(p, field if field != "job_title" else "job_title", None)
        from scout.learning.feedback import person_field_overridden

        await person_field_overridden(s, p, field, value)  # source that produced `old` → wrong (never raises)
        await registry.observe_person(s, workspace_id, p, {field: value}, ev)
        if field == "job_title":
            from scout.extract.titles import normalize_title

            info = normalize_title(str(value))
            p.normalized_title, p.role_family, p.seniority = (
                info.normalized_title,
                info.role_family,
                info.seniority,
            )
            p.department, p.decision_power = info.department, info.decision_power
        return old
    if field not in EDITABLE_COMPANY_FIELDS:
        raise ValidationFailed(f"Field '{field}' is not editable")
    if field == "website_url" and value:
        # User-supplied URL that the crawler will fetch: refuse internal targets up front (the crawler's
        # connect-time checks still apply to whatever the name resolves to later).
        from scout.crawl.ssrf import SSRFBlocked, validate_url
        from scout.util.urls import normalize_website

        url = normalize_website(str(value)) or str(value)
        try:
            validate_url(url)
        except SSRFBlocked as exc:
            raise ValidationFailed(f"Website URL not allowed: {exc}") from exc
        value = url
    c = await s.get(Company, entity_id)
    if c is None or c.workspace_id != workspace_id:
        raise NotFound("Company not found")
    old = (
        getattr(c, field, None)
        if field != "employee_count"
        else {"min": c.employee_min, "max": c.employee_max}
    )
    await registry.observe_company(s, workspace_id, c, {field: value}, ev)
    return old


async def _set_person_email(
    s: AsyncSession, workspace_id: uuid.UUID, person_id: uuid.UUID, address: str, *, user_id: str
) -> str | None:
    from scout.db.enums import EmailDiscoveryMethod, EmailKind, EmailStatus

    p = await s.get(Person, person_id)
    if p is None or p.workspace_id != workspace_id:
        raise NotFound("Person not found")
    address = address.strip().lower()
    if "@" not in address:
        raise ValidationFailed("Invalid email address")
    old = None
    old_e = None
    if p.primary_email_id:
        old_e = await s.get(Email, p.primary_email_id)
        old = old_e.address if old_e else None
    from scout.learning.feedback import email_overridden

    await email_overridden(
        s, old_e, address
    )  # the user's correction judges the resolver that found the old one
    if old_e is not None:
        old_e.is_primary = False
    e = await s.scalar(sa.select(Email).where(Email.workspace_id == workspace_id, Email.address == address))
    if e is None:
        local, domain = address.split("@", 1)
        e = Email(
            workspace_id=workspace_id,
            person_id=p.id,
            company_id=p.company_id,
            address=address,
            local_part=local,
            domain=domain,
            kind=EmailKind.person,
            discovery_method=EmailDiscoveryMethod.user,
            status=EmailStatus.UNKNOWN,
            overall_confidence=0.9,
            is_user_confirmed=True,
        )
        s.add(e)
        await s.flush()
    e.is_primary = True
    e.is_user_confirmed = True
    p.primary_email_id = e.id
    await _teach_domain(e.domain, p, address, workspace_id)
    return old


async def _teach_domain(domain: str | None, p: Person, address: str, workspace_id: uuid.UUID) -> None:
    """A user-confirmed address is the strongest evidence of the domain's convention (email engine samples)."""
    if not domain:
        return
    try:
        from scout.db.enums import EmailEvidenceSource
        from scout.email.contracts import ObservedEmail
        from scout.email.intel.samples import record_observed_emails

        local = address.split("@", 1)[0]
        sample = ObservedEmail(
            address=address,
            local_part=local,
            source=EmailEvidenceSource.user,
            first_name=p.first_name,
            last_name=p.last_name,
            confidence=0.99,
        )
        await record_observed_emails(domain, [sample], workspace_id=workspace_id)
    except Exception as exc:  # learning must never block a user edit
        log.info("email.user_sample_failed", domain=domain, error=str(exc))


async def approve_review(
    s: AsyncSession, workspace_id: uuid.UUID, entity_type: EntityType, ids: list[uuid.UUID], *, user_id: str
) -> int:
    """Manual approval (spec §212): raises confidence, records a user observation, clears the review flag."""

    def evidence() -> registry.Evidence:
        return registry.Evidence(
            source_type=SourceType.user,
            confidence=1.0,
            source_key="user",
            evidence=f"Approved by {user_id}",
            user_confirmed=True,
        )

    if entity_type == EntityType.person:
        people = (
            await s.scalars(sa.select(Person).where(Person.workspace_id == workspace_id, Person.id.in_(ids)))
        ).all()
        from scout.learning.feedback import people_approved

        await people_approved(s, people)  # sources holding the approved name/title → correct (never raises)
        for p in people:
            await registry.observe_person(
                s,
                workspace_id,
                p,
                {"full_name": p.full_name, **({"job_title": p.job_title} if p.job_title else {})},
                evidence(),
            )
            p.identity_confidence = max(p.identity_confidence or 0, 0.97)
            p.needs_review = False
            p.has_conflicts = False
        return len(people)
    companies = (
        await s.scalars(sa.select(Company).where(Company.workspace_id == workspace_id, Company.id.in_(ids)))
    ).all()
    for c in companies:
        await registry.observe_company(s, workspace_id, c, {"name": c.name}, evidence())
        c.company_confidence = max(c.company_confidence or 0, 0.97)
        c.needs_review = False
        c.has_conflicts = False
    return len(companies)
