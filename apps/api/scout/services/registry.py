"""Global lead registry (spec §19–25, §52–60, §103, §140, §156–157).

Canonical companies and people per workspace, field-level provenance, conflict detection,
discovery-history counters and lead exposures. Removing list memberships never touches this.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import CompanyStatus, EntityType, ExposureType, SourceType
from scout.db.models import (
    Company,
    CompanyFieldObservation,
    LeadExposure,
    Person,
    PersonEmployment,
    PersonFieldObservation,
)
from scout.errors import ValidationFailed
from scout.extract.names import looks_like_company_name
from scout.util.text import normalize_company_name, normalize_person_name, title_case_name
from scout.util.urls import is_company_domain, normalize_website, registrable_domain

# Source quality by evidence type (spec §102). Feeds the field resolver.
SOURCE_TYPE_QUALITY: dict[SourceType, float] = {
    SourceType.user: 1.0,
    SourceType.website: 0.95,
    SourceType.registry: 0.95,
    SourceType.tech_scan: 0.9,
    SourceType.public_profile: 0.85,
    SourceType.import_: 0.8,
    SourceType.maps: 0.75,
    SourceType.directory: 0.75,
    SourceType.ai_extraction: 0.75,
    SourceType.grounded_search: 0.7,
    SourceType.derived: 0.7,
    SourceType.search_snippet: 0.6,
}

FRESHNESS_HALF_LIFE_DAYS = 180.0

COMPANY_FIELDS = {
    "name",
    "website_url",
    "description",
    "country",
    "region",
    "city",
    "postal_code",
    "address",
    "latitude",
    "longitude",
    "industry",
    "sub_industry",
    "category_raw",
    "employee_count",
    "phone",
    "linkedin_url",
    "registry_id",
    "founded_year",
    "status",
}
PERSON_FIELDS = {"full_name", "job_title", "public_profile_url", "location", "phone"}


@dataclass
class Evidence:
    """Provenance for an observation. Required for every person and every factual field."""

    source_type: SourceType
    confidence: float
    source_key: str | None = None
    source_url: str | None = None
    evidence: str | None = None
    source_title: str | None = None
    page_id: uuid.UUID | None = None
    observed_at: datetime | None = None
    user_confirmed: bool = False

    def score(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        age_days = max(0.0, ((now - (self.observed_at or now)).total_seconds()) / 86400.0)
        decay = math.pow(0.5, age_days / FRESHNESS_HALF_LIFE_DAYS)
        return SOURCE_TYPE_QUALITY.get(self.source_type, 0.5) * max(0.0, min(1.0, self.confidence)) * decay


@dataclass
class CompanyInput:
    name: str
    website: str | None = None
    domain: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    industry: str | None = None
    sub_industry: str | None = None
    category_raw: str | None = None
    employee_min: int | None = None
    employee_max: int | None = None
    employee_confidence: float | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    registry_source: str | None = None
    registry_id: str | None = None
    description: str | None = None
    founded_year: int | None = None
    status: CompanyStatus | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def company_key(domain: str | None, name: str, city: str | None) -> str:
    """Candidate key used for in-campaign dedupe and reservations."""
    if domain:
        return f"d:{domain}"
    return f"n:{normalize_company_name(name)}|{(city or '').strip().lower()}"


def canonical_domain(website: str | None, domain: str | None = None) -> str | None:
    d = registrable_domain(domain or website) if (domain or website) else None
    return d if d and is_company_domain(d) else None


# ---------------------------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------------------------


async def find_company(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    domain: str | None = None,
    registry_source: str | None = None,
    registry_id: str | None = None,
    name: str | None = None,
    city: str | None = None,
) -> Company | None:
    """Identity resolution: domain > official identifier > fuzzy name + same city (only without stronger ids)."""
    if domain:
        c = await s.scalar(
            sa.select(Company).where(
                Company.workspace_id == workspace_id, Company.normalized_domain == domain
            )
        )
        if c:
            return c
    if registry_id:
        # Same official identifier: the source must agree when both sides know it (a SIREN read on a legal
        # notice before the registry saw the company has no source yet).
        source_ok = (
            sa.or_(Company.registry_source == registry_source, Company.registry_source.is_(None))
            if registry_source
            else sa.true()
        )
        c = await s.scalar(
            sa.select(Company)
            .where(Company.workspace_id == workspace_id, Company.registry_id == registry_id, source_ok)
            .order_by(Company.registry_source.is_(None), Company.created_at)
            .limit(1)
        )
        if c:
            return c
    if not domain and not registry_id and name and city:
        norm = normalize_company_name(name)
        c = await s.scalar(
            sa.select(Company)
            .where(
                Company.workspace_id == workspace_id,
                sa.func.lower(Company.city) == city.strip().lower(),
                sa.func.similarity(Company.normalized_name, norm) >= 0.92,
            )
            .order_by(sa.func.similarity(Company.normalized_name, norm).desc())
            .limit(1)
        )
        if c:
            return c
    return None


def _company_facts(data: CompanyInput) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for f in (
        "name",
        "description",
        "country",
        "region",
        "city",
        "postal_code",
        "address",
        "latitude",
        "longitude",
        "industry",
        "sub_industry",
        "category_raw",
        "phone",
        "linkedin_url",
        "registry_id",
        "founded_year",
    ):
        v = getattr(data, f)
        if v not in (None, ""):
            facts[f] = v
    website = normalize_website(data.website)
    if website:
        facts["website_url"] = website
    if data.employee_min is not None or data.employee_max is not None:
        facts["employee_count"] = {"min": data.employee_min, "max": data.employee_max}
    if data.status is not None:
        facts["status"] = data.status.value
    return facts


async def upsert_company(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    data: CompanyInput,
    evidence: Evidence,
    *,
    campaign_id: uuid.UUID | None = None,
    existing: Company | None = None,
) -> tuple[Company, bool]:
    """Find-or-create the canonical company, record field observations and refresh canonical values."""
    domain = canonical_domain(data.website, data.domain)
    company = existing or await find_company(
        s,
        workspace_id,
        domain=domain,
        registry_source=data.registry_source,
        registry_id=data.registry_id,
        name=data.name,
        city=data.city,
    )
    created = False
    now = datetime.now(UTC)
    if company is None:
        company = Company(
            workspace_id=workspace_id,
            name=data.name.strip(),
            normalized_name=normalize_company_name(data.name),
            domain=domain,
            normalized_domain=domain,
            website_url=normalize_website(data.website) or (f"https://{domain}/" if domain else None),
            registry_source=data.registry_source if data.registry_id else None,
            registry_id=data.registry_id,
            first_campaign_id=campaign_id,
            last_campaign_id=campaign_id,
        )
        s.add(company)
        await s.flush()
        created = True
    else:
        company.last_seen_at = now
        if campaign_id:
            company.last_campaign_id = campaign_id
            if company.first_campaign_id is None:
                company.first_campaign_id = campaign_id
        if domain and not company.normalized_domain:
            taken = await s.scalar(
                sa.select(Company.id).where(
                    Company.workspace_id == workspace_id, Company.normalized_domain == domain
                )
            )
            if taken is None:
                company.domain = company.normalized_domain = domain
        if data.registry_id and not company.registry_id:
            company.registry_source, company.registry_id = data.registry_source, data.registry_id
    facts = _company_facts(data)
    if facts:
        await observe_company(s, workspace_id, company, facts, evidence, campaign_id=campaign_id)
    return company, created


async def observe_company(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    company: Company,
    facts: dict[str, Any],
    evidence: Evidence,
    *,
    campaign_id: uuid.UUID | None = None,
) -> None:
    """Store observations (field-level provenance) and re-resolve the affected canonical columns."""
    observed_at = evidence.observed_at or datetime.now(UTC)
    for field_name, value in facts.items():
        if field_name not in COMPANY_FIELDS or value in (None, "", [], {}):
            continue
        s.add(
            CompanyFieldObservation(
                workspace_id=workspace_id,
                company_id=company.id,
                field_name=field_name,
                value_json=value,
                source_type=evidence.source_type,
                source_key=evidence.source_key,
                source_url=evidence.source_url,
                source_title=evidence.source_title,
                page_id=evidence.page_id,
                evidence=(evidence.evidence or "")[:2000] or None,
                confidence=evidence.confidence,
                is_user_confirmed=evidence.user_confirmed,
                observed_at=observed_at,
                campaign_id=campaign_id,
            )
        )
    await s.flush()
    await resolve_company_fields(s, company, [f for f in facts if f in COMPANY_FIELDS])


def _values_agree(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return (
            normalize_company_name(a) == normalize_company_name(b) or a.strip().lower() == b.strip().lower()
        )
    return a == b


def _pick_best(observations: Sequence[Any]) -> tuple[Any, float, bool]:
    """Choose the current best value: user-confirmed > quality × confidence × freshness (+ agreement)."""
    if not observations:
        return None, 0.0, False
    now = datetime.now(UTC)
    user = [o for o in observations if o.is_user_confirmed]
    if user:
        best = max(user, key=lambda o: o.observed_at)
        return best.value_json, 1.0, False
    scored: list[tuple[float, Any]] = []
    for o in observations:
        ev = Evidence(source_type=o.source_type, confidence=o.confidence, observed_at=o.observed_at)
        scored.append((ev.score(now), o))
    # agreement bonus: values corroborated by several observations
    totals: list[tuple[float, Any]] = []
    for sc, o in scored:
        agree = sum(s2 for s2, o2 in scored if o2 is not o and _values_agree(o2.value_json, o.value_json))
        totals.append((sc + 0.25 * agree, o))
    totals.sort(key=lambda t: t[0], reverse=True)
    best_score, best = totals[0]
    # conflict: another credible observation disagrees
    conflict = any(
        sc >= 0.6
        and not _values_agree(o.value_json, best.value_json)
        and o.field_name not in ("description",)
        for sc, o in scored
    )
    return best.value_json, min(1.0, best_score), conflict


async def resolve_company_fields(s: AsyncSession, company: Company, fields: Iterable[str]) -> None:
    fields = [f for f in set(fields) if f in COMPANY_FIELDS]
    if not fields:
        return
    rows = (
        await s.scalars(
            sa.select(CompanyFieldObservation).where(
                CompanyFieldObservation.company_id == company.id,
                CompanyFieldObservation.field_name.in_(fields),
            )
        )
    ).all()
    by_field: dict[str, list[CompanyFieldObservation]] = {}
    for r in rows:
        by_field.setdefault(r.field_name, []).append(r)
    any_conflict = company.has_conflicts
    for f, obs in by_field.items():
        value, score, conflict = _pick_best(obs)
        any_conflict = any_conflict or conflict
        if value is None:
            continue
        if f == "employee_count" and isinstance(value, dict):
            company.employee_min = value.get("min")
            company.employee_max = value.get("max")
            company.employee_confidence = round(score, 3)
        elif f == "website_url":
            company.website_url = value
        elif f == "status":
            company.status = (
                CompanyStatus(value) if value in CompanyStatus._value2member_map_ else company.status
            )
        elif f == "registry_id":
            if not company.registry_id:
                await _set_registry_id(s, company, str(value))
        elif f == "name":
            company.name = str(value)
            company.normalized_name = normalize_company_name(str(value))
        elif f == "country":
            company.country = str(value).upper()[:2]
        elif f in ("latitude", "longitude"):
            setattr(company, f, float(value))
        elif f == "founded_year":
            try:
                company.founded_year = int(value)
            except (TypeError, ValueError):
                pass
        else:
            setattr(company, f, value)
    company.has_conflicts = any_conflict
    if any_conflict:
        company.needs_review = True


def infer_registry_source(registry_id: str, country: str | None) -> str | None:
    """Registry an identifier belongs to, when unambiguous (a 9-digit French SIREN)."""
    if re.fullmatch(r"\d{9}", registry_id) and (country or "FR").upper() == "FR":
        return "fr_sirene"
    return None


async def _set_registry_id(s: AsyncSession, company: Company, registry_id: str) -> None:
    """Adopt an observed official identifier unless another company of the workspace already owns it
    (that is a potential duplicate: flagged for review, never merged silently)."""
    source = company.registry_source or infer_registry_source(registry_id, company.country)
    owner = await s.scalar(
        sa.select(Company.id).where(
            Company.workspace_id == company.workspace_id,
            Company.registry_id == registry_id,
            Company.registry_source.is_not_distinct_from(source),
            Company.id != company.id,
        )
    )
    if owner is not None:
        company.needs_review = True
        return
    company.registry_id = registry_id
    company.registry_source = source


# ---------------------------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------------------------


def person_key(company_id: uuid.UUID | None, full_name: str, profile_url: str | None = None) -> str:
    if profile_url:
        return f"p:{profile_url.lower().rstrip('/')}"
    return f"c:{company_id}|{normalize_person_name(full_name)}"


async def find_person(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    company_id: uuid.UUID | None,
    full_name: str,
    profile_url: str | None = None,
    email: str | None = None,
) -> Person | None:
    """Strong identity signals only: verified email > public profile > company + normalized name (spec §24)."""
    if email:
        from scout.db.models import Email

        p = await s.scalar(
            sa.select(Person)
            .join(Email, Email.person_id == Person.id)
            .where(Email.workspace_id == workspace_id, Email.address == email.lower())
            .limit(1)
        )
        if p:
            return p
    if profile_url:
        p = await s.scalar(
            sa.select(Person).where(
                Person.workspace_id == workspace_id, Person.public_profile_url == profile_url
            )
        )
        if p:
            return p
    if company_id:
        return await s.scalar(
            sa.select(Person).where(
                Person.workspace_id == workspace_id,
                Person.company_id == company_id,
                Person.normalized_name == normalize_person_name(full_name),
            )
        )
    return None


async def upsert_person(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    company_id: uuid.UUID | None,
    full_name: str,
    evidence: Evidence,
    first_name: str | None = None,
    last_name: str | None = None,
    job_title: str | None = None,
    title_info: Any | None = None,  # scout.extract.types.TitleInfo
    profile_url: str | None = None,
    location: str | None = None,
    email: str | None = None,
    campaign_id: uuid.UUID | None = None,
) -> tuple[Person, bool]:
    """Find-or-create a person. Evidence is mandatory: no person exists because a model guessed a name."""
    if evidence is None or not (evidence.source_url or evidence.evidence) or evidence.confidence <= 0:
        raise ValidationFailed("A person requires supporting evidence (source URL or verbatim evidence)")
    full_name = title_case_name(full_name) if full_name.isupper() else full_name.strip()
    if not full_name:
        raise ValidationFailed("Empty person name")
    # Company ≠ person (spec §199): an organisation name is never stored as a person, whatever the path
    # (extraction, registry directors, import, AI tool).
    company_name = (
        await s.scalar(sa.select(Company.name).where(Company.id == company_id)) if company_id else None
    )
    if looks_like_company_name(full_name, company_name):
        raise ValidationFailed(f"'{full_name}' looks like a company name, not a person")
    person = await find_person(
        s, workspace_id, company_id=company_id, full_name=full_name, profile_url=profile_url, email=email
    )
    created = False
    now = datetime.now(UTC)
    if person is None:
        parts = full_name.split()
        new = Person(
            workspace_id=workspace_id,
            company_id=company_id,
            full_name=full_name,
            normalized_name=normalize_person_name(full_name),
            first_name=first_name or (parts[0] if parts else None),
            last_name=last_name or (" ".join(parts[1:]) if len(parts) > 1 else None),
            first_campaign_id=campaign_id,
            last_campaign_id=campaign_id,
            identity_confidence=round(evidence.confidence, 3),
        )
        await s.flush()  # nothing else pending may be caught in the savepoint below
        try:
            # Savepoint: a concurrent campaign may insert the same person (same company + name or profile)
            # between our lookup and this insert; then we reuse its row instead of failing the job.
            async with s.begin_nested():
                s.add(new)
                await s.flush()
            person, created = new, True
        except IntegrityError:
            person = await find_person(
                s,
                workspace_id,
                company_id=company_id,
                full_name=full_name,
                profile_url=profile_url,
                email=email,
            )
            if person is None:
                raise
    if not created:
        person.last_seen_at = now
        if campaign_id:
            person.last_campaign_id = campaign_id
            person.first_campaign_id = person.first_campaign_id or campaign_id
        # corroboration raises identity confidence (bounded)
        prev = person.identity_confidence or 0.0
        person.identity_confidence = round(
            min(0.98, max(prev, evidence.confidence) + (0.03 if prev else 0.0)), 3
        )
    if title_info is not None:
        person.normalized_title = title_info.normalized_title
        person.role_family = title_info.role_family
        person.seniority = title_info.seniority
        person.department = title_info.department
        person.decision_power = title_info.decision_power
    facts: dict[str, Any] = {"full_name": full_name}
    if job_title:
        facts["job_title"] = job_title
    if profile_url:
        facts["public_profile_url"] = profile_url
    if location:
        facts["location"] = location
    await observe_person(s, workspace_id, person, facts, evidence, campaign_id=campaign_id)
    if company_id:
        stmt = (
            pg_insert(PersonEmployment)
            .values(
                workspace_id=workspace_id,
                person_id=person.id,
                company_id=company_id,
                title=job_title,
                is_current=True,
                source_key=evidence.source_key,
            )
            .on_conflict_do_update(
                index_elements=["person_id", "company_id"],
                set_={
                    "title": sa.func.coalesce(job_title, PersonEmployment.title),
                    "observed_at": sa.func.now(),
                },
            )
        )
        await s.execute(stmt)
    return person, created


async def observe_person(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    person: Person,
    facts: dict[str, Any],
    evidence: Evidence,
    *,
    campaign_id: uuid.UUID | None = None,
) -> None:
    observed_at = evidence.observed_at or datetime.now(UTC)
    for field_name, value in facts.items():
        if field_name not in PERSON_FIELDS or value in (None, ""):
            continue
        s.add(
            PersonFieldObservation(
                workspace_id=workspace_id,
                person_id=person.id,
                field_name=field_name,
                value_json=value,
                source_type=evidence.source_type,
                source_key=evidence.source_key,
                source_url=evidence.source_url,
                source_title=evidence.source_title,
                page_id=evidence.page_id,
                evidence=(evidence.evidence or "")[:2000] or None,
                confidence=evidence.confidence,
                is_user_confirmed=evidence.user_confirmed,
                observed_at=observed_at,
                campaign_id=campaign_id,
            )
        )
    await s.flush()
    rows = (
        await s.scalars(
            sa.select(PersonFieldObservation).where(
                PersonFieldObservation.person_id == person.id,
                PersonFieldObservation.field_name.in_(list(facts.keys())),
            )
        )
    ).all()
    by_field: dict[str, list[PersonFieldObservation]] = {}
    for r in rows:
        by_field.setdefault(r.field_name, []).append(r)
    conflict_any = person.has_conflicts
    for f, obs in by_field.items():
        value, _score, conflict = _pick_best(obs)
        if f == "job_title":
            conflict_any = conflict_any or conflict
        if value is None:
            continue
        if f == "full_name":
            person.full_name = str(value)
        elif f == "job_title":
            person.job_title = str(value)
        elif f == "public_profile_url":
            person.public_profile_url = str(value)
        elif f == "location":
            person.location = str(value)
        elif f == "phone":
            person.phone = str(value)
    person.has_conflicts = conflict_any
    if conflict_any:
        person.needs_review = True


# ---------------------------------------------------------------------------------------------
# Exposures & discovery history
# ---------------------------------------------------------------------------------------------


async def record_exposures(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    exposure_type: ExposureType,
    *,
    person_ids: Sequence[uuid.UUID] = (),
    company_ids: Sequence[uuid.UUID] = (),
    campaign_id: uuid.UUID | None = None,
    list_id: uuid.UUID | None = None,
    import_id: uuid.UUID | None = None,
    export_id: uuid.UUID | None = None,
) -> None:
    """Append exposures for people (and their companies) and companies; update history counters."""
    person_ids = list(dict.fromkeys(person_ids))
    company_ids = list(dict.fromkeys(company_ids))
    extra = {"campaign_id": campaign_id, "list_id": list_id, "import_id": import_id, "export_id": export_id}
    if person_ids:
        rows = (
            await s.execute(
                sa.select(Person.id, Person.company_id).where(
                    Person.workspace_id == workspace_id, Person.id.in_(person_ids)
                )
            )
        ).all()
        if rows:
            await s.execute(
                sa.insert(LeadExposure),
                [
                    {
                        "workspace_id": workspace_id,
                        "entity_type": EntityType.person,
                        "entity_id": pid,
                        "company_id": cid,
                        "exposure_type": exposure_type,
                        **extra,
                    }
                    for pid, cid in rows
                ],
            )
            company_ids = list(dict.fromkeys([*company_ids, *[cid for _, cid in rows if cid]]))
            await _bump_history(s, Person, [pid for pid, _ in rows], exposure_type, campaign_id)
    if company_ids:
        owned = (
            await s.scalars(
                sa.select(Company.id).where(Company.workspace_id == workspace_id, Company.id.in_(company_ids))
            )
        ).all()
        if owned:
            await s.execute(
                sa.insert(LeadExposure),
                [
                    {
                        "workspace_id": workspace_id,
                        "entity_type": EntityType.company,
                        "entity_id": cid,
                        "company_id": cid,
                        "exposure_type": exposure_type,
                        **extra,
                    }
                    for cid in owned
                ],
            )
            await _bump_history(s, Company, list(owned), exposure_type, campaign_id)


async def _bump_history(
    s: AsyncSession,
    model: type[Company] | type[Person],
    ids: list[uuid.UUID],
    exposure_type: ExposureType,
    campaign_id: uuid.UUID | None,
) -> None:
    if not ids:
        return
    values: dict[str, Any] = {}
    if exposure_type in (ExposureType.DISCOVERED, ExposureType.IMPORTED):
        values = {"times_discovered": model.times_discovered + 1, "last_seen_at": sa.func.now()}
        if campaign_id:
            values["last_campaign_id"] = campaign_id
            values["first_campaign_id"] = sa.func.coalesce(model.first_campaign_id, campaign_id)
    elif exposure_type == ExposureType.EXPORTED:
        values = {"times_exported": model.times_exported + 1, "last_exported_at": sa.func.now()}
    elif exposure_type == ExposureType.ADDED_TO_LIST:
        values = {"times_added_to_lists": model.times_added_to_lists + 1}
    elif exposure_type == ExposureType.CONTACTED:
        values = {"contacted_at": sa.func.now()}
    elif exposure_type == ExposureType.ENRICHED:
        values = {"last_enriched_at": sa.func.now()}
    if values:
        await s.execute(sa.update(model).where(model.id.in_(ids)).values(**values))
