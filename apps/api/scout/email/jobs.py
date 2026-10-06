"""Job handlers: `email.find` ({"person_ids": [...]}) and `email.verify` ({"email_ids": [...]})."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from typing import Any

import structlog

from scout.email.store import find_and_save_for_person, reverify
from scout.errors import NotFound, PermanentError
from scout.jobs.registry import JobContext, job_handler

log = structlog.get_logger(__name__)

FIND_CONCURRENCY = 4


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
    sem = asyncio.Semaphore(FIND_CONCURRENCY)
    statuses: Counter[str] = Counter()
    found = missing = 0

    async def one(pid: uuid.UUID) -> None:
        nonlocal found, missing
        async with sem:
            try:
                finding = await find_and_save_for_person(ctx.workspace_id, pid)
            except NotFound:
                missing += 1
                return
        statuses[finding.status.value] += 1
        found += finding.address is not None
        await ctx.emit(
            "cell.updated",
            {
                "entity": "person",
                "id": str(pid),
                "column": "email",
                "value": finding.address,
                "status": finding.status.value,
                "confidence": finding.overall_confidence,
                "reason": finding.reason,
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
