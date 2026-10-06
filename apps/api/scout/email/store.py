"""Persistence: domain pattern memory, findings (`emails` + `email_checks`), re-verification.

`domain_email_patterns` is a global data asset; `emails` rows are workspace-scoped. Rows marked
`is_user_confirmed` are never overwritten (their history still receives new `email_checks`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.engine import session_scope
from scout.db.enums import EmailDiscoveryMethod, EmailKind, EmailStatus, SmtpResult
from scout.db.models import Company, DomainEmailPattern, Email, EmailCheck, Person, WebsitePage
from scout.email.finder import find_email, matches_person
from scout.email.lists import is_disposable_domain, is_free_provider, is_role_local_part
from scout.email.patterns import PATTERN_SET, infer_pattern, infer_patterns, pattern_confidence
from scout.email.status import STATUS_RANK, derive_status
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address
from scout.email.types import EmailCandidate, EmailFinding, VerificationResult
from scout.email.verifier import get_verifier
from scout.errors import NotFound

log = structlog.get_logger(__name__)

_GUESSED = (
    EmailDiscoveryMethod.known_pattern,
    EmailDiscoveryMethod.inferred_pattern,
    EmailDiscoveryMethod.permutation,
)
REVERIFY_CONCURRENCY = 4

# ---- domain pattern memory ----------------------------------------------------------------


async def load_domain_patterns(domain: str | None) -> list[tuple[str, float, int]]:
    """[(pattern, confidence, evidence)] for a domain, best first; evidence = samples + SMTP successes."""
    d = normalize_domain(domain)
    if d is None:
        return []
    async with session_scope() as s:
        rows = (
            await s.scalars(
                sa.select(DomainEmailPattern)
                .where(DomainEmailPattern.domain == d, DomainEmailPattern.confidence > 0)
                .order_by(DomainEmailPattern.confidence.desc(), DomainEmailPattern.supporting_samples.desc())
            )
        ).all()
    return [
        (r.pattern, float(r.confidence), r.supporting_samples + r.successful_checks)
        for r in rows
        if r.pattern in PATTERN_SET
    ]


async def _bump_pattern(
    s: AsyncSession, domain: str, pattern: str, *, samples: int = 0, successes: int = 0, failures: int = 0
) -> None:
    """Upsert counters for (domain, pattern) and recompute its confidence."""
    T = DomainEmailPattern
    verified = successes > 0 or failures > 0
    ins = pg_insert(T).values(
        domain=domain,
        pattern=pattern,
        supporting_samples=samples,
        successful_checks=successes,
        failed_checks=failures,
        confidence=pattern_confidence(samples, successes, failures),
        last_verified_at=sa.func.now() if verified else None,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[T.domain, T.pattern],
        set_={
            "supporting_samples": T.supporting_samples + ins.excluded.supporting_samples,
            "successful_checks": T.successful_checks + ins.excluded.successful_checks,
            "failed_checks": T.failed_checks + ins.excluded.failed_checks,
            "last_verified_at": sa.func.coalesce(ins.excluded.last_verified_at, T.last_verified_at),
            "updated_at": sa.func.now(),
        },
    ).returning(T.id, T.supporting_samples, T.successful_checks, T.failed_checks)
    row = (await s.execute(stmt)).one()
    await _set_confidence(s, *row)


async def _set_confidence(
    s: AsyncSession, row_id: uuid.UUID, samples: int, successes: int, failures: int
) -> None:
    await s.execute(
        sa.update(DomainEmailPattern)
        .where(DomainEmailPattern.id == row_id)
        .values(confidence=pattern_confidence(samples, successes, failures))
    )


async def learn_patterns(domain: str, samples: Sequence[tuple[str, str, str]]) -> dict[str, int]:
    """Learn from published (first, last, local_part) samples: supporting_samples += n per pattern."""
    d = normalize_domain(domain)
    counts = infer_patterns(samples)
    if d is None or not counts:
        return {}
    async with session_scope() as s:
        for pattern, n in counts.items():
            await _bump_pattern(s, d, pattern, samples=n)
    return counts


async def record_pattern_outcome(domain: str, pattern: str, *, success: bool) -> None:
    """SMTP outcome of a guessed address: success creates/strengthens, failure weakens existing rows."""
    d = normalize_domain(domain)
    if d is None or pattern not in PATTERN_SET:
        return
    async with session_scope() as s:
        if success:
            await _bump_pattern(s, d, pattern, successes=1)
            return
        T = DomainEmailPattern
        row = (
            await s.execute(
                sa.update(T)
                .where(T.domain == d, T.pattern == pattern)
                .values(failed_checks=T.failed_checks + 1, last_verified_at=sa.func.now())
                .returning(T.id, T.supporting_samples, T.successful_checks, T.failed_checks)
            )
        ).first()
        if row is not None:
            await _set_confidence(s, *row)


# ---- findings ---------------------------------------------------------------------------------


def _check_payload(v: VerificationResult) -> dict[str, Any]:
    data = asdict(v)
    data.pop("raw", None)
    return {"signals": data, "raw": v.raw}


def _apply_verification(row: Email, v: VerificationResult | None) -> None:
    if v is None:
        return
    row.mx_valid = v.mx_valid
    row.smtp_result = v.smtp_result
    row.catch_all = v.catch_all
    row.disposable = v.disposable
    row.role_address = v.role_address
    row.free_provider = v.free_provider


def _check_row(
    workspace_id: uuid.UUID, email_id: uuid.UUID, status: EmailStatus, v: VerificationResult
) -> EmailCheck:
    return EmailCheck(
        workspace_id=workspace_id,
        email_id=email_id,
        verifier=v.verifier,
        status=status,
        mx_valid=v.mx_valid,
        smtp_result=v.smtp_result,
        catch_all=v.catch_all,
        result=_check_payload(v),
        duration_ms=v.duration_ms,
        error=v.error,
    )


async def _locked_email(s: AsyncSession, workspace_id: uuid.UUID, address: str, **insert: Any) -> Email:
    """Insert-if-missing then lock the (workspace_id, address) row."""
    local, domain = split_address(address)
    await s.execute(
        pg_insert(Email)
        .values(workspace_id=workspace_id, address=address, local_part=local, domain=domain, **insert)
        .on_conflict_do_nothing(index_elements=["workspace_id", "address"])
    )
    row = await s.scalar(
        sa.select(Email)
        .where(Email.workspace_id == workspace_id, Email.address == address)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def _update_primary(s: AsyncSession, row: Email, person_id: uuid.UUID, *, make_primary: bool) -> None:
    person = await s.get(Person, person_id, with_for_update=True)
    if person is None:
        return
    if row.status == EmailStatus.INVALID:
        if row.is_primary and not row.is_user_confirmed:
            row.is_primary = False
            if person.primary_email_id == row.id:
                person.primary_email_id = None
        return
    if not make_primary:
        return
    current = await s.get(Email, person.primary_email_id) if person.primary_email_id else None
    if current is not None and current.id != row.id:
        if current.is_user_confirmed:
            return
        if (STATUS_RANK[current.status], current.overall_confidence or 0.0) > (
            STATUS_RANK[row.status],
            row.overall_confidence or 0.0,
        ):
            return
    await s.execute(
        sa.update(Email)
        .where(Email.person_id == person_id, Email.id != row.id, Email.is_primary.is_(True))
        .values(is_primary=False)
    )
    row.is_primary = True
    person.primary_email_id = row.id


async def save_finding(
    workspace_id: uuid.UUID,
    *,
    person_id: uuid.UUID,
    company_id: uuid.UUID | None,
    finding: EmailFinding,
    make_primary: bool = True,
) -> Email | None:
    """Upsert the person's email from a finding, append an `email_checks` row, maintain the primary."""
    addr = normalize_address(finding.address)
    if addr is None:
        return None
    method = finding.method or EmailDiscoveryMethod.permutation
    v = finding.verification
    async with session_scope() as s:
        row = await _locked_email(
            s,
            workspace_id,
            addr,
            person_id=person_id,
            company_id=company_id,
            kind=EmailKind.person,
            discovery_method=method,
            status=finding.status,
        )
        if row.person_id not in (None, person_id):
            log.warning("email.save.owned_by_other_person", address=addr, person_id=str(person_id))
            return None
        if v is not None:
            s.add(_check_row(workspace_id, row.id, finding.status, v))
        if row.is_user_confirmed:
            return row
        row.person_id = person_id
        row.company_id = company_id or row.company_id
        row.kind = EmailKind.person
        if not (row.discovery_method not in _GUESSED and method in _GUESSED):
            row.discovery_method = method  # never downgrade published/import/user to a guess
        row.pattern = finding.pattern or row.pattern
        row.source_url = finding.source_url or row.source_url
        row.status = finding.status
        row.pattern_confidence = finding.pattern_confidence
        row.overall_confidence = finding.overall_confidence
        row.last_checked_at = datetime.now(UTC)
        _apply_verification(row, v)
        await s.flush()
        await _update_primary(s, row, person_id, make_primary=make_primary)
        await s.flush()
        await s.refresh(row)
    return row


