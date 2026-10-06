"""Saved views (spec §80): filters, sort, column order/visibility/width, pinned columns, density."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from scout.db.enums import EntityType
from scout.db.models import SavedView
from scout.errors import Conflict, NotFound
from scout.query.filters import FilterGroup, SortSpec


class ViewLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: FilterGroup = Field(default_factory=FilterGroup)
    sort: list[SortSpec] = Field(default_factory=list)
    column_order: list[str] = Field(default_factory=list)
    column_visibility: dict[str, bool] = Field(default_factory=dict)
    column_widths: dict[str, int] = Field(default_factory=dict)
    pinned_columns: list[str] = Field(default_factory=list)
    density: str = "compact"


def view_to_dict(v: SavedView) -> dict[str, Any]:
    return {
        "id": v.id, "list_id": v.list_id, "entity_type": v.entity_type.value, "name": v.name, "filters": v.filters,
        "sort": v.sort, "column_order": v.column_order, "column_visibility": v.column_visibility,
        "column_widths": v.column_widths, "pinned_columns": v.pinned_columns, "density": v.density,
        "is_default": v.is_default, "position": v.position, "updated_at": v.updated_at,
    }


async def list_views(
    s: AsyncSession, workspace_id: uuid.UUID, *, list_id: uuid.UUID | None, entity_type: EntityType
) -> list[SavedView]:
    q = sa.select(SavedView).where(SavedView.workspace_id == workspace_id, SavedView.entity_type == entity_type)
    q = q.where(SavedView.list_id == list_id) if list_id else q.where(SavedView.list_id.is_(None))
    views = list((await s.scalars(q.order_by(SavedView.is_default.desc(), SavedView.position, SavedView.created_at))).all())
    if not views:
        default = SavedView(workspace_id=workspace_id, list_id=list_id, entity_type=entity_type, name="All", is_default=True)
        s.add(default)
        await s.flush()
        views = [default]
    return views


async def get_view(s: AsyncSession, workspace_id: uuid.UUID, view_id: uuid.UUID) -> SavedView:
    v = await s.get(SavedView, view_id)
    if v is None or v.workspace_id != workspace_id:
        raise NotFound("View not found")
    return v


async def create_view(
    s: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str,
    list_id: uuid.UUID | None,
    entity_type: EntityType,
    layout: ViewLayout,
    user_id: str | None,
) -> SavedView:
    existing = await s.scalar(
        sa.select(SavedView).where(
            SavedView.workspace_id == workspace_id,
            SavedView.list_id == list_id if list_id else SavedView.list_id.is_(None),
            SavedView.entity_type == entity_type,
            sa.func.lower(SavedView.name) == name.strip().lower(),
        )
    )
    if existing:
        raise Conflict(f'A view named "{existing.name}" already exists')
    pos = await s.scalar(
        sa.select(sa.func.coalesce(sa.func.max(SavedView.position), 0)).where(
            SavedView.workspace_id == workspace_id, SavedView.list_id == list_id if list_id else SavedView.list_id.is_(None)
        )
    )
    v = SavedView(
        workspace_id=workspace_id, list_id=list_id, entity_type=entity_type, name=name.strip(),
        filters=layout.filters.model_dump(mode="json"), sort=[x.model_dump() for x in layout.sort],
        column_order=layout.column_order, column_visibility=layout.column_visibility,
        column_widths=layout.column_widths, pinned_columns=layout.pinned_columns, density=layout.density,
        position=int(pos or 0) + 1, created_by=user_id,
    )
    s.add(v)
    await s.flush()
    return v


async def update_view(
    s: AsyncSession, workspace_id: uuid.UUID, view_id: uuid.UUID, *, name: str | None = None, layout: dict[str, Any] | None = None
) -> SavedView:
    v = await get_view(s, workspace_id, view_id)
    if name:
        v.name = name.strip()
    if layout:
        parsed = ViewLayout.model_validate({**_layout_of(v), **layout})
        v.filters = parsed.filters.model_dump(mode="json")
        v.sort = [x.model_dump() for x in parsed.sort]
        v.column_order = parsed.column_order
        v.column_visibility = parsed.column_visibility
        v.column_widths = parsed.column_widths
        v.pinned_columns = parsed.pinned_columns
        v.density = parsed.density
    return v


def _layout_of(v: SavedView) -> dict[str, Any]:
    return {
        "filters": v.filters or {}, "sort": v.sort or [], "column_order": v.column_order or [],
        "column_visibility": v.column_visibility or {}, "column_widths": v.column_widths or {},
        "pinned_columns": v.pinned_columns or [], "density": v.density,
    }


async def delete_view(s: AsyncSession, workspace_id: uuid.UUID, view_id: uuid.UUID) -> None:
    v = await get_view(s, workspace_id, view_id)
    if v.is_default:
        raise Conflict("The default view cannot be deleted")
    await s.delete(v)
