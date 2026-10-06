"""Lists as first-class entities (spec §18) + row references for bulk actions.

Lists never own leads: memberships are links into the global registry, and removing a membership
never erases discovery history or exposures.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import EntityType, ExposureType
from scout.db.models import (
    Company,
    CustomColumn,
    CustomFieldValue,
    Email,
    List,
    ListMembership,
    Person,
    SavedView,
)
from scout.errors import Conflict, NotFound, ValidationFailed
from scout.query.filters import FilterGroup
from scout.query.rows import RowScope, select_ids
from scout.services import registry
from scout.services.exclusion import suppressed_companies, suppressed_people


class RowRef(BaseModel):
    """Reference to a set of rows without enumerating them (AI tools + bulk actions)."""

    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] | None = None
    selection: Literal["current"] | None = Field(
        default=None, description="The rows currently selected in the table"
    )
    filter: FilterGroup | None = None
    list_id: uuid.UUID | None = Field(default=None, description="Scope of the filter (default: all leads)")
    entity_type: EntityType = EntityType.person
    search: str | None = None


@dataclass
class ResolvedRows:
    entity_type: EntityType
    ids: list[uuid.UUID]


async def resolve_rows(
    s: AsyncSession, workspace_id: uuid.UUID, ref: RowRef, *, current_selection: list[uuid.UUID] | None = None
) -> ResolvedRows:
    if ref.selection == "current":
        ids = list(current_selection or [])
        if not ids:
            raise ValidationFailed(
                "No rows are selected", hint="Select rows in the table first, or describe them with a filter"
            )
    elif ref.ids is not None:
        ids = list(ref.ids)
    else:
        scope = RowScope(
            kind="list"
            if ref.list_id
            else ("people" if ref.entity_type == EntityType.person else "companies"),
            entity_type=ref.entity_type,
            list_id=ref.list_id,
        )
        ids = await select_ids(s, workspace_id, scope, filters=ref.filter, search=ref.search)
    model = Person if ref.entity_type == EntityType.person else Company
    if ids:
        owned = set(
            (
                await s.scalars(
                    sa.select(model.id).where(model.workspace_id == workspace_id, model.id.in_(ids))
                )
            ).all()
        )
        ids = [i for i in dict.fromkeys(ids) if i in owned]
    return ResolvedRows(entity_type=ref.entity_type, ids=ids)


# ---------------------------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------------------------


async def get_list(s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID) -> List:
    lst = await s.get(List, list_id)
    if lst is None or lst.workspace_id != workspace_id:
        raise NotFound("List not found")
    return lst


async def find_list_by_name(s: AsyncSession, workspace_id: uuid.UUID, name: str) -> List | None:
    return await s.scalar(
        sa.select(List).where(
            List.workspace_id == workspace_id,
            sa.func.lower(List.name) == name.strip().lower(),
            List.is_archived.is_(False),
        )
    )


async def create_list(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str,
    user_id: str | None,
    entity_type: EntityType = EntityType.person,
    description: str | None = None,
    color: str | None = None,
    source_campaign_id: uuid.UUID | None = None,
    filter_snapshot: dict[str, Any] | None = None,
    if_exists: Literal["error", "return", "suffix"] = "error",
) -> tuple[List, bool]:
    name = name.strip()
    if not name:
        raise ValidationFailed("List name cannot be empty")
    if len(name) > 120:
        raise ValidationFailed("List name is too long (max 120 characters)")
    existing = await find_list_by_name(s, workspace_id, name)
    if existing:
        if if_exists == "return":
            return existing, False
        if if_exists == "suffix":
            n = 2
            while await find_list_by_name(s, workspace_id, f"{name} ({n})"):
                n += 1
            name = f"{name} ({n})"
        else:
            raise Conflict(f'A list named "{existing.name}" already exists', list_id=str(existing.id))
    lst = List(
        workspace_id=workspace_id,
        name=name,
        entity_type=entity_type,
        description=description,
        color=color,
        source_campaign_id=source_campaign_id,
        filter_snapshot=filter_snapshot,
        created_by=user_id,
    )
    s.add(lst)
    await s.flush()
    s.add(
        SavedView(
            workspace_id=workspace_id, list_id=lst.id, entity_type=entity_type, name="All", is_default=True
        )
    )
    await s.flush()
    return lst, True


async def rename_list(
    s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID, name: str
) -> tuple[List, str]:
    lst = await get_list(s, workspace_id, list_id)
    other = await find_list_by_name(s, workspace_id, name)
    if other and other.id != lst.id:
        raise Conflict(f'A list named "{other.name}" already exists')
    old = lst.name
    lst.name = name.strip()
    return lst, old


async def set_archived(s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID, archived: bool) -> List:
    lst = await get_list(s, workspace_id, list_id)
    if not archived and await find_list_by_name(s, workspace_id, lst.name):
        lst.name = f"{lst.name} (restored)"
    lst.is_archived = archived
    lst.archived_at = sa.func.now() if archived else None
    return lst


async def delete_list(s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID) -> int:
    """Delete a list and its memberships. Registry entities, exposures and history are kept."""
    lst = await get_list(s, workspace_id, list_id)
    count = await s.scalar(
        sa.select(sa.func.count()).select_from(ListMembership).where(ListMembership.list_id == list_id)
    )
    await s.delete(lst)
    return int(count or 0)


async def duplicate_list(
    s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID, *, name: str | None, user_id: str | None
) -> List:
    src = await get_list(s, workspace_id, list_id)
    dst, _ = await create_list(
        s,
        workspace_id,
        name=name or f"{src.name} copy",
        user_id=user_id,
        entity_type=src.entity_type,
        description=src.description,
        color=src.color,
        if_exists="suffix",
    )
    await s.execute(
        sa.text(
            "INSERT INTO list_memberships (id, workspace_id, list_id, person_id, company_id, added_by, added_via, campaign_id) "
            "SELECT gen_random_uuid(), workspace_id, :dst, person_id, company_id, :uid, 'manual', campaign_id "
            "FROM list_memberships WHERE list_id = :src"
        ),
        {"dst": dst.id, "src": src.id, "uid": user_id},
    )
    # list-specific columns are duplicated with their values
    cols = (await s.scalars(sa.select(CustomColumn).where(CustomColumn.list_id == src.id))).all()
    for col in cols:
        new_col = CustomColumn(
            workspace_id=workspace_id,
            list_id=dst.id,
            name=col.name,
            slug=col.slug,
            data_type=col.data_type,
            kind=col.kind,
            entity_type=col.entity_type,
            resolver_type=col.resolver_type,
            instructions=col.instructions,
            configuration=col.configuration,
            source_preferences=col.source_preferences,
            confidence_threshold=col.confidence_threshold,
            refresh_policy=col.refresh_policy,
            position=col.position,
            is_hidden=col.is_hidden,
            created_by=user_id,
        )
        s.add(new_col)
        await s.flush()
        await s.execute(
            sa.text(
                "INSERT INTO custom_field_values (id, workspace_id, column_id, entity_type, entity_id, value_json, "
                "display_value, confidence, source_id, source_url, evidence, resolver, status, error, input_hash, "
                "is_user_override, model, cost_usd, observed_at) SELECT gen_random_uuid(), workspace_id, :new, "
                "entity_type, entity_id, value_json, display_value, confidence, source_id, source_url, evidence, "
                "resolver, status, error, input_hash, is_user_override, model, cost_usd, observed_at "
                "FROM custom_field_values WHERE column_id = :old"
            ),
            {"new": new_col.id, "old": col.id},
        )
    views = (
        await s.scalars(
            sa.select(SavedView).where(SavedView.list_id == src.id, SavedView.is_default.is_(False))
        )
    ).all()
    for v in views:
        s.add(
            SavedView(
                workspace_id=workspace_id,
                list_id=dst.id,
                entity_type=v.entity_type,
                name=v.name,
                filters=v.filters,
                sort=v.sort,
                column_order=v.column_order,
                column_visibility=v.column_visibility,
                column_widths=v.column_widths,
                pinned_columns=v.pinned_columns,
                density=v.density,
                position=v.position,
                created_by=user_id,
            )
        )
    await s.flush()
    return dst


# ---------------------------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------------------------


@dataclass
class MembershipChange:
    list_id: uuid.UUID
    added: list[uuid.UUID]
    already_present: int
    skipped_suppressed: int


async def add_to_list(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    list_id: uuid.UUID,
    entity_type: EntityType,
    ids: list[uuid.UUID],
    *,
    user_id: str | None,
    added_via: str = "manual",
    campaign_id: uuid.UUID | None = None,
) -> MembershipChange:
    lst = await get_list(s, workspace_id, list_id)
    if lst.is_archived:
        raise ValidationFailed(f'List "{lst.name}" is archived')
    ids = list(dict.fromkeys(ids))
    if not ids:
        return MembershipChange(list_id, [], 0, 0)
    # Company lists accept company ids; a people list given companies adds nothing (explicit error instead).
    if lst.entity_type != entity_type:
        if lst.entity_type == EntityType.company and entity_type == EntityType.person:
            company_ids = (
                await s.scalars(
                    sa.select(Person.company_id).where(
                        Person.workspace_id == workspace_id, Person.id.in_(ids), Person.company_id.isnot(None)
                    )
                )
            ).all()
            ids = list(dict.fromkeys(c for c in company_ids if c is not None))
            entity_type = EntityType.company
        else:
            raise ValidationFailed(f'"{lst.name}" is a people list; select people rather than companies')
    # Workspace isolation: only entities owned by this workspace can be added (never trust caller ids).
    if entity_type == EntityType.person:
        owned_q = sa.select(Person.id).where(Person.workspace_id == workspace_id, Person.id.in_(ids))
    else:
        owned_q = sa.select(Company.id).where(Company.workspace_id == workspace_id, Company.id.in_(ids))
    owned = set((await s.scalars(owned_q)).all())
    ids = [i for i in ids if i in owned]
    if not ids:
        return MembershipChange(list_id, [], 0, 0)
    # suppression always wins
    if entity_type == EntityType.person:
        sup_ids, _ = await suppressed_people(s, workspace_id, person_ids=ids)
        emails = dict(
            (
                await s.execute(
                    sa.select(Person.id, Email.address)
                    .join(Email, Email.id == Person.primary_email_id)
                    .where(Person.id.in_(ids))
                )
            ).all()
        )
        _, sup_emails = await suppressed_people(s, workspace_id, emails=list(emails.values()))
        sup_ids |= {pid for pid, addr in emails.items() if addr in sup_emails}
    else:
        sup_ids, _ = await suppressed_companies(s, workspace_id, company_ids=ids)
    keep = [i for i in ids if i not in sup_ids]
    col = "person_id" if entity_type == EntityType.person else "company_id"
    added: list[uuid.UUID] = []
    if keep:
        stmt = (
            pg_insert(ListMembership)
            .values(
                [
                    {
                        "workspace_id": workspace_id,
                        "list_id": list_id,
                        col: i,
                        "added_by": user_id,
                        "added_via": added_via,
                        "campaign_id": campaign_id,
                    }
                    for i in keep
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["list_id", col], index_where=sa.text(f"{col} IS NOT NULL")
            )
            .returning(getattr(ListMembership, col))
        )
        added = [r for r in (await s.execute(stmt)).scalars().all() if r]
    if added:
        await registry.record_exposures(
            s,
            workspace_id,
            ExposureType.ADDED_TO_LIST,
            person_ids=added if entity_type == EntityType.person else (),
            company_ids=added if entity_type == EntityType.company else (),
            list_id=list_id,
            campaign_id=campaign_id,
        )
    await s.execute(sa.update(List).where(List.id == list_id).values(updated_at=sa.func.now()))
    return MembershipChange(list_id, added, len(keep) - len(added), len(sup_ids))


async def remove_from_list(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    list_id: uuid.UUID,
    entity_type: EntityType,
    ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    await get_list(s, workspace_id, list_id)
    col = ListMembership.person_id if entity_type == EntityType.person else ListMembership.company_id
    res = await s.execute(
        sa.delete(ListMembership)
        .where(ListMembership.list_id == list_id, ListMembership.workspace_id == workspace_id, col.in_(ids))
        .returning(col)
    )
    return [r for r in res.scalars().all() if r]


async def move_between_lists(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    from_list_id: uuid.UUID,
    to_list_id: uuid.UUID,
    entity_type: EntityType,
    ids: list[uuid.UUID],
    *,
    user_id: str | None,
) -> tuple[MembershipChange, list[uuid.UUID]]:
    if from_list_id == to_list_id:
        raise ValidationFailed("Source and destination lists are the same")
    change = await add_to_list(
        s, workspace_id, to_list_id, entity_type, ids, user_id=user_id, added_via="manual"
    )
    removed = await remove_from_list(s, workspace_id, from_list_id, entity_type, ids)
    return change, removed


# ---------------------------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------------------------


async def list_overview(
    s: AsyncSession, workspace_id: uuid.UUID, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    q = (
        sa.select(List, sa.func.count(ListMembership.id))
        .outerjoin(ListMembership, ListMembership.list_id == List.id)
        .where(List.workspace_id == workspace_id)
        .group_by(List.id)
        .order_by(List.updated_at.desc())
    )
    if not include_archived:
        q = q.where(List.is_archived.is_(False))
    out = []
    for lst, count in (await s.execute(q)).all():
        out.append(
            {
                "id": lst.id,
                "name": lst.name,
                "description": lst.description,
                "entity_type": lst.entity_type.value,
                "color": lst.color,
                "is_archived": lst.is_archived,
                "count": int(count),
                "created_at": lst.created_at,
                "updated_at": lst.updated_at,
                "source_campaign_id": lst.source_campaign_id,
            }
        )
    return out


async def quality_summary(s: AsyncSession, workspace_id: uuid.UUID, list_id: uuid.UUID) -> dict[str, Any]:
    """Compact list stats (spec §148): counts, SAFE emails, decision-maker confidence, boolean column trues."""
    lst = await get_list(s, workspace_id, list_id)
    if lst.entity_type == EntityType.person:
        row = (
            await s.execute(
                sa.text(
                    "SELECT count(*) AS total, count(*) FILTER (WHERE e.status = 'SAFE') AS safe, "
                    "count(*) FILTER (WHERE e.id IS NOT NULL) AS with_email, avg(p.identity_confidence) AS person_conf, "
                    "avg(q.icp_score) AS avg_score "
                    "FROM list_memberships m JOIN people p ON p.id = m.person_id "
                    "LEFT JOIN emails e ON e.id = p.primary_email_id "
                    "LEFT JOIN LATERAL (SELECT icp_score FROM qualification_scores qs WHERE qs.person_id = p.id "
                    "ORDER BY computed_at DESC LIMIT 1) q ON TRUE WHERE m.list_id = :l"
                ),
                {"l": list_id},
            )
        ).one()
        stats: dict[str, Any] = {
            "total": row.total,
            "safe_emails": row.safe,
            "with_email": row.with_email,
            "decision_maker_confidence": float(row.person_conf) if row.person_conf is not None else None,
            "avg_icp_score": float(row.avg_score) if row.avg_score is not None else None,
        }
        entity_join = "JOIN people p ON p.id = m.person_id"
        person_expr, company_expr = "p.id", "p.company_id"
    else:
        total = await s.scalar(
            sa.select(sa.func.count()).select_from(ListMembership).where(ListMembership.list_id == list_id)
        )
        stats = {"total": int(total or 0)}
        entity_join = "JOIN companies c ON c.id = m.company_id"
        person_expr, company_expr = "NULL::uuid", "c.id"
    cols = (
        await s.scalars(
            sa.select(CustomColumn).where(
                CustomColumn.workspace_id == workspace_id,
                sa.or_(CustomColumn.list_id.is_(None), CustomColumn.list_id == list_id),
                CustomColumn.data_type == "boolean",
            )
        )
    ).all()
    booleans = []
    for col in cols[:6]:
        ent = person_expr if col.entity_type == EntityType.person else company_expr
        n = await s.scalar(
            sa.text(
                f"SELECT count(*) FROM list_memberships m {entity_join} JOIN custom_field_values v ON "
                f"v.column_id = :c AND v.entity_id = {ent} AND v.display_value = 'true' WHERE m.list_id = :l"
            ),
            {"c": col.id, "l": list_id},
        )
        booleans.append({"column_id": col.id, "name": col.name, "true_count": int(n or 0)})
    stats["boolean_columns"] = booleans
    return stats


async def values_for_cells(
    s: AsyncSession, column_id: uuid.UUID, entity_ids: list[uuid.UUID]
) -> list[CustomFieldValue]:
    return list(
        (
            await s.scalars(
                sa.select(CustomFieldValue).where(
                    CustomFieldValue.column_id == column_id, CustomFieldValue.entity_id.in_(entity_ids)
                )
            )
        ).all()
    )