async def save_company_email(
    workspace_id: uuid.UUID, company_id: uuid.UUID, address: str, source_url: str | None
) -> Email | None:
    """Store a published company-level address (role/generic, person_id NULL, status UNKNOWN until verified)."""
    addr = normalize_address(address)
    if addr is None or not is_valid_syntax(addr):
        return None
    local, domain = split_address(addr)
    kind = EmailKind.role if is_role_local_part(local) else EmailKind.generic
    async with session_scope() as s:
        row = await _locked_email(
            s,
            workspace_id,
            addr,
            company_id=company_id,
            kind=kind,
            discovery_method=EmailDiscoveryMethod.published,
            source_url=source_url,
            status=EmailStatus.UNKNOWN,
            role_address=kind == EmailKind.role,
            free_provider=is_free_provider(domain),
            disposable=is_disposable_domain(domain),
        )
        if row.person_id is None and not row.is_user_confirmed:
            row.company_id = row.company_id or company_id
            row.source_url = row.source_url or source_url
        await s.flush()
        await s.refresh(row)
    return row


async def reverify(workspace_id: uuid.UUID, email_ids: Sequence[uuid.UUID]) -> int:
    """Re-run verification; append checks; update status. Returns rows updated (confirmed rows excluded)."""
    if not email_ids:
        return 0
    async with session_scope() as s:
        rows = (
            await s.scalars(
                sa.select(Email).where(Email.workspace_id == workspace_id, Email.id.in_(list(email_ids)))
            )
        ).all()
    verifier = get_verifier()
    sem = asyncio.Semaphore(REVERIFY_CONCURRENCY)

    async def one(snapshot: Email) -> bool:
        async with sem:
            v = await verifier.verify(snapshot.address)
        evidence = 0
        if snapshot.discovery_method in (
            EmailDiscoveryMethod.known_pattern,
            EmailDiscoveryMethod.inferred_pattern,
        ):
            evidence = next(
                (n for p, _, n in await load_domain_patterns(snapshot.domain) if p == snapshot.pattern), 0
            )
        cand = EmailCandidate(
            address=snapshot.address,
            method=snapshot.discovery_method,
            pattern=snapshot.pattern,
            pattern_confidence=snapshot.pattern_confidence or 0.0,
            source_url=snapshot.source_url,
            supporting_samples=evidence,
        )
        status, conf = derive_status(cand, v, for_person=snapshot.person_id is not None)
        async with session_scope() as s:
            row = await s.get(Email, snapshot.id, with_for_update=True)
            if row is None:
                return False
            s.add(_check_row(workspace_id, row.id, status, v))
            if row.is_user_confirmed:
                return False
            row.status = status
            row.overall_confidence = conf
            row.last_checked_at = datetime.now(UTC)
            _apply_verification(row, v)
            await s.flush()
            if row.person_id is not None:
                await _update_primary(s, row, row.person_id, make_primary=False)
        return True

    return sum(await asyncio.gather(*(one(r) for r in rows)))


