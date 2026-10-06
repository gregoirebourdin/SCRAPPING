"""What the engine has (registry mode) or produces now (live mode) for one ground-truth item.

Both collectors return the same ``actual`` shape consumed by ``metrics.compare_item``::

    {company: {found, id, name, domain} | None,
     people: [{id, first, last, full_name, title, source, email: {address, status, resolver, pattern} | None}],
     company_emails: [{address, status}],
     enrichment: {key: {value, status, resolver, data_type}},
     domain: {catch_all, pattern},
     email_resolutions: [{person, ms, deep, cache_hit, cost_usd}],
     crawl: {attempted, pages, status} | None}

Provenance (person source, e-mail resolver / pattern, cell resolver) rides along for learning feedback.

Live mode never hands expected values to the engine: only the item's ``input`` (domain or name, and the
people's names for e-mail datasets). Engine output lands in the workspace registry as in a normal run
(companies, people, e-mails); no lead exposure, list membership or campaign is created.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from scout.benchmark.metrics import name_tokens, names_match, norm_domain
from scout.db.engine import session_scope
from scout.db.enums import EmailResolutionPath, EntityType, SourceType, VerificationRequestStatus
from scout.db.models import (
    Company,
    CustomColumn,
    CustomFieldValue,
    DomainDnsCache,
    DomainEmailPattern,
    DomainProfile,
    Email,
    EmailResolution,
    EmailVerificationRequest,
    Person,
    PersonFieldObservation,
)
from scout.util.text import normalize_company_name

log = structlog.get_logger("benchmark.collect")

MAX_PEOPLE_SCAN = 200
MAX_COMPANY_EMAILS = 500


@dataclass
class LiveConfig:
    """Live-mode knobs (validated by the API; defaults are the cheap, deterministic path)."""

    crawl: bool = True
    crawl_max_age_days: int = 30  # cache-first: a fresh cached crawl costs nothing
    resolve_website: bool = True  # name-only inputs: find the website
    people_ai: bool = False  # AI people extraction when deterministic extraction finds nobody
    max_people: int = 10
    email: str = "fast"  # off | fast | fast_deep
    deep_wait_s: float = 90.0
    enrichment: bool = True
    enrichment_ai: bool = False  # allow AI planning / AI strategies for columns
    rel_tol: float = 0.05

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LiveConfig:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ItemContext:
    workspace_id: uuid.UUID
    kind: str
    item_input: Mapping[str, Any]
    expected: Mapping[str, Any]
    columns: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # dataset enrichment prompts
    label: str = ""
    budget_ok: Callable[[], Awaitable[bool]] | None = None


# =============================================================================================
# Shared snapshot of a company's registry state
# =============================================================================================


async def _person_sources(s: AsyncSession, person_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not person_ids:
        return {}
    rows = (
        await s.execute(
            sa.select(PersonFieldObservation.person_id, PersonFieldObservation.source_key)
            .where(
                PersonFieldObservation.person_id.in_(person_ids),
                PersonFieldObservation.field_name == "full_name",
            )
            .order_by(PersonFieldObservation.observed_at)
        )
    ).all()
    out: dict[uuid.UUID, str] = {}
    for pid, key in rows:
        if key and pid not in out:  # first observation = discovering source
            out[pid] = key
    return out


async def _best_emails(s: AsyncSession, person_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Email]:
    if not person_ids:
        return {}
    rows = (
        await s.scalars(
            sa.select(Email)
            .where(Email.person_id.in_(person_ids))
            .order_by(
                Email.person_id,
                Email.is_user_confirmed.desc(),
                Email.is_primary.desc(),
                Email.overall_confidence.desc().nullslast(),
            )
        )
    ).all()
    out: dict[uuid.UUID, Email] = {}
    for e in rows:
        if e.person_id is not None and e.person_id not in out:
            out[e.person_id] = e
    return out


async def _resolvers(s: AsyncSession, person_ids: Sequence[uuid.UUID]) -> dict[tuple[uuid.UUID, str], str]:
    if not person_ids:
        return {}
    rows = (
        await s.execute(
            sa.select(EmailResolution.person_id, EmailResolution.address, EmailResolution.resolver)
            .where(EmailResolution.person_id.in_(person_ids), EmailResolution.resolver.is_not(None))
            .order_by(EmailResolution.created_at)
        )
    ).all()
    return {(pid, addr): res for pid, addr, res in rows if pid and addr and res}


async def _domain_facts(s: AsyncSession, domain: str | None) -> dict[str, Any]:
    if not domain:
        return {"catch_all": None, "pattern": None}
    prof = await s.get(DomainProfile, domain)
    catch_all = prof.catch_all if prof else None
    pattern = prof.dominant_pattern if prof else None
    if pattern is None:
        pattern = await s.scalar(
            sa.select(DomainEmailPattern.pattern)
            .where(DomainEmailPattern.domain == domain)
            .order_by(DomainEmailPattern.confidence.desc(), DomainEmailPattern.supporting_samples.desc())
            .limit(1)
        )
    if catch_all is None:
        catch_all = await s.scalar(sa.select(DomainDnsCache.catch_all).where(DomainDnsCache.domain == domain))
    return {"catch_all": catch_all, "pattern": pattern}


def _status(v: Any) -> str | None:
    return str(getattr(v, "value", v)) if v is not None else None


async def snapshot_people(s: AsyncSession, people: Sequence[Person]) -> list[dict[str, Any]]:
    """People rows → actual people (with their best e-mail and discovering source)."""
    ids = [p.id for p in people]
    sources = await _person_sources(s, ids)
    emails = await _best_emails(s, ids)
    resolvers = await _resolvers(s, ids)
    out: list[dict[str, Any]] = []
    for p in people:
        e = emails.get(p.id)
        email = None
        if e is not None:
            resolver = resolvers.get((p.id, e.address)) or _status(e.discovery_method)
            email = {
                "address": e.address,
                "status": _status(e.status),
                "resolver": resolver,
                "pattern": e.pattern,
                "catch_all": e.catch_all,
                "user_confirmed": e.is_user_confirmed,
            }
        out.append(
            {
                "id": str(p.id),
                "first": p.first_name,
                "last": p.last_name,
                "full_name": p.full_name,
                "title": p.job_title,
                "source": sources.get(p.id),
                "email": email,
            }
        )
    return out


async def _column_for(s: AsyncSession, workspace_id: uuid.UUID, key: str) -> CustomColumn | None:
    k = key.strip().lower()
    cols = (
        await s.scalars(
            sa.select(CustomColumn)
            .where(
                CustomColumn.workspace_id == workspace_id,
                sa.or_(sa.func.lower(CustomColumn.slug) == k, sa.func.lower(CustomColumn.name) == k),
            )
            .order_by(CustomColumn.list_id.is_not(None), CustomColumn.created_at)
        )
    ).all()
    return cols[0] if cols else None


def _expected_person_ids(
    expected: Mapping[str, Any], actual_people: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Actual person ids matching expected people, in ground-truth order (person-level column lookup)."""
    ids: list[str] = []
    for e in expected.get("people") or []:
        et = name_tokens(e.get("first"), e.get("last"), e.get("full_name"))
        for a in actual_people:
            if names_match(et, name_tokens(a.get("first"), a.get("last"), a.get("full_name"))):
                ids.append(str(a["id"]))
                break
    return ids


