"""Job handlers: `email.find` ({"person_ids": [...]}) and `email.verify` ({"email_ids": [...]}).

`email.find` runs the Email Intelligence Engine fast path per person (contacts are processed grouped by
company domain so each Domain Intelligence Profile is built once and reused); ambiguous cases are queued
for the per-domain deep path (`email.deep_domain`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from typing import Any

import sqlalchemy as sa
import structlog

from scout.db.engine import session_scope
from scout.db.models import Company, Person
from scout.email.engine import resolve_for_person
from scout.email.store import reverify
from scout.errors import NotFound, PermanentError
from scout.jobs.registry import JobContext, job_handler

log = structlog.get_logger(__name__)

FIND_CONCURRENCY = 8  # fast path is DNS/cache bound; SMTP lives in its own pool (deep path)


def _uuids(payload: dict[str, Any], key: str) -> list[uuid.UUID]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise PermanentError(f"payload.{key} must be a list of UUIDs")
    try:
        return list(dict.fromkeys(uuid.UUID(str(x)) for x in raw))
    except ValueError as exc:
        raise PermanentError(f"payload.{key} contains an invalid UUID") from exc


@job_handler("email.find", timeout_s=1800.0)
async def email_find(ctx: JobContext) -> dict[str, Any]:
    """Find, verify and save the email of each person (bounded concurrency)."""
    person_ids = _uuids(ctx.payload, "person_ids")
    # Group by company domain: the first contact of a domain builds its profile, the others reuse it.
    async with session_scope() as s:
        rows = (
            await s.execute(
                sa.select(Person.id, Company.normalized_domain)
                .outerjoin(Company, Company.id == Person.company_id)
                .where(Person.workspace_id == ctx.workspace_id, Person.id.in_(person_ids))
            )
        ).all()
    domain_of = {pid: d or "" for pid, d in rows}
    person_ids.sort(key=lambda pid: domain_of.get(pid, ""))
    sem = asyncio.Semaphore(FIND_CONCURRENCY)
    statuses: Counter[str] = Counter()
    found = missing = 0

    async def one(pid: uuid.UUID) -> None:
        nonlocal found, missing
        async with sem:
            if pid not in domain_of:
                missing += 1
                return
            try:
                res = await resolve_for_person(ctx.workspace_id, pid, force=bool(ctx.payload.get("force")))
            except NotFound:
                missing += 1
                return
        statuses[res.status.value] += 1
        found += res.address is not None
        await ctx.emit(
            "cell.updated",
            {
                "entity": "person",
                "id": str(pid),
                "column": "email",
                "value": res.address,
                "status": res.status.value,
                "confidence": res.confidence,
                "reason": res.reason,
                "verifying": res.deep_requested,
            },
        )

    await asyncio.gather(*(one(pid) for pid in person_ids))
    return {"processed": len(person_ids), "found": found, "missing": missing, "statuses": dict(statuses)}


@job_handler("email.verify", timeout_s=1800.0)
async def email_verify(ctx: JobContext) -> dict[str, Any]:
    """Re-verify stored emails (user-confirmed rows keep their status)."""
    email_ids = _uuids(ctx.payload, "email_ids")
    updated = await reverify(ctx.workspace_id, email_ids)
    return {"requested": len(email_ids), "updated": updated}
