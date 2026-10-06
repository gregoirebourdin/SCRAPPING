"""Composite strategy: values built from data Scout already holds (e.g. *CEO email* = person → email →
verification status), with explicit "Missing dependency" reasons when prerequisites are absent."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import ColumnDataType, RoleFamily, Seniority
from scout.db.models import Email, Person
from scout.enrich.matching import find_terms, fold
from scout.enrich.resolvers.base import ResolveContext, failed, ok, unknown
from scout.enrich.types import CellResult

RESOLVER = "composite"

ROLE_ALIASES: dict[str, list[str]] = {
    "ceo": ["ceo", "chief executive", "directeur general", "directrice generale", "dg", "pdg", "president",
            "presidente", "managing director", "gerant", "gerante", "dirigeant", "dirigeante", "owner",
            "fondateur", "fondatrice", "founder", "co-founder", "cofounder", "general manager"],
    "founder": ["founder", "co-founder", "cofounder", "fondateur", "fondatrice", "co-fondateur", "cofondateur",
                "owner", "proprietaire"],
    "cto": ["cto", "chief technology", "directeur technique", "directrice technique", "head of engineering",
            "vp engineering", "head of tech"],
    "cmo": ["cmo", "chief marketing", "directeur marketing", "directrice marketing", "head of marketing",
            "marketing director", "responsable marketing", "vp marketing"],
    "cfo": ["cfo", "chief financial", "directeur financier", "directrice financiere", "daf", "head of finance"],
    "coo": ["coo", "chief operating", "directeur des operations", "directrice des operations", "head of operations"],
    "sales": ["sales", "commercial", "business developer", "account executive", "head of sales",
              "directeur commercial", "directrice commerciale", "vp sales"],
    "marketing": ["marketing", "growth", "acquisition", "communication"],
    "hr": ["hr", "human resources", "rh", "ressources humaines", "talent", "recrutement", "people"],
}
_FAMILY = {"ceo": RoleFamily.executive, "founder": RoleFamily.founder, "cto": RoleFamily.technology,
           "cmo": RoleFamily.marketing, "cfo": RoleFamily.finance, "coo": RoleFamily.operations,
           "sales": RoleFamily.sales, "marketing": RoleFamily.marketing, "hr": RoleFamily.hr}
_TOP = {Seniority.owner, Seniority.c_level}


def role_key(role: str) -> str | None:
    folded = fold(role)
    for key, aliases in ROLE_ALIASES.items():
        if key in folded.split() or find_terms(folded, aliases, first_only=True):
            return key
    return None


def role_score(person: Person, role: str) -> float:
    key = role_key(role)
    aliases = ROLE_ALIASES.get(key or "", []) or [role]
    title = f"{person.job_title or ''} {person.normalized_title or ''}"
    if find_terms(title, aliases, first_only=True):
        return 1.0
    if key and person.role_family == _FAMILY.get(key):
        return 0.6
    if key in {"ceo", "founder"} and person.seniority in _TOP:
        return 0.5
    return 0.0


async def _email_of_role(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    role = plan.concept or "CEO"
    if rc.company is None:
        return unknown(plan, resolver=RESOLVER, error="Missing dependency: company")
    async with session_scope() as s:
        people = (await s.scalars(
            sa.select(Person).where(Person.workspace_id == rc.workspace_id, Person.company_id == rc.company.id)
        )).all()
        ranked = sorted(
            ((role_score(p, role), p) for p in people),
            key=lambda x: (-x[0], -(x[1].decision_power or 0), -(x[1].identity_confidence or 0)),
        )
        ranked = [(sc, p) for sc, p in ranked if sc > 0]
        if not ranked:
            return unknown(plan, resolver=RESOLVER, error=f"Missing dependency: no {role} identified at this company")
        score, person = ranked[0]
        emails = (await s.scalars(
            sa.select(Email).where(Email.workspace_id == rc.workspace_id, Email.person_id == person.id)
            .order_by(Email.is_primary.desc(), Email.overall_confidence.desc().nulls_last())
        )).all()
    email = next((e for e in emails if e.id == person.primary_email_id), None) or (emails[0] if emails else None)
    if email is None:
        return unknown(plan, resolver=RESOLVER, error=f"Missing dependency: email for {person.full_name}")
    status = getattr(email.status, "value", email.status)
    conf = (email.overall_confidence if email.overall_confidence is not None else 0.5) * (1.0 if score >= 1 else 0.8)
    value: Any = email.address
    if plan.data_type == ColumnDataType.json:
        value = {"email": email.address, "status": status, "person_id": str(person.id), "person": person.full_name}
    return ok(plan, value, resolver=RESOLVER, confidence=conf,
              evidence=f"{person.full_name}" + (f" — {person.job_title}" if person.job_title else "") + f" · {status}",
              source_url=email.source_url, source_id="email_finder")


def missing_dependencies(rc: ResolveContext) -> list[str]:
    return [d for d in rc.plan.depends_on if rc.dependency_values.get(d) in (None, "")]


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    missing = missing_dependencies(rc)
    if missing:
        return unknown(plan, resolver=RESOLVER, error=f"Missing dependency: {', '.join(missing)}")
    if plan.field == "email_of_role":
        return await _email_of_role(rc)
    if plan.depends_on:
        values = {d: rc.dependency_values[d] for d in plan.depends_on}
        if plan.data_type == ColumnDataType.json:
            return ok(plan, values, resolver=RESOLVER, confidence=1.0, evidence="Combined from dependencies",
                      source_id="derived")
        text = " · ".join(str(v) for v in values.values())
        return ok(plan, text, resolver=RESOLVER, confidence=1.0, evidence="Combined from dependencies",
                  source_id="derived")
    return failed(plan, resolver=RESOLVER, error=f"Unsupported composite field: {plan.field}")
