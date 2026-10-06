"""Deterministic-field strategy: copy/derive canonical company or person fields (no AI, no network)."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.models import QualificationScore
from scout.enrich.resolvers.base import ResolveContext, failed, ok, unknown
from scout.enrich.types import CellResult

RESOLVER = "record"


def employee_range_label(lo: int | None, hi: int | None) -> str | None:
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return str(lo) if lo == hi else f"{lo}–{hi}"
    if lo is not None:
        return f"{lo}+"
    return f"≤ {hi}"


async def _icp_score(rc: ResolveContext) -> int | None:
    if rc.company is None:
        return None
    cond: list[Any] = [QualificationScore.workspace_id == rc.workspace_id, QualificationScore.company_id == rc.company.id]
    if rc.person is not None:
        cond.append(QualificationScore.person_id == rc.person.id)
    async with session_scope() as s:
        return await s.scalar(
            sa.select(QualificationScore.icp_score).where(*cond).order_by(QualificationScore.computed_at.desc()).limit(1)
        )


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    f = plan.field or ""
    c, p = rc.company, rc.person
    company_conf = (c.company_confidence if c is not None and c.company_confidence is not None else 0.9)
    person_conf = (p.identity_confidence if p is not None and p.identity_confidence is not None else 0.9)
    value: Any = None
    conf: float = company_conf
    source_id = "company_record"
    evidence = f"{f.replace('_', ' ').capitalize()} from the company record"

    if f in {"job_title", "seniority", "full_name", "email_status", "person_email"}:
        if p is None:
            return unknown(plan, resolver=RESOLVER, error="Missing dependency: person")
        source_id, conf = "person_record", person_conf
        evidence = f"{f.replace('_', ' ').capitalize()} from the person record"
        if f == "job_title":
            value = p.job_title
        elif f == "seniority":
            value = getattr(p.seniority, "value", p.seniority)
        elif f == "full_name":
            value = p.full_name
        else:
            emails = await rc.emails()
            email = next((e for e in emails if e.id == p.primary_email_id), None) or (emails[0] if emails else None)
            if email is None:
                return unknown(plan, resolver=RESOLVER, error="Missing dependency: email")
            source_id = "email_verification"
            conf = email.overall_confidence if email.overall_confidence is not None else 0.5
            value = getattr(email.status, "value", email.status) if f == "email_status" else email.address
            evidence = f"{email.address} · {getattr(email.status, 'value', email.status)}"
    elif c is None:
        return unknown(plan, resolver=RESOLVER, error="Missing dependency: company")
    elif f == "employee_range":
        value = employee_range_label(c.employee_min, c.employee_max)
        conf = c.employee_confidence if c.employee_confidence is not None else company_conf
    elif f == "industry":
        value = c.industry or c.sub_industry or c.category_raw
    elif f == "domain":
        value = c.website_url or (f"https://{c.domain}" if c.domain else None)
    elif f == "company_name":
        value = c.name
    elif f == "icp_score":
        value = await _icp_score(rc)
        source_id, evidence = "scoring", "Latest ICP score"
    elif f in {"city", "country", "region", "postal_code", "founded_year", "description", "phone", "address"}:
        value = getattr(c, f)
    else:
        return failed(plan, resolver=RESOLVER, error=f"Unsupported record field: {f}")

    if value is None or value == "":
        return unknown(plan, resolver=RESOLVER, evidence=f"No {f.replace('_', ' ')} on record", source_id=source_id)
    return ok(plan, value, resolver=RESOLVER, confidence=conf, evidence=evidence, source_id=source_id)
