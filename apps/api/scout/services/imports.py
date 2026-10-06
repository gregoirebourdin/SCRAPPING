"""CSV import with column mapping (spec §85). Imported leads enter the global registry and, by default,
are marked as previously known (IMPORTED exposures) so future searches can exclude them."""

from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import (
    EmailDiscoveryMethod,
    EmailKind,
    EmailStatus,
    EntityType,
    ExposureType,
    ImportStatus,
    SourceType,
)
from scout.db.models import Email, Import, Person
from scout.errors import ValidationFailed
from scout.jobs import queue
from scout.jobs.events import emit
from scout.jobs.registry import JobContext, job_handler
from scout.services import registry
from scout.services.exclusion import suppressed_companies, suppressed_people
from scout.services.lists import add_to_list
from scout.util.text import normalize_key
from scout.util.urls import registrable_domain

MAX_ROWS = 100_000
CHUNK = 500

TARGET_FIELDS = {
    "full_name": "Full name",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "title": "Job title",
    "company": "Company",
    "website": "Website",
    "domain": "Domain",
    "profile_url": "LinkedIn / profile URL",
    "phone": "Phone",
    "city": "City",
    "country": "Country",
    "industry": "Industry",
    "ignore": "Ignore",
}

_SYNONYMS: dict[str, list[str]] = {
    "full_name": ["full name", "name", "nom complet", "contact", "contact name", "person"],
    "first_name": ["first name", "firstname", "prenom", "given name"],
    "last_name": ["last name", "lastname", "surname", "nom", "family name", "nom de famille"],
    "email": ["email", "e mail", "mail", "email address", "adresse email", "courriel", "work email"],
    "title": ["title", "job title", "poste", "fonction", "role", "position", "titre"],
    "company": ["company", "company name", "entreprise", "societe", "organization", "organisation", "account", "raison sociale"],
    "website": ["website", "site", "site web", "url", "company website", "web"],
    "domain": ["domain", "company domain", "domaine"],
    "profile_url": ["linkedin", "linkedin url", "profile", "profile url", "linkedin profile"],
    "phone": ["phone", "telephone", "tel", "mobile", "phone number"],
    "city": ["city", "ville", "town", "locality"],
    "country": ["country", "pays", "country code"],
    "industry": ["industry", "secteur", "sector", "industrie"],
}


