"""Suppression list (spec §87) and GDPR erasure."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import SuppressionEntity, SuppressionReason
from scout.db.models import Company, Email, ListMembership, Person, PersonFieldObservation, SuppressionEntry
from scout.errors import NotFound
from scout.util.text import normalize_person_name


async def suppress(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    reason: SuppressionReason,
    user_id: str | None,
    person_ids: list[uuid.UUID] = (),  # type: ignore[assignment]
    company_ids: list[uuid.UUID] = (),  # type: ignore[assignment]
    emails: list[str] = (),  # type: ignore[assignment]
    domains: list[str] = (),  # type: ignore[assignment]
    note: str | None = None,
    remove_from_lists: bool = True,
) -> int:
    """Suppress entities. Suppressed people/companies are never rediscovered or re-added automatically."""
    rows: list[dict[str, Any]] = []
    if person_ids:
        people = (await s.execute(sa.select(Person.id, Person.primary_email_id).where(Person.workspace_id == workspace_id, Person.id.in_(person_ids)))).all()
        for pid, _ in people:
            rows.append({"entity_type": SuppressionEntity.person, "entity_id": pid, "value": str(pid)})
        email_rows = (await s.scalars(sa.select(Email.address).where(Email.person_id.in_([p for p, _ in people])))).all()
        for addr in email_rows:
            rows.append({"entity_type": SuppressionEntity.email, "entity_id": None, "value": addr.lower()})
        await s.execute(sa.update(Person).where(Person.id.in_([p for p, _ in people])).values(suppressed_at=sa.func.now()))
        if remove_from_lists:
            await s.execute(sa.delete(ListMembership).where(ListMembership.person_id.in_([p for p, _ in people])))
    if company_ids:
        comps = (await s.execute(sa.select(Company.id, Company.normalized_domain).where(Company.workspace_id == workspace_id, Company.id.in_(company_ids)))).all()
        for cid, dom in comps:
            rows.append({"entity_type": SuppressionEntity.company, "entity_id": cid, "value": str(cid)})
            if dom:
                rows.append({"entity_type": SuppressionEntity.domain, "entity_id": None, "value": dom})
        await s.execute(sa.update(Company).where(Company.id.in_([c for c, _ in comps])).values(suppressed_at=sa.func.now()))
        if remove_from_lists:
            await s.execute(sa.delete(ListMembership).where(ListMembership.company_id.in_([c for c, _ in comps])))
    for e in emails:
        rows.append({"entity_type": SuppressionEntity.email, "entity_id": None, "value": e.strip().lower()})
    for d in domains:
        rows.append({"entity_type": SuppressionEntity.domain, "entity_id": None, "value": d.strip().lower()})
    if not rows:
        return 0
    stmt = pg_insert(SuppressionEntry).values(
        [{**r, "workspace_id": workspace_id, "reason": reason, "note": note, "created_by": user_id} for r in rows]
    ).on_conflict_do_nothing(index_elements=["workspace_id", "entity_type", "value"])
    res = await s.execute(stmt.returning(SuppressionEntry.id))
    return len(res.scalars().all())


async def list_suppressions(s: AsyncSession, workspace_id: uuid.UUID, *, limit: int = 500) -> list[dict[str, Any]]:
    rows = (
        await s.scalars(
            sa.select(SuppressionEntry).where(SuppressionEntry.workspace_id == workspace_id).order_by(SuppressionEntry.created_at.desc()).limit(limit)
        )
    ).all()
    out = []
    for r in rows:
        label = r.value
        if r.entity_type == SuppressionEntity.person and r.entity_id:
            label = await s.scalar(sa.select(Person.full_name).where(Person.id == r.entity_id)) or r.value
        elif r.entity_type == SuppressionEntity.company and r.entity_id:
            label = await s.scalar(sa.select(Company.name).where(Company.id == r.entity_id)) or r.value
        out.append({"id": r.id, "entity_type": r.entity_type.value, "entity_id": r.entity_id, "value": r.value,
                    "label": label, "reason": r.reason.value, "note": r.note, "created_at": r.created_at})
    return out


async def unsuppress(s: AsyncSession, workspace_id: uuid.UUID, entry_id: uuid.UUID) -> None:
    e = await s.get(SuppressionEntry, entry_id)
    if e is None or e.workspace_id != workspace_id:
        raise NotFound("Suppression entry not found")
    if e.reason == SuppressionReason.gdpr:
        from scout.errors import Forbidden

        raise Forbidden("GDPR suppressions cannot be removed")
    await s.delete(e)


async def gdpr_erase_person(s: AsyncSession, workspace_id: uuid.UUID, person_id: uuid.UUID, *, user_id: str | None) -> None:
    """Erase personal data but keep a hashed suppression key so the person is never rediscovered."""
    p = await s.get(Person, person_id)
    if p is None or p.workspace_id != workspace_id:
        raise NotFound("Person not found")
    key = hashlib.sha256(f"{p.company_id}|{normalize_person_name(p.full_name)}".encode()).hexdigest()
    emails = (await s.scalars(sa.select(Email.address).where(Email.person_id == person_id))).all()
    for addr in emails:
        await s.execute(
            pg_insert(SuppressionEntry)
            .values(workspace_id=workspace_id, entity_type=SuppressionEntity.email, value=addr, reason=SuppressionReason.gdpr, created_by=user_id)
            .on_conflict_do_nothing(index_elements=["workspace_id", "entity_type", "value"])
        )
    await s.execute(
        pg_insert(SuppressionEntry)
        .values(workspace_id=workspace_id, entity_type=SuppressionEntity.person, value=f"erased:{key}", reason=SuppressionReason.gdpr, created_by=user_id)
        .on_conflict_do_nothing(index_elements=["workspace_id", "entity_type", "value"])
    )
    await s.execute(sa.delete(PersonFieldObservation).where(PersonFieldObservation.person_id == person_id))
    await s.delete(p)