async def registry_enrichment(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    keys: Sequence[str],
    company: Company,
    actual_people: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    person_ids = _expected_person_ids(expected, actual_people) or [str(a["id"]) for a in actual_people]
    for key in keys:
        col = await _column_for(s, workspace_id, key)
        if col is None:
            out[key] = {"status": "missing", "value": None, "reason": "No column with this slug or name"}
            continue
        if col.entity_type == EntityType.person:
            entity_ids = [uuid.UUID(x) for x in person_ids]
        else:
            entity_ids = [company.id]
        cells = (
            await s.scalars(
                sa.select(CustomFieldValue).where(
                    CustomFieldValue.column_id == col.id, CustomFieldValue.entity_id.in_(entity_ids)
                )
            )
        ).all()
        by_entity = {c.entity_id: c for c in cells}
        cell = next((by_entity[i] for i in entity_ids if i in by_entity), None)
        if cell is None:
            out[key] = {"status": "missing", "value": None, "data_type": _status(col.data_type)}
            continue
        out[key] = {
            "status": _status(cell.status),
            "value": cell.value_json,
            "resolver": cell.resolver,
            "data_type": _status(col.data_type),
            "user_override": cell.is_user_override,
        }
    return out


async def company_emails(s: AsyncSession, company_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await s.execute(
            sa.select(Email.address, Email.status)
            .where(Email.company_id == company_id)
            .limit(MAX_COMPANY_EMAILS)
        )
    ).all()
    return [{"address": a, "status": _status(st)} for a, st in rows]


async def _resolution_stats(
    s: AsyncSession, people: Sequence[Mapping[str, Any]], *, since: datetime | None
) -> list[dict[str, Any]]:
    """Per person: the latest resolution (fast or cache path) plus any deep path that followed it."""
    ids = [uuid.UUID(str(p["id"])) for p in people if p.get("id")]
    if not ids:
        return []
    q = sa.select(EmailResolution).where(EmailResolution.person_id.in_(ids))
    if since is not None:
        q = q.where(EmailResolution.created_at >= since)
    rows = (await s.scalars(q.order_by(EmailResolution.created_at))).all()
    by_person: dict[uuid.UUID, list[EmailResolution]] = {}
    for r in rows:
        if r.person_id is not None:
            by_person.setdefault(r.person_id, []).append(r)
    names = {uuid.UUID(str(p["id"])): p.get("full_name") for p in people if p.get("id")}
    out: list[dict[str, Any]] = []
    for pid, rs in by_person.items():
        heads = [i for i, r in enumerate(rs) if r.path != EmailResolutionPath.deep]
        group = rs[heads[-1] :] if heads else rs
        head = group[0]
        out.append(
            {
                "person": names.get(pid),
                "ms": sum(int(r.duration_ms or 0) for r in group),
                "deep": any(r.path == EmailResolutionPath.deep for r in group),
                "cache_hit": head.path == EmailResolutionPath.cache
                or any(bool(v) for v in (head.cache_hits or {}).values()),
                "cost_usd": float(sum(float(r.cost_usd or 0) for r in group)),
                "smtp_probes": sum(int(r.smtp_probes or 0) for r in group),
            }
        )
    return out


def _wants_email_stats(
    expected: Mapping[str, Any], people: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """People for whom an e-mail is expected (resolution timings are only meaningful there)."""
    targets = [name_tokens(e.get("first"), e.get("last")) for e in expected.get("emails") or []]
    targets = [t for t in targets if t]
    return [
        p
        for p in people
        if any(
            names_match(t, name_tokens(p.get("first"), p.get("last"), p.get("full_name"))) for t in targets
        )
    ]


# =============================================================================================
# Registry mode
# =============================================================================================


async def find_company(
    s: AsyncSession, workspace_id: uuid.UUID, company_input: Mapping[str, Any]
) -> Company | None:
    domain = norm_domain(company_input.get("domain"))
    if domain:
        return await s.scalar(
            sa.select(Company).where(
                Company.workspace_id == workspace_id, Company.normalized_domain == domain
            )
        )
    name = (company_input.get("name") or "").strip()
    if not name:
        return None
    norm = normalize_company_name(name)
    q = sa.select(Company).where(Company.workspace_id == workspace_id, Company.normalized_name == norm)
    city = (company_input.get("city") or "").strip().lower()
    if city:
        q = q.where(sa.func.lower(Company.city) == city)
    rows = (await s.scalars(q.order_by(Company.created_at).limit(2))).all()
    return rows[0] if len(rows) == 1 else None  # ambiguous names are not guessed


async def registry_actual(ctx: ItemContext) -> dict[str, Any]:
    """Compare against what the workspace already holds. Read-only, zero cost."""
    async with session_scope() as s:
        company = await find_company(s, ctx.workspace_id, ctx.item_input.get("company") or {})
        if company is None:
            return {"company": {"found": False}, "people": [], "company_emails": [], "enrichment": {}}
        people_rows = (
            await s.scalars(
                sa.select(Person)
                .where(Person.workspace_id == ctx.workspace_id, Person.company_id == company.id)
                .order_by(Person.created_at)
                .limit(MAX_PEOPLE_SCAN)
            )
        ).all()
        people = await snapshot_people(s, people_rows)
        keys = list((ctx.expected.get("enrichment") or {}).keys())
        enrichment = (
            await registry_enrichment(s, ctx.workspace_id, keys, company, people, ctx.expected)
            if keys
            else {}
        )
        resolutions = await _resolution_stats(s, _wants_email_stats(ctx.expected, people), since=None)
        return {
            "company": {
                "found": True,
                "id": str(company.id),
                "name": company.name,
                "domain": company.normalized_domain,
            },
            "people": people,
            "company_emails": await company_emails(s, company.id),
            "enrichment": enrichment,
            "domain": await _domain_facts(s, company.normalized_domain),
            "email_resolutions": resolutions,
        }


# =============================================================================================
# Live mode
# =============================================================================================


def _benchmark_evidence(label: str, confidence: float = 0.8) -> Any:
    from scout.services import registry

    return registry.Evidence(
        source_type=SourceType.import_,
        confidence=confidence,
        source_key="benchmark",
        evidence=f"Benchmark input ({label})"[:300],
    )


async def _live_company(ctx: ItemContext, cfg: LiveConfig, notes: list[str]) -> Company | None:
    from scout.services import registry

    ci = ctx.item_input.get("company") or {}
    domain = norm_domain(ci.get("domain"))
    website = f"https://{domain}/" if domain else None
    name = (ci.get("name") or "").strip()
    if not domain and name and cfg.resolve_website:
        from scout.crawl.resolve import resolve_website

        resolved = await resolve_website(name, city=ci.get("city"), country=ci.get("country"))
        if resolved is not None:
            domain, website = resolved.domain, resolved.url
        else:
            notes.append("website not found")
    if not domain:
        return None
    async with session_scope() as s:
        company, _ = await registry.upsert_company(
            s,
            ctx.workspace_id,
            registry.CompanyInput(
                name=name or domain.split(".")[0].replace("-", " ").title(),
                website=website,
                domain=domain,
                city=ci.get("city"),
                country=(ci.get("country") or "")[:2].upper() or None,
            ),
            _benchmark_evidence(ctx.label),
        )
        await s.flush()
        s.expunge(company)
        return company


async def _live_people(
    ctx: ItemContext, cfg: LiveConfig, company: Company, pages: Sequence[Any], notes: list[str]
) -> list[uuid.UUID]:
    from scout.extract.titles import normalize_title
    from scout.services import registry

    given = list(ctx.item_input.get("people") or [])
    out: list[uuid.UUID] = []
    if given:
        async with session_scope() as s:
            for p in given[: cfg.max_people * 5]:
                full = " ".join(x for x in (p.get("first"), p.get("last")) if x) or (p.get("full_name") or "")
                if not full.strip():
                    continue
                try:
                    async with s.begin_nested():
                        person, _ = await registry.upsert_person(
                            s,
                            ctx.workspace_id,
                            company_id=company.id,
                            full_name=full,
                            first_name=p.get("first"),
                            last_name=p.get("last"),
                            job_title=p.get("title"),
                            title_info=normalize_title(p["title"]) if p.get("title") else None,
                            evidence=_benchmark_evidence(ctx.label, 0.9),
                        )
                    out.append(person.id)
                except Exception as exc:
                    notes.append(f"person skipped: {exc}"[:200])
        return out
    if ctx.kind != "leads":
        return out
    from scout.extract.people import extract_people

    cands = (
        extract_people(pages, company_name=company.name, domain=company.normalized_domain) if pages else []
    )
    if not cands and pages and cfg.people_ai and (ctx.budget_ok is None or await ctx.budget_ok()):
        from scout.extract.ai_people import ai_extract_people
        from scout.services.usage import allow_expensive

        if await allow_expensive():
            try:
                cands = await ai_extract_people(pages, company_name=company.name)
            except Exception as exc:
                notes.append(f"AI people extraction failed: {exc}"[:200])
    cands = sorted(cands, key=lambda c: c.confidence, reverse=True)[: cfg.max_people]
    st_map = {
        "registry": SourceType.registry,
        "website": SourceType.website,
        "ai_extraction": SourceType.ai_extraction,
        "grounded_search": SourceType.grounded_search,
        "public_profile": SourceType.public_profile,
    }
    async with session_scope() as s:
        for c in cands:
            try:
                async with s.begin_nested():
                    person, _ = await registry.upsert_person(
                        s,
                        ctx.workspace_id,
                        company_id=company.id,
                        full_name=c.full_name,
                        first_name=c.first_name,
                        last_name=c.last_name,
                        job_title=c.title,
                        title_info=normalize_title(c.title) if c.title else None,
                        profile_url=c.profile_url,
                        email=c.email,
                        evidence=registry.Evidence(
                            source_type=st_map.get(c.source_type, SourceType.website),
                            confidence=c.confidence,
                            source_key="ai_extraction" if c.source_type == "ai_extraction" else "website",
                            source_url=c.source_url,
                            evidence=c.evidence,
                        ),
                    )
                out.append(person.id)
            except Exception as exc:  # e.g. a company name extracted as a person: rejected by the registry
                notes.append(f"candidate rejected: {exc}"[:200])
    return out


async def _wait_deep(workspace_id: uuid.UUID, person_ids: Sequence[uuid.UUID], timeout_s: float) -> bool:
    """Wait for this item's deep (SMTP) verifications to conclude; False on timeout."""
    if not person_ids:
        return True
    deadline = time.monotonic() + max(0.0, timeout_s)
    active = (
        VerificationRequestStatus.pending,
        VerificationRequestStatus.processing,
        VerificationRequestStatus.retry,
    )
    while True:
        async with session_scope() as s:
            n = await s.scalar(
                sa.select(sa.func.count())
                .select_from(EmailVerificationRequest)
                .where(
                    EmailVerificationRequest.workspace_id == workspace_id,
                    EmailVerificationRequest.person_id.in_(person_ids),
                    EmailVerificationRequest.status.in_(active),
                )
            )
        if not n:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(2.0)


async def _live_enrichment(
    ctx: ItemContext, cfg: LiveConfig, company: Company, pages: Sequence[Any], notes: list[str]
) -> dict[str, dict[str, Any]]:
    from scout.enrich.engine import plan_of, resolve_plan
    from scout.enrich.planner import plan_column

    keys = list((ctx.expected.get("enrichment") or {}).keys())
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        async with session_scope() as s:
            col = await _column_for(s, ctx.workspace_id, key)
        prompt = ctx.columns.get(key) or {}
        try:
            if col is not None:
                plan = plan_of(col)
            else:
                from scout.db.enums import ColumnDataType

                dt = prompt.get("data_type")
                plan = await plan_column(
                    key,
                    prompt.get("instruction"),
                    data_type=ColumnDataType(dt) if dt else None,
                    use_ai=cfg.enrichment_ai,
                )
            if plan.strategy in (
                "semantic_classifier",
                "ai_extraction",
                "generated_text",
                "web_research",
            ) and not (cfg.enrichment_ai and (ctx.budget_ok is None or await ctx.budget_ok())):
                out[key] = {
                    "status": "unknown",
                    "value": None,
                    "resolver": plan.strategy,
                    "reason": "AI strategies disabled",
                }
                continue
            if plan.entity_type == EntityType.person:
                out[key] = {
                    "status": "unknown",
                    "value": None,
                    "resolver": plan.strategy,
                    "reason": "person-level column",
                }
                continue
            res = await resolve_plan(ctx.workspace_id, plan, company=company, pages=list(pages))
            out[key] = {
                "status": _status(res.status),
                "value": res.value,
                "resolver": res.resolver or plan.strategy,
                "data_type": _status(plan.data_type),
                "cost_usd": res.cost_usd,
            }
        except Exception as exc:
            notes.append(f"enrichment {key}: {exc}"[:200])
            out[key] = {"status": "failed", "value": None, "reason": str(exc)[:200]}
    return out


async def live_actual(ctx: ItemContext, cfg: LiveConfig) -> dict[str, Any]:
    """Run the engine on one item (company → crawl → people → e-mail → enrichment) and snapshot the output."""
    from scout.crawl.cache import ensure_crawled

    notes: list[str] = []
    started_at = datetime.now(UTC)
    company = await _live_company(ctx, cfg, notes)
    if company is None:
        return {
            "company": {"found": False},
            "people": [],
            "company_emails": [],
            "enrichment": {},
            "notes": notes,
        }
    pages: list[Any] = []
    crawl = None
    if cfg.crawl:
        pages = list(await ensure_crawled(ctx.workspace_id, company.id, max_age_days=cfg.crawl_max_age_days))
        async with session_scope() as s:
            ws_status = await s.scalar(sa.select(Company.website_status).where(Company.id == company.id))
        crawl = {"attempted": True, "pages": len(pages), "status": _status(ws_status)}
    person_ids = await _live_people(ctx, cfg, company, pages, notes)
    resolutions: list[dict[str, Any]] = []
    if cfg.email != "off" and ctx.kind in ("leads", "email") and person_ids:
        from scout.email.engine import resolve_for_person

        deep_ids: list[uuid.UUID] = []
        for pid in person_ids:
            try:
                res = await resolve_for_person(ctx.workspace_id, pid, allow_deep=cfg.email == "fast_deep")
            except Exception as exc:
                notes.append(f"email resolution failed: {exc}"[:200])
                continue
            if res.deep_requested:
                deep_ids.append(pid)
        if deep_ids and not await _wait_deep(ctx.workspace_id, deep_ids, cfg.deep_wait_s):
            notes.append(f"deep verification still pending after {int(cfg.deep_wait_s)} s")
    enrichment = await _live_enrichment(ctx, cfg, company, pages, notes) if cfg.enrichment else {}
    async with session_scope() as s:
        rows = (
            (await s.scalars(sa.select(Person).where(Person.id.in_(person_ids)))).all() if person_ids else []
        )
        order = {pid: i for i, pid in enumerate(person_ids)}
        people = await snapshot_people(s, sorted(rows, key=lambda p: order.get(p.id, 0)))
        if cfg.email != "off" and person_ids:
            resolutions = await _resolution_stats(s, people, since=started_at)
        return {
            "company": {
                "found": True,
                "id": str(company.id),
                "name": company.name,
                "domain": company.normalized_domain,
            },
            "people": people,
            "company_emails": await company_emails(s, company.id),
            "enrichment": enrichment,
            "domain": await _domain_facts(s, company.normalized_domain),
            "email_resolutions": resolutions,
            "crawl": crawl,
            "notes": notes,
        }