def decode_csv(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationFailed("Could not decode the CSV file")


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ValidationFailed("The CSV file is empty")
    headers = [h.strip() for h in rows[0]]
    body = rows[1:]
    if len(body) > MAX_ROWS:
        raise ValidationFailed(f"Too many rows ({len(body)}); the limit is {MAX_ROWS}")
    return headers, body


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for h in headers:
        key = normalize_key(h)
        best = "ignore"
        for target, syns in _SYNONYMS.items():
            if target in used:
                continue
            if key in syns or key.replace(" ", "") in [x.replace(" ", "") for x in syns]:
                best = target
                break
        if best == "ignore":
            for target, syns in _SYNONYMS.items():
                if target not in used and any(sy in key for sy in syns if len(sy) > 3):
                    best = target
                    break
        mapping[h] = best
        if best != "ignore":
            used.add(best)
    return mapping


def preview(raw: bytes) -> dict[str, Any]:
    headers, body = parse_csv(decode_csv(raw))
    return {
        "headers": headers,
        "sample": body[:10],
        "row_count": len(body),
        "suggested_mapping": suggest_mapping(headers),
        "targets": TARGET_FIELDS,
    }


async def start_import(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    raw: bytes,
    filename: str,
    mapping: dict[str, str],
    list_id: uuid.UUID | None,
    mark_as_known: bool,
    user_id: str | None,
) -> Import:
    headers, body = parse_csv(decode_csv(raw))
    unknown = [t for t in mapping.values() if t not in TARGET_FIELDS]
    if unknown:
        raise ValidationFailed(f"Unknown mapping targets: {unknown}")
    if not any(t in mapping.values() for t in ("full_name", "first_name", "email", "company", "website", "domain")):
        raise ValidationFailed("Map at least a name, email, company or website column")
    idx = {h: i for i, h in enumerate(headers)}
    records: list[dict[str, str]] = []
    for row in body:
        rec: dict[str, str] = {}
        for header, target in mapping.items():
            if target == "ignore" or header not in idx:
                continue
            i = idx[header]
            if i < len(row) and row[i].strip():
                rec[target] = row[i].strip()
        if rec:
            records.append(rec)
    imp = Import(
        workspace_id=workspace_id, list_id=list_id, filename=filename[:200], status=ImportStatus.running,
        column_mapping=mapping, mark_as_known=mark_as_known, row_count=len(records), created_by=user_id,
    )
    s.add(imp)
    await s.flush()
    for start in range(0, len(records), CHUNK):
        await queue.enqueue(
            s, workspace_id=workspace_id, type="import.chunk", priority=5,
            payload={"import_id": str(imp.id), "offset": start, "rows": records[start : start + CHUNK]},
            dedupe_key=f"import:{imp.id}:{start}",
        )
    if not records:
        imp.status = ImportStatus.completed
    return imp


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _import_record(s: AsyncSession, imp: Import, rec: dict[str, str], row_no: int) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    """Returns (outcome, person_id, company_id); outcome ∈ imported|merged|skipped."""
    ws = imp.workspace_id
    email = (rec.get("email") or "").lower().strip()
    if email and not _EMAIL_RE.match(email):
        email = ""
    website = rec.get("website") or rec.get("domain")
    domain = registrable_domain(website) if website else None
    if not domain and email:
        d = email.split("@", 1)[1]
        from scout.email.lists import FREE_PROVIDERS  # type: ignore[import-not-found]

        domain = None if d in FREE_PROVIDERS else registrable_domain(d)
    company_name = rec.get("company") or (domain.split(".")[0].replace("-", " ").title() if domain else None)
    ev = registry.Evidence(
        source_type=SourceType.import_, confidence=0.8, source_key="import",
        evidence=f"Imported from {imp.filename} (row {row_no})",
    )
    company_id = None
    created_company = False
    if company_name:
        if domain:
            sup_ids, sup_domains = await suppressed_companies(s, ws, domains=[domain])
            if sup_domains:
                return "skipped", None, None
        company, created_company = await registry.upsert_company(
            s, ws,
            registry.CompanyInput(name=company_name, website=website, domain=domain, city=rec.get("city"),
                                  country=(rec.get("country") or "")[:2].upper() or None, industry=rec.get("industry"),
                                  phone=None if rec.get("full_name") or rec.get("first_name") else rec.get("phone")),
            ev,
        )
        company_id = company.id
        sup_ids, _ = await suppressed_companies(s, ws, company_ids=[company.id])
        if sup_ids:
            return "skipped", None, None
    full_name = rec.get("full_name") or " ".join(x for x in (rec.get("first_name"), rec.get("last_name")) if x)
    if not full_name:
        return ("imported" if created_company else "merged"), None, company_id
    if email:
        _, sup_emails = await suppressed_people(s, ws, emails=[email])
        if sup_emails:
            return "skipped", None, None
    from scout.extract.titles import normalize_title

    title = rec.get("title")
    person, created = await registry.upsert_person(
        s, ws, company_id=company_id, full_name=full_name, evidence=ev, first_name=rec.get("first_name"),
        last_name=rec.get("last_name"), job_title=title, title_info=normalize_title(title) if title else None,
        profile_url=rec.get("profile_url"), email=email or None,
    )
    sup_ids, _ = await suppressed_people(s, ws, person_ids=[person.id])
    if sup_ids:
        return "skipped", None, None
    if rec.get("phone"):
        person.phone = rec["phone"]
    if email:
        existing = await s.scalar(sa.select(Email).where(Email.workspace_id == ws, Email.address == email))
        if existing is None:
            local, dom = email.split("@", 1)
            e = Email(
                workspace_id=ws, person_id=person.id, company_id=company_id, address=email, local_part=local, domain=dom,
                kind=EmailKind.person, discovery_method=EmailDiscoveryMethod.import_, status=EmailStatus.UNKNOWN,
                overall_confidence=0.5, is_primary=person.primary_email_id is None,
            )
            s.add(e)
            await s.flush()
            if person.primary_email_id is None:
                person.primary_email_id = e.id
    return ("imported" if created else "merged"), person.id, company_id


@job_handler("import.chunk", timeout_s=900)
async def import_chunk(ctx: JobContext) -> dict[str, Any]:
    p = ctx.payload
    import_id = uuid.UUID(p["import_id"])
    offset = int(p.get("offset", 0))
    imported = merged = skipped = errors = 0
    error_rows: list[dict[str, Any]] = []
    person_ids: list[uuid.UUID] = []
    company_only: list[uuid.UUID] = []
    async with session_scope() as s:
        imp = await s.get(Import, import_id)
        if imp is None:
            return {"skipped": "import deleted"}
        for i, rec in enumerate(p.get("rows", [])):
            try:
                async with s.begin_nested():
                    outcome, pid, cid = await _import_record(s, imp, rec, offset + i + 2)
            except Exception as exc:  # row-level errors never abort the import
                errors += 1
                if len(error_rows) < 50:
                    error_rows.append({"row": offset + i + 2, "error": str(exc)[:200]})
                continue
            if outcome == "skipped":
                skipped += 1
                continue
            imported += outcome == "imported"
            merged += outcome == "merged"
            if pid:
                person_ids.append(pid)
            elif cid:
                company_only.append(cid)
        if imp.mark_as_known and (person_ids or company_only):
            await registry.record_exposures(
                s, imp.workspace_id, ExposureType.IMPORTED, person_ids=person_ids, company_ids=company_only, import_id=imp.id
            )
        if imp.list_id:
            if person_ids:
                await add_to_list(s, imp.workspace_id, imp.list_id, EntityType.person, person_ids, user_id=imp.created_by, added_via="import")
            if company_only:
                from scout.db.models import List

                lst = await s.get(List, imp.list_id)
                if lst and lst.entity_type == EntityType.company:
                    await add_to_list(s, imp.workspace_id, imp.list_id, EntityType.company, company_only, user_id=imp.created_by, added_via="import")
        await s.execute(
            sa.update(Import).where(Import.id == import_id).values(
                imported_count=Import.imported_count + imported,
                merged_count=Import.merged_count + merged,
                skipped_count=Import.skipped_count + skipped,
                error_count=Import.error_count + errors,
                errors=Import.errors.op("||")(sa.literal(error_rows, type_=JSONB)) if error_rows else Import.errors,
            )
        )
        await s.flush()
        await s.refresh(imp)
        done = imp.imported_count + imp.merged_count + imp.skipped_count + imp.error_count
        if done >= imp.row_count and imp.status != ImportStatus.completed:
            imp.status = ImportStatus.completed
            imp.finished_at = sa.func.now()
        await emit(imp.workspace_id, "import.progress", {
            "import_id": str(imp.id), "done": done, "total": imp.row_count, "imported": imp.imported_count,
            "merged": imp.merged_count, "skipped": imp.skipped_count, "errors": imp.error_count,
            "status": imp.status.value if hasattr(imp.status, "value") else str(imp.status),
        }, session=s)
    return {"imported": imported, "merged": merged, "skipped": skipped, "errors": errors}


async def get_import(s: AsyncSession, workspace_id: uuid.UUID, import_id: uuid.UUID) -> dict[str, Any]:
    imp = await s.get(Import, import_id)
    if imp is None or imp.workspace_id != workspace_id:
        from scout.errors import NotFound

        raise NotFound("Import not found")
    return {
        "id": imp.id, "filename": imp.filename, "status": imp.status.value, "row_count": imp.row_count,
        "imported_count": imp.imported_count, "merged_count": imp.merged_count, "skipped_count": imp.skipped_count,
        "error_count": imp.error_count, "errors": imp.errors, "list_id": imp.list_id, "mark_as_known": imp.mark_as_known,
        "created_at": imp.created_at, "finished_at": imp.finished_at,
    }


async def people_count_for_import(s: AsyncSession, import_id: uuid.UUID) -> int:
    from scout.db.models import LeadExposure

    return int(
        await s.scalar(
            sa.select(sa.func.count(sa.distinct(LeadExposure.entity_id))).where(
                LeadExposure.import_id == import_id, LeadExposure.entity_type == EntityType.person
            )
        )
        or 0
    )


__all__ = ["Person", "get_import", "import_chunk", "preview", "start_import"]