# ---- orchestration ----------------------------------------------------------------------------


def _person_names(p: Person) -> tuple[str | None, str | None]:
    first, last = p.first_name, p.last_name
    if first and last:
        return first, last
    tokens = (p.full_name or "").split()
    if len(tokens) >= 2:
        return first or tokens[0], last or " ".join(tokens[1:])
    return first or (tokens[0] if tokens else None), last


def _page_emails(pages: Sequence[tuple[str, list[Any] | None]]) -> list[tuple[str, str | None]]:
    out: dict[str, str | None] = {}
    for url, emails in pages:
        for item in emails or []:
            raw = (
                item
                if isinstance(item, str)
                else (
                    (item.get("address") or item.get("email") or item.get("value"))
                    if isinstance(item, dict)
                    else None
                )
            )
            addr = normalize_address(raw) if isinstance(raw, str) else None
            if addr and addr not in out:
                out[addr] = url
    return list(out.items())


def _domain_evidence(
    domain: str,
    published: list[tuple[str, str | None]],
    person: tuple[str | None, str | None],
    colleagues: Sequence[tuple[str | None, str | None]],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Split on-domain published addresses into named colleague samples and nameless local parts."""
    samples: list[tuple[str, str, str]] = []
    observed: list[str] = []
    for addr, _ in published:
        local, d = split_address(addr)
        if d != domain or is_role_local_part(local) or matches_person(person[0], person[1], local)[0]:
            continue
        for first, last in colleagues:
            if first and last and infer_pattern(first, last, local) not in (None, "{f}{l}"):
                samples.append((first, last, local))
                break
        else:
            observed.append(local)
    return samples, observed


async def _learn_from_finding(
    domain: str, first: str | None, last: str | None, finding: EmailFinding
) -> None:
    """Idempotent learning: each address teaches the global pattern memory at most once."""
    addresses = [a.candidate.address for a in finding.attempts]
    if not addresses:
        return
    async with session_scope() as s:
        known = (
            await s.execute(
                sa.select(Email.address, Email.status, Email.discovery_method).where(
                    Email.address.in_(addresses)
                )
            )
        ).all()
    published_known = {a for a, _, m in known if m == EmailDiscoveryMethod.published}
    safe_known = {a for a, st, _ in known if st == EmailStatus.SAFE}
    invalid_known = {a for a, st, _ in known if st == EmailStatus.INVALID}

    for att in finding.attempts:
        c, v = att.candidate, att.verification
        local, d = split_address(c.address)
        if d != domain or not c.pattern:
            continue
        if c.method == EmailDiscoveryMethod.published:
            if att.status != EmailStatus.INVALID and c.address not in published_known and first and last:
                await learn_patterns(domain, [(first, last, local)])
        elif v.smtp_result == SmtpResult.accepted and v.catch_all is False and c.address not in safe_known:
            await record_pattern_outcome(domain, c.pattern, success=True)
        elif v.smtp_result == SmtpResult.rejected and c.address not in invalid_known:
            await record_pattern_outcome(domain, c.pattern, success=False)


async def find_and_save_for_person(workspace_id: uuid.UUID, person_id: uuid.UUID) -> EmailFinding:
    """Load evidence for a person, run the waterfall with the configured verifier, learn, save."""
    async with session_scope() as s:
        person = await s.get(Person, person_id)
        if person is None or person.workspace_id != workspace_id:
            raise NotFound("Person not found", person_id=str(person_id))
        confirmed = await s.scalar(
            sa.select(Email)
            .where(
                Email.workspace_id == workspace_id,
                Email.person_id == person_id,
                Email.is_user_confirmed.is_(True),
            )
            .order_by(Email.is_primary.desc(), Email.updated_at.desc())
            .limit(1)
        )
        company = await s.get(Company, person.company_id) if person.company_id else None
        pages: list[tuple[str, list[Any] | None]] = []
        colleagues: list[tuple[str | None, str | None]] = []
        if company is not None:
            pages = [
                (r.url, r.emails)
                for r in await s.execute(
                    sa.select(WebsitePage.url, WebsitePage.emails).where(
                        WebsitePage.workspace_id == workspace_id, WebsitePage.company_id == company.id
                    )
                )
            ]
            colleagues = [
                _person_names(p)
                for p in await s.scalars(
                    sa.select(Person).where(
                        Person.workspace_id == workspace_id,
                        Person.company_id == company.id,
                        Person.id != person_id,
                    )
                )
            ]

    if confirmed is not None:
        return EmailFinding(
            address=confirmed.address,
            status=confirmed.status,
            overall_confidence=confirmed.overall_confidence
            if confirmed.overall_confidence is not None
            else 1.0,
            method=confirmed.discovery_method,
            pattern=confirmed.pattern,
            pattern_confidence=confirmed.pattern_confidence,
            verification=None,
            source_url=confirmed.source_url,
        )

    first, last = _person_names(person)
    domain = normalize_domain((company.normalized_domain or company.domain) if company else None)
    published = _page_emails(pages)
    samples, observed = _domain_evidence(domain, published, (first, last), colleagues) if domain else ([], [])
    finding = await find_email(
        first=first,
        last=last,
        domain=domain,
        published=published,
        known_patterns=await load_domain_patterns(domain),
        observed_local_parts=observed,
        observed_samples=samples,
        company_size_max=company.employee_max if company else None,
        country=company.country if company else None,
        verifier=get_verifier(),
    )
    if domain:
        await _learn_from_finding(domain, first, last, finding)
    if finding.address:
        await save_finding(workspace_id, person_id=person_id, company_id=person.company_id, finding=finding)
    log.info(
        "email.find_and_save",
        person_id=str(person_id),
        domain=domain,
        status=finding.status,
        method=finding.method,
        tried=len(finding.candidates_tried),
        reason=finding.reason,
    )
    return finding
