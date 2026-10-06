"""Exclusion engine (spec §20–22, §88, §172–174) and suppression checks (spec §87).

Exclusion modes compile to explicit rules evaluated in batched SQL before any expensive work.
Company-level and person-level rules are independent, so "new people at known companies" is native.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import USER_FACING_EXPOSURES, EntityType, ExclusionMode, ExposureType, SuppressionEntity
from scout.db.models import ListMembership, Person, SuppressionEntry
from scout.schemas.campaign import ExclusionSpec


@dataclass
class ExclusionRule:
    entity: EntityType
    exposure_types: list[ExposureType] | None = None  # None = any user-facing exposure
    within_days: int | None = None
    list_ids: list[uuid.UUID] = field(default_factory=list)
    campaign_ids: list[uuid.UUID] = field(default_factory=list)
    import_ids: list[uuid.UUID] = field(default_factory=list)
    include_list_history: bool = True
    mode: ExclusionMode = ExclusionMode.CUSTOM

    def describe(self) -> str:
        what = "people" if self.entity == EntityType.person else "companies"
        if self.list_ids:
            return f"{what} in excluded lists"
        if self.import_ids:
            return f"{what} from excluded imports"
        if self.exposure_types == [ExposureType.EXPORTED]:
            return f"previously exported {what}"
        if self.exposure_types == [ExposureType.CONTACTED]:
            return f"contacted {what}"
        if self.within_days:
            return f"{what} seen in the last {self.within_days} days"
        return f"previously seen {what}"


def compile_rules(spec: ExclusionSpec, *, target_list_id: uuid.UUID | None = None) -> list[ExclusionRule]:
    """Translate an ExclusionSpec (mode + flags) into explicit rules."""
    P, C = EntityType.person, EntityType.company
    m = spec.mode
    rules: list[ExclusionRule] = []
    want_company = (
        spec.previous_companies
        or spec.include_company_scope
        or not spec.allow_new_people_at_existing_companies
    )
    if m == ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE:
        rules.append(ExclusionRule(P, mode=m))
        if want_company:
            rules.append(ExclusionRule(C, mode=m))
    elif m == ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES:
        rules.append(ExclusionRule(C, mode=m))
    elif m == ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES:
        rules += [ExclusionRule(P, mode=m), ExclusionRule(C, mode=m)]
    elif m == ExclusionMode.EXCLUDE_EXPORTED:
        rules.append(ExclusionRule(P, [ExposureType.EXPORTED], mode=m))
        if want_company:
            rules.append(ExclusionRule(C, [ExposureType.EXPORTED], mode=m))
    elif m in (ExclusionMode.EXCLUDE_SPECIFIC_LISTS, ExclusionMode.EXCLUDE_CURRENT_LIST):
        lists = list(spec.list_ids)
        if m == ExclusionMode.EXCLUDE_CURRENT_LIST and target_list_id:
            lists.append(target_list_id)
        if lists:
            if spec.list_scope in ("people", "both"):
                rules.append(ExclusionRule(P, list_ids=lists, mode=m))
            if spec.list_scope in ("companies", "both") or want_company:
                rules.append(ExclusionRule(C, list_ids=lists, mode=m))
    elif m == ExclusionMode.EXCLUDE_CONTACTED:
        rules.append(ExclusionRule(P, [ExposureType.CONTACTED], mode=m))
        if want_company:
            rules.append(ExclusionRule(C, [ExposureType.CONTACTED], mode=m))
    elif m == ExclusionMode.EXCLUDE_WITHIN_COOLDOWN:
        rules.append(ExclusionRule(P, within_days=spec.cooldown_days or 90, mode=m))
        if want_company:
            rules.append(ExclusionRule(C, within_days=spec.cooldown_days or 90, mode=m))
    elif m == ExclusionMode.CUSTOM:
        for r in spec.rules:
            rules.append(
                ExclusionRule(
                    EntityType(r.entity),
                    list(r.exposure_types) if r.exposure_types else None,
                    r.within_days,
                    list(r.list_ids),
                    list(r.campaign_ids),
                    list(r.import_ids),
                    r.include_list_history,
                    mode=m,
                )
            )
    # Imports explicitly excluded (e.g. "nobody from my uploaded file") apply in any mode.
    if spec.import_ids and m != ExclusionMode.CUSTOM:
        rules.append(ExclusionRule(P, import_ids=list(spec.import_ids), mode=m))
        if want_company:
            rules.append(ExclusionRule(C, import_ids=list(spec.import_ids), mode=m))
    # Explicit flags complete a NONE/other mode (e.g. previous_people=True with mode NONE from a template).
    if m == ExclusionMode.NONE:
        if spec.previous_people:
            rules.append(ExclusionRule(P, mode=ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE))
        if spec.previous_companies:
            rules.append(ExclusionRule(C, mode=ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES))
    return rules


async def _rule_matches(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    rule: ExclusionRule,
    ids: list[uuid.UUID],
    current_campaign_id: uuid.UUID | None,
) -> set[uuid.UUID]:
    if not ids:
        return set()
    hits: set[uuid.UUID] = set()
    types = [t.value for t in (rule.exposure_types or USER_FACING_EXPOSURES)]
    is_list_rule = bool(rule.list_ids)
    if not is_list_rule:
        params = {
            "ws": workspace_id,
            "etype": rule.entity.value,
            "ids": ids,
            "types": types,
            "within": rule.within_days,
            "campaigns": rule.campaign_ids,
            "imports": rule.import_ids,
            "current": current_campaign_id,
        }
        sql = """
            SELECT DISTINCT e.entity_id FROM lead_exposures e
            WHERE e.workspace_id = :ws AND e.entity_type = :etype AND e.entity_id = ANY(:ids)
              AND e.exposure_type = ANY(:types)
              AND (CAST(:within AS integer) IS NULL OR e.occurred_at >= now() - make_interval(days => CAST(:within AS integer)))
              AND (cardinality(CAST(:campaigns AS uuid[])) = 0 OR e.campaign_id = ANY(CAST(:campaigns AS uuid[])))
              AND (cardinality(CAST(:imports AS uuid[])) = 0 OR e.import_id = ANY(CAST(:imports AS uuid[])))
              AND (CAST(:current AS uuid) IS NULL OR e.campaign_id IS DISTINCT FROM CAST(:current AS uuid))
        """
        hits |= set((await s.execute(sa.text(sql), params)).scalars().all())
        return hits
    # list rules: current memberships (+ people's companies for company rules) + historical ADDED_TO_LIST
    if rule.entity == EntityType.person:
        q = sa.select(ListMembership.person_id).where(
            ListMembership.workspace_id == workspace_id,
            ListMembership.list_id.in_(rule.list_ids),
            ListMembership.person_id.in_(ids),
        )
        hits |= {r for r in (await s.scalars(q)).all() if r}
    else:
        q1 = sa.select(ListMembership.company_id).where(
            ListMembership.workspace_id == workspace_id,
            ListMembership.list_id.in_(rule.list_ids),
            ListMembership.company_id.in_(ids),
        )
        q2 = (
            sa.select(Person.company_id)
            .join(ListMembership, ListMembership.person_id == Person.id)
            .where(
                ListMembership.workspace_id == workspace_id,
                ListMembership.list_id.in_(rule.list_ids),
                Person.company_id.in_(ids),
            )
        )
        hits |= {r for r in (await s.scalars(q1)).all() if r}
        hits |= {r for r in (await s.scalars(q2)).all() if r}
    if rule.include_list_history:
        sql = """
            SELECT DISTINCT e.entity_id FROM lead_exposures e
            WHERE e.workspace_id = :ws AND e.entity_type = :etype AND e.entity_id = ANY(:ids)
              AND e.exposure_type = 'ADDED_TO_LIST' AND e.list_id = ANY(:lists)
              AND (CAST(:current AS uuid) IS NULL OR e.campaign_id IS DISTINCT FROM CAST(:current AS uuid))
        """
        hits |= set(
            (
                await s.execute(
                    sa.text(sql),
                    {
                        "ws": workspace_id,
                        "etype": rule.entity.value,
                        "ids": ids,
                        "lists": rule.list_ids,
                        "current": current_campaign_id,
                    },
                )
            )
            .scalars()
            .all()
        )
    return hits


async def excluded_entities(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    entity: EntityType,
    ids: Sequence[uuid.UUID],
    rules: Sequence[ExclusionRule],
    *,
    current_campaign_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, str]:
    """Return {entity_id: reason} for ids excluded by any rule of the given entity type."""
    ids = list(dict.fromkeys(ids))
    out: dict[uuid.UUID, str] = {}
    for rule in rules:
        if rule.entity != entity:
            continue
        remaining = [i for i in ids if i not in out]
        for hit in await _rule_matches(s, workspace_id, rule, remaining, current_campaign_id):
            out[hit] = rule.describe()
    return out


# ---------------------------------------------------------------------------------------------
# Suppression (always wins, checked first)
# ---------------------------------------------------------------------------------------------


async def suppressed_companies(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    company_ids: Sequence[uuid.UUID] = (),
    domains: Sequence[str] = (),
) -> tuple[set[uuid.UUID], set[str]]:
    ids_hit: set[uuid.UUID] = set()
    domains_hit: set[str] = set()
    if company_ids:
        rows = (
            await s.scalars(
                sa.select(SuppressionEntry.entity_id).where(
                    SuppressionEntry.workspace_id == workspace_id,
                    SuppressionEntry.entity_type == SuppressionEntity.company,
                    SuppressionEntry.entity_id.in_(list(company_ids)),
                )
            )
        ).all()
        ids_hit = {r for r in rows if r is not None}
    doms = [d.lower() for d in domains if d]
    if doms:
        domains_hit = set(
            (
                await s.scalars(
                    sa.select(SuppressionEntry.value).where(
                        SuppressionEntry.workspace_id == workspace_id,
                        SuppressionEntry.entity_type == SuppressionEntity.domain,
                        SuppressionEntry.value.in_(doms),
                    )
                )
            ).all()
        )
    return ids_hit, domains_hit


async def suppressed_people(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    person_ids: Sequence[uuid.UUID] = (),
    emails: Sequence[str] = (),
) -> tuple[set[uuid.UUID], set[str]]:
    ids_hit: set[uuid.UUID] = set()
    emails_hit: set[str] = set()
    if person_ids:
        rows = (
            await s.scalars(
                sa.select(SuppressionEntry.entity_id).where(
                    SuppressionEntry.workspace_id == workspace_id,
                    SuppressionEntry.entity_type == SuppressionEntity.person,
                    SuppressionEntry.entity_id.in_(list(person_ids)),
                )
            )
        ).all()
        ids_hit = {r for r in rows if r is not None}
    addrs = [e.lower() for e in emails if e]
    if addrs:
        emails_hit = set(
            (
                await s.scalars(
                    sa.select(SuppressionEntry.value).where(
                        SuppressionEntry.workspace_id == workspace_id,
                        SuppressionEntry.entity_type == SuppressionEntity.email,
                        SuppressionEntry.value.in_(addrs),
                    )
                )
            ).all()
        )
    return ids_hit, emails_hit


def default_exclusion_for_prompt(text: str) -> ExclusionMode | None:
    """Spec §21 default: 'new / fresh / not already scraped / never seen' → EXCLUDE_PREVIOUS_PEOPLE."""
    t = text.lower()
    triggers = (
        "new lead",
        "new leads",
        "new people",
        "new contacts",
        "fresh",
        "not already",
        "never seen",
        "never scraped",
        "haven't seen",
        "have not seen",
        "already seen",
        "already scraped",
        "previously scraped",
        "don't repeat",
        "do not repeat",
        "don't give me anyone",
        "do not include leads",
        "nouveaux",
        "nouvelles",
        "jamais vu",
        "déjà vu",
        "deja vu",
        "déjà scrap",
        "deja scrap",
        "pas déjà",
        "sans doublon",
        "only new",
        "seen before",
        "i have seen",
        "i've seen",
        "ive seen",
        "scraped before",
        "already have",
        "don't repeat anyone",
        "no duplicates",
        "jamais scrap",
        "déjà eu",
        "deja eu",
    )
    return ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE if any(k in t for k in triggers) else None
